"""Native maintenance for the current UpdateStore, without another journal.

The coordinator owns save proof, phase order, exclusive maintenance and committed
configuration. Pending native effects are observed on retry, never resent.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import tempfile

from .config_units import current_git_declarations
from .git_update import UpdateError, _sync
from .supervision_inventory import (
    FileSnapshot, collect_enrollment, is_unit_template, validate_darwin_unit,
    _environment, _darwin_source,
)
from .supervision_runtime import _call, assert_quiescent, unit_start_budget


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def consumer_scope(rows):
    """Consent binds immutable intent, never mutable progress."""
    return {row["target"]: _hash(row) for row in rows}


def validate_candidate_consumers(old_snapshot, candidate_snapshot, manager):
    """Reject unsupported roster or placement changes before maintenance."""
    if manager not in {"Linux", "Darwin"}:
        raise UpdateError("unsupported supervision platform")
    if any(old_snapshot.get(key) != candidate_snapshot.get(key)
           for key in ("installation", "data_root")):
        raise UpdateError("candidate consumer installation differs")
    def bindings(snapshot):
        rows = [row for row in snapshot["units"] if row["platform"] == manager]
        result = {}
        for row in rows:
            source = row["source"]
            if source in result:
                raise UpdateError("duplicate candidate consumer source")
            result[source] = (row["working_directory"], row["scope"], row.get("fleet"),
                              row.get("bot"), row.get("service"))
        return result
    if bindings(old_snapshot) != bindings(candidate_snapshot):
        raise UpdateError("candidate adds, removes or relocates native consumers; unsupported in this update")


def _content(snapshot):
    return base64.b64decode(snapshot["content"]["base64"], validate=True)


def _check(snapshot):
    actual = FileSnapshot.read(Path(snapshot["path"]))
    if (actual.resolved != snapshot["resolved"] or actual.mode != snapshot["mode"]
            or actual.link != snapshot["link"] or actual.sha256 != snapshot["sha256"]
            or actual.content != _content(snapshot)):
        raise UpdateError(f"native definition changed: {snapshot['path']}")


def freeze_service_intent(current_snapshot, adapter, install_directory):
    """Freeze complete current declarations and actual native disposition."""
    install_directory = Path(install_directory)
    if not install_directory.is_absolute() or install_directory.resolve() != install_directory:
        raise UpdateError("native install directory must be absolute and unredirected")
    manager = platform.system()
    declarations = current_git_declarations(current_snapshot, manager)
    inventory = collect_enrollment(Path(current_snapshot["data_root"]), declarations,
                                   adapter=adapter).require_complete()
    rows = inventory.payload()["units"]
    scheduled = {row["declaration"]["service"] for row in rows
                 if row["target"].endswith(".timer")}
    for row in rows:
        if any(Path(node["path"]).parent != install_directory for node in row["installed"]):
            raise UpdateError("observed native definition differs from install directory")
        props = dict(row["properties"])
        row["manager"] = manager
        row["scheduled"] = row["target"] in scheduled
        if manager == "Darwin":
            # Parse the plist itself: the binding parser normalizes only identity.
            import plistlib
            plist = plistlib.loads(_content(row["generated"]))
            row["scheduled"] = any(key in plist for key in ("StartCalendarInterval", "StartInterval"))
        row["saved_native"] = " ".join(props[key] for key in ("UnitFileState", "LoadState", "ActiveState"))
        row["restore_native"] = row["saved_native"]
        if row["scheduled"] and manager == "Linux":
            row["restore_native"] = " ".join(row["saved_native"].split()[:2] + ["inactive"])
        row["defer_native_load"] = (manager == "Darwin" and not row["scheduled"]
            and props.get("LoadState") == "loaded" and props.get("ActiveState") == "inactive"
            and bool(plist.get("RunAtLoad") or plist.get("KeepAlive")))
        row["bot_binding"] = None
        row["disposition"] = "resident"
        if row["declaration"]["scope"] == "bot":
            source = row["installed"][0] if row["installed"] else row["generated"]
            env = (_darwin_source(_content(source))["environment"] if manager == "Darwin"
                   else _environment(props.get("Environment", "")))
            if manager == "Linux" and not row["installed"]:
                env = {}
                for line in _content(source).decode().splitlines():
                    if line.startswith("Environment="):
                        for entry in shlex.split(line.partition("=")[2]):
                            name, separator, value = entry.partition("=")
                            if not separator or name in env:
                                raise UpdateError("ambiguous uninstalled bot environment")
                            env[name] = value
            tmpdir = env.get("TMUX_TMPDIR")
            if not tmpdir or not Path(tmpdir).is_absolute():
                raise UpdateError("bot definition lacks explicit private tmux directory")
            binding = [row["declaration"]["working_directory"], Path(row["declaration"]["source"]).stem, tmpdir]
            row["bot_binding"] = binding
            if row["installed"]:
                observed = _native(adapter, row, "svc_bot_session_observe", *binding, _file(row), row["target"])
                if observed not in {"ready", "absent", "dead"}:
                    raise UpdateError("bot disposition is unknown")
                row["disposition"] = "running" if observed == "ready" else "stopped"
                if manager == "Darwin" and not row["scheduled"]:
                    row["defer_native_load"] = (observed != "ready"
                        and props.get("LoadState") == "loaded"
                        and bool(plist.get("RunAtLoad") or plist.get("KeepAlive")))
            else:
                socket = Path(tmpdir) / f"tmux-{os.getuid()}" / binding[1]
                if socket.exists() or socket.is_symlink():
                    raise UpdateError("uninstalled bot retains a private session socket")
                row["disposition"] = "uninstalled"
        if len(row["installed"]) > 1:
            raise UpdateError("consumer has multiple installed definitions")
    return rows


def _file(row):
    return row["installed"][0]["path"]


def _native(adapter, row, function, *args, timeout=30):
    return _call(adapter, function, row["target"], *args, timeout=timeout)


def _row(store, key):
    store._assert_locked()
    record = store.load()
    row = record["consumers"][key]
    intent = row["intent"]
    if intent["target"] != key or record["scope"].get(key) != _hash(intent):
        raise UpdateError("consumer differs from frozen scope")
    return record, row, intent


def _darwin(adapter, row):
    if row["manager"] == "Darwin":
        validate_darwin_unit(adapter, row["target"], source=_content(row["generated"]),
            installed_path=_file(row), working_directory=row["declaration"]["working_directory"],
            environment=dict(row["declaration"]["environment"]), original=dict(row["properties"]))


def _park_path(store, row):
    return store.work / "native" / _hash(row)


def _park(store, record, progress, row):
    source, parked = Path(_file(row)), _park_path(store, row)
    parked.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parked.parent.is_symlink() or parked.parent.resolve() != parked.parent:
        raise UpdateError("native parking directory is redirected")
    if progress.get("park") is None:
        _check(row["installed"][0])
        progress["park"] = "pending"
        store.save(record)
    if source.exists() or source.is_symlink():
        _check(row["installed"][0])
        if parked.exists() or parked.is_symlink():
            raise UpdateError("both native definition and parked node exist")
        os.replace(source, parked)
        _sync(source.parent)
        _sync(parked.parent)
    elif not (parked.exists() or parked.is_symlink()):
        raise UpdateError("native definition and parked node are both absent")
    # Symlink before images retain their literal link, including relative links.
    saved = row["installed"][0]
    if saved["link"] is not None:
        if not parked.is_symlink() or os.readlink(parked) != saved["link"]:
            raise UpdateError("parked native link changed")
    elif parked.is_symlink() or parked.read_bytes() != _content(saved) or parked.stat().st_mode & 0o777 != saved["mode"]:
        raise UpdateError("parked native definition changed")
    progress["park"] = "done"
    store.save(record)


def observe_quiet(store, key, adapter):
    """Fresh final observation; caller holds its exclusive maintenance fence."""
    record, progress, row = _row(store, key)
    if not row["installed"] or is_unit_template(key):
        evidence = {"uninstalled_or_template": True, "intent": _hash(row)}
    else:
        binding = row["bot_binding"]
        socket = (Path(binding[2]) / f"tmux-{os.getuid()}" / binding[1]) if binding else None
        evidence = assert_quiescent(adapter, installed_file=Path(_file(row)), target=key,
                                    socket_path=socket).details
    record["quiet"][key] = {"identity": record["scope"][key], "evidence_sha256": _hash(evidence)}
    store.save(record)
    return evidence


def quiesce_consumer(store, key, adapter):
    record, progress, row = _row(store, key)
    if not row["installed"]:
        progress["stop"] = "done"
        store.save(record)
        return progress
    _check(row["generated"])
    _darwin(adapter, row)
    if progress.get("stop") is None and progress.get("park") is None:
        _check(row["installed"][0])
        _native(adapter, row, "svc_activation_assert_external", _file(row), key, str(os.getpid()))
        observed = _native(adapter, row, "svc_activation_snapshot", _file(row), key)
        if row["scheduled"]:
            before, after = row["saved_native"].split(), observed.split()
            same = before[:2] == after[:2] and len(after) == 3 and after[2] in {"active", "activating", "inactive", "failed"}
        else:
            same = observed == row["saved_native"]
        if not same:
            raise UpdateError("native state drifted before stop")
        if row["bot_binding"]:
            session = _native(adapter, row, "svc_bot_session_observe", *row["bot_binding"], _file(row), key)
            if session not in {"ready", "absent", "dead"}:
                raise UpdateError("bot session disposition is unknown before stop")
            disposition = "running" if session == "ready" else "stopped"
            if disposition != row["disposition"]:
                raise UpdateError("bot session disposition drifted before stop; prepare from its intended state")
    if row["manager"] == "Linux":
        _park(store, record, progress, row)
    if is_unit_template(key):
        progress["stop"] = "done"
        store.save(record)
        return progress
    if progress.get("stop") is None:
        progress["stop"] = "pending"
        store.save(record)
        capability = ()
        if row["manager"] == "Linux":
            if row["bot_binding"]:
                capability = ("bot-owned", *row["bot_binding"])
            elif row["scheduled"]:
                capability = ("timer-owned",)
        _native(adapter, row, "svc_activation_pause", _file(row), key, row["restore_native"],
                str(os.getpid()), *capability, timeout=120)
    observe_quiet(store, key, adapter)
    # observe_quiet reloads the same record; preserve its fresh receipt.
    record, progress, row = _row(store, key)
    progress["stop"] = "done"
    store.save(record)
    return progress


def restore_consumer(store, key, adapter, current_snapshot):
    record, progress, row = _row(store, key)
    if not row["installed"]:
        progress["restore"] = "done"
        store.save(record)
        return progress
    if key.endswith(".timer") or row["manager"] == "Darwin" and row["scheduled"]:
        for other_key, other in record["consumers"].items():
            intent = other["intent"]
            if not (other_key.endswith(".timer") or intent["manager"] == "Darwin" and intent["scheduled"]):
                if other.get("restore") != "done":
                    raise UpdateError("restore services before schedules")
    declarations = current_git_declarations(current_snapshot, row["manager"])
    declaration = next((item for item in declarations if str(item.source) == row["declaration"]["source"]), None)
    if (declaration is None or str(declaration.working_directory) != row["declaration"]["working_directory"]
            or getattr(declaration, "bot", row["declaration"].get("bot")) != row["declaration"].get("bot")):
        raise UpdateError("restored consumer binding differs from committed configuration")
    source = Path(_file(row))
    parked = _park_path(store, row)
    if progress.get("restore") is None:
        progress["restore"] = "files"
        store.save(record)
    # The committed generated source is the only publication input. Persist the
    # intended replacement before touching the exact frozen installed node.
    candidate = declaration.source.read_bytes()
    old = row["installed"][0]
    if source.exists() or source.is_symlink():
        if source.is_symlink():
            if old["link"] is None or os.readlink(source) != old["link"] or str(source.resolve()) != old["resolved"]:
                raise UpdateError("installed consumer link ownership changed")
            if source.read_bytes() != candidate:
                raise UpdateError("installed consumer link differs from committed definition")
        elif (old["link"] is not None or source.read_bytes() not in (_content(old), candidate)
              or source.stat().st_mode & 0o777 not in {old["mode"], declaration.source.stat().st_mode & 0o777}):
            raise UpdateError("installed consumer ownership changed")
    elif not (row["manager"] == "Linux" and (parked.exists() or parked.is_symlink())):
        raise UpdateError("restored native definition is absent")
    if progress["restore"] == "files":
        progress["publication"] = hashlib.sha256(candidate).hexdigest()
        store.save(record)
        if not source.is_symlink():
            # Returning a symlink preserves its literal spelling; generated
            # publication has already updated its committed destination.
            if old["link"] is not None:
                if not parked.is_symlink() or os.readlink(parked) != old["link"]:
                    raise UpdateError("parked native link changed")
                os.replace(parked, source)
                if source.read_bytes() != candidate or str(source.resolve()) != old["resolved"]:
                    raise UpdateError("restored native link differs from committed definition")
            else:
                if parked.exists() and (parked.is_symlink() or parked.read_bytes() != _content(old)):
                    raise UpdateError("parked native definition changed")
                fd, name = tempfile.mkstemp(prefix=".update-native-", dir=source.parent)
                temporary = Path(name)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(candidate)
                        stream.flush()
                        os.fchmod(stream.fileno(), declaration.source.stat().st_mode & 0o777)
                        os.fsync(stream.fileno())
                    os.replace(temporary, source)
                finally:
                    temporary.unlink(missing_ok=True)
            _sync(source.parent)
        progress["publication_complete"] = True
        store.save(record)
    if is_unit_template(key):
        progress["restore"] = "done"
        store.save(record)
        return progress
    restored = dict(row)
    restored["generated"] = dict(row["generated"])
    restored["generated"]["content"] = {"base64": base64.b64encode(candidate).decode()}
    restored["declaration"] = dict(row["declaration"])
    restored["declaration"]["environment"] = getattr(declaration, "environment", row["declaration"]["environment"])
    defer_load = row.get("defer_native_load", False)
    if row["manager"] == "Darwin" and not row["scheduled"]:
        import plistlib
        policy = plistlib.loads(candidate)
        was_stopped = (row["disposition"] == "stopped" if row["bot_binding"]
                       else row["saved_native"].endswith(" inactive"))
        defer_load = was_stopped and bool(policy.get("RunAtLoad") or policy.get("KeepAlive"))
    if defer_load:
        _darwin(adapter, restored)
        observed = _native(adapter, row, "svc_inventory_state", source, key)
        if observed != "unchanged unloaded inactive":
            raise UpdateError("previously stopped launchd consumer is not unloaded and inactive")
        progress["outcome"] = "stopped, deferred native load"
        progress["restore"] = "done"
        store.save(record)
        return progress
    desired = row["restore_native"]
    if row["bot_binding"] and row["disposition"] != "running":
        desired = " ".join(desired.split()[:2] + ["inactive"])
    observed = _native(adapter, row, "svc_activation_snapshot", source, key)
    if progress["restore"] == "files":
        _darwin(adapter, restored)
        progress["restore"] = "pending"
        store.save(record)
        _native(adapter, row, "svc_activation_resume", source, key, desired,
                timeout=unit_start_budget(source, source.read_bytes()))
        observed = _native(adapter, row, "svc_activation_snapshot", source, key)
        if (row["manager"] == "Darwin" and not row["scheduled"]
                and (row["disposition"] == "running" or desired.endswith(" active"))
                and observed.endswith(" inactive")):
            progress["start"] = "pending"
            store.save(record)
            _native(adapter, row, "svc_activation_start", source, key,
                    timeout=unit_start_budget(source, source.read_bytes()))
            observed = _native(adapter, row, "svc_activation_snapshot", source, key)
    expected = observed == desired
    if row["manager"] == "Darwin" and row["scheduled"]:
        expected = observed.split()[:2] == desired.split()[:2]
    if row["bot_binding"] and row["disposition"] == "running":
        expected = _native(adapter, row, "svc_bot_session_observe", *row["bot_binding"], source, key) == "ready"
    if not expected:
        raise UpdateError("native restoration remains pending; observation only on retry")
    _darwin(adapter, restored)
    progress["restore"] = "done"
    store.save(record)
    return progress
