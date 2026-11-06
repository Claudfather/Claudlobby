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

from .config_units import current_git_declarations, staged_git_declarations
from .git_update import UpdateError, _sync
from .supervision_inventory import (
    FileSnapshot, collect_enrollment, is_unit_template, validate_darwin_unit,
    _environment, _darwin_source, _catalog, _wanted_by, _enablement,
)
from .supervision_runtime import (_call, assert_quiescent, assert_native_absent, unit_start_budget,
                                  assert_private_endpoint_quiet, RuntimeEvidenceError)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def consumer_scope(rows):
    """Consent binds immutable intent, never mutable progress."""
    return {row["target"]: _hash(row) for row in rows}


def validate_candidate_consumers(old_snapshot, candidate_snapshot, manager, *, baseline=None):
    """Reject unsupported roster or placement changes before maintenance."""
    if manager not in {"Linux", "Darwin"}:
        raise UpdateError("unsupported supervision platform")
    if old_snapshot is not None and any(old_snapshot.get(key) != candidate_snapshot.get(key)
           for key in ("installation", "data_root")):
        raise UpdateError("candidate consumer installation differs")
    def bindings(units):
        rows = [row for row in units if row["platform"] == manager]
        result = {}
        for row in rows:
            source = row["source"]
            if source in result:
                raise UpdateError("duplicate candidate consumer source")
            result[source] = (row["working_directory"], row["scope"], row.get("fleet"),
                              row.get("bot"), row.get("service"))
        return result
    old = bindings(baseline.units) if baseline is not None else (bindings(old_snapshot['units']) if old_snapshot is not None else {})
    candidate = bindings(candidate_snapshot['units'])
    if baseline is not None:
        old_enroll = {row["source"]: row["enroll"] for row in baseline.units if row["platform"] == manager}
        new_enroll = {row["source"]: row["enroll"] for row in candidate_snapshot["units"] if row["platform"] == manager}
        if old_enroll != new_enroll:
            raise UpdateError("sealed conversion must preserve native enrollment policy")
    if baseline is not None and (baseline.manager != manager or set(old) != set(candidate)):
        raise UpdateError('sealed conversion must preserve the exact native consumer roster')
    if set(old) - set(candidate):
        raise UpdateError("candidate removes native consumers; removal is unsupported in this update")
    if any(old[key] != candidate[key] for key in old):
        raise UpdateError("candidate relocates or rebinds a retained native consumer")
    names = [Path(source).name for source in candidate]
    if len(names) != len(set(names)):
        raise UpdateError("candidate native names collide")


def _content(snapshot):
    return base64.b64decode(snapshot["content"]["base64"], validate=True)


def _check(snapshot):
    actual = FileSnapshot.read(Path(snapshot["path"]))
    if (actual.resolved != snapshot["resolved"] or actual.mode != snapshot["mode"]
            or actual.link != snapshot["link"] or actual.sha256 != snapshot["sha256"]
            or actual.content != _content(snapshot)):
        raise UpdateError(f"native definition changed: {snapshot['path']}")


def freeze_service_intent(current_snapshot, adapter, install_directory, *, candidate_snapshot=None, rendered=None, baseline=None):
    """Freeze complete current declarations and actual native disposition."""
    install_directory = Path(install_directory)
    if not install_directory.is_absolute() or install_directory.resolve() != install_directory:
        raise UpdateError("native install directory must be absolute and unredirected")
    manager = platform.system()
    if baseline is not None:
        if current_snapshot is not None or baseline.install_directory != install_directory or baseline.manager != manager:
            raise UpdateError('sealed conversion native placement differs')
        observed_manager, observed_domain, _, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
        if (observed_manager, observed_domain) != (baseline.manager, baseline.domain):
            raise UpdateError("sealed conversion requires its original native manager domain")
        inventory = collect_enrollment(baseline.root, baseline.declarations, adapter=adapter,
                                       historical_release=baseline.release,
                                       **({"historical_parked": baseline.parked} if getattr(baseline, "parked", None) else {})).require_complete()
        rows = inventory.payload()['units']
        expected = {str(item.source): next((entry["target"] for entry in baseline.placements
                    if entry["source"] == str(item.source)),
                    baseline.domain + "/" + item.source.stem if manager == "Darwin" else item.source.name)
                    for item in baseline.declarations}
        observed = {row["declaration"]["source"]: row["target"] for row in rows}
        if expected != observed:
            raise UpdateError("sealed conversion inventory differs from historical native targets")
    elif current_snapshot is None:
        if candidate_snapshot is None or rendered is None:
            raise UpdateError("initial native preparation requires exact candidate render")
        rows = []
    else:
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
        parked = next((value for value in getattr(baseline, "parked", {}).values()
                       if value["target"] == row["target"]), None)
        masked = (manager == "Linux" and props.get("UnitFileState") == "masked-runtime"
                  and props.get("LoadState") == "masked" and props.get("ActiveState") in {"inactive", "failed"})
        previous = dict(parked["properties"]) if parked is not None else {}
        # A completed old producer pause owns this transient mask. Restore its
        # original enrollment/activity; quiet bots retain their observed pause.
        resume_producer = (masked and parked is not None and parked["phase"] == "producers"
            and row["declaration"]["scope"] != "bot" and bool(parked.get("paused_native"))
            and previous.get("UnitFileState") in {"enabled", "enabled-runtime", "disabled", "static"}
            and previous.get("LoadState") == "loaded")
        if resume_producer and (previous.get("ActiveState") not in
                ({"active", "activating", "inactive", "failed"} if row["target"] in scheduled else {"active", "inactive"})):
            raise UpdateError("original producer activity has no supported restoration intent")
        if masked and not resume_producer:
            masks = [node for node in inventory.payload()["observed_files"]
                     if Path(node["path"]).name == row["target"] and node["link"] == "/dev/null"]
            if len(masks) != 1 or row["installed"]:
                raise UpdateError("quiet masked consumer lacks exact runtime mask evidence")
            row["preserved_mask"] = {"snapshot": masks[0], "installed_path": str(install_directory / row["target"])}
        elif parked is not None:
            if row["installed"]:
                raise UpdateError("parked consumer unexpectedly has an installed definition")
            row["installed"] = [parked["snapshot"]]
            row["legacy_parked"] = parked
        row["manager"] = manager
        row["scheduled"] = row["target"] in scheduled
        if manager == "Darwin":
            # Parse the plist itself: the binding parser normalizes only identity.
            import plistlib
            plist = plistlib.loads(_content(row["generated"]))
            row["scheduled"] = any(key in plist for key in ("StartCalendarInterval", "StartInterval"))
        row["saved_native"] = None if is_unit_template(row["target"]) else " ".join(("not-found" if key == "UnitFileState" and props["LoadState"] == "not-found"
                                        else props[key]) for key in ("UnitFileState", "LoadState", "ActiveState"))
        row["restore_native"] = row["saved_native"]
        if parked is not None and not is_unit_template(row["target"]):
            if resume_producer:
                row["restore_native"] = " ".join(previous[key] for key in ("UnitFileState", "LoadState", "ActiveState"))
            elif not masked:
                row["restore_native"] = " ".join(previous[key] for key in ("UnitFileState", "LoadState")) + " " + props["ActiveState"]
        if row["scheduled"] and manager == "Linux":
            row["restore_native"] = " ".join(row["restore_native"].split()[:2] + ["inactive"])
        row["defer_native_load"] = (manager == "Darwin" and not row["scheduled"]
            and props.get("LoadState") == "loaded" and props.get("ActiveState") == "inactive"
            and bool(plist.get("RunAtLoad") or plist.get("KeepAlive")))
        row["bot_binding"] = None
        row["disposition"] = "resident"
        if row["declaration"]["scope"] == "bot":
            source = row["installed"][0] if row["installed"] else row["generated"]
            env = (_darwin_source(_content(source))["environment"] if manager == "Darwin"
                   else _environment(props.get("Environment", "")))
            if manager == "Linux" and (not row["installed"] or parked is not None):
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
                if parked is not None:
                    from .native_session import observe
                    observed = observe(adapter, Path(binding[0]), binding[1], binding[2])
                    row["disposition"] = "running"
                else:
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
                # A correct tmux stop can leave an owned non-listening socket.
                # Inventory has already proved native absence. Reuse the exact
                # endpoint proof without treating generated bytes as installed.
                assert_private_endpoint_quiet(socket, target=row["target"])
                row["disposition"] = "stopped" if masked else "uninstalled"
        if len(row["installed"]) > 1:
            raise UpdateError("consumer has multiple installed definitions")
    if candidate_snapshot is not None:
        validate_candidate_consumers(current_snapshot, candidate_snapshot, manager, baseline=baseline)
        if rendered is None:
            raise UpdateError("candidate native additions require exact staged bytes")
        old_sources = {row["declaration"]["source"] for row in rows}
        staged = staged_git_declarations(candidate_snapshot, rendered, manager)
        paired = {item["service"] for _, item, _ in staged if item["service"]}
        for declaration, item, content in staged:
            if str(declaration.source) not in old_sources:
                rows.append(_freeze_added(candidate_snapshot, adapter, install_directory, declaration, item, content, paired))
        if baseline is not None:
            originals = {row["declaration"]["source"]: row for row in rows if row["bot_binding"]}
            for declaration, item, content in staged:
                old = originals.get(str(declaration.source))
                if old is not None:
                    env = _darwin_source(content)["environment"] if manager == "Darwin" else {
                        key: value for line in content.decode().splitlines() if line.startswith("Environment=")
                        for entry in shlex.split(line.partition("=")[2])
                        for key, _, value in (entry.partition("="),)}
                    if env.get("TMUX_TMPDIR") != old["bot_binding"][2]:
                        raise UpdateError("sealed conversion cannot change the private native session socket")
        phases = {item["source"]: item["phase"] for item in candidate_snapshot["units"]}
        for row in rows:
            row["phase"] = phases[row["declaration"]["source"]]
    return rows


def _file(row):
    return row["installed"][0]["path"]


def _native(adapter, row, function, *args, timeout=30):
    return _call(adapter, function, row["target"], *args, timeout=timeout)


def _prepare_bot_readiness(store, record, progress, row, adapter, root):
    """Retain the lifecycle's readiness fence before this bot's native start."""
    if progress.get("readiness") is None:
        fence = _native(adapter, row, "svc_activation_bot_fence", root, row["bot_binding"][0]).split("\t")
        if len(fence) != 2 or not fence[0].isdigit() or int(fence[0]) <= 0 or not fence[1]:
            raise UpdateError("bot readiness fence is incomplete")
        progress["readiness"] = {"ceiling": int(fence[0]), "token": fence[1]}
        store.save(record)


def _wait_bot_readiness(progress, row, adapter, root, installed):
    """Observe the original fenced start using the shared lifecycle's full budget."""
    fence = progress.get("readiness")
    if (not isinstance(fence, dict) or set(fence) != {"ceiling", "token"}
            or type(fence["ceiling"]) is not int or fence["ceiling"] <= 0
            or not isinstance(fence["token"], str) or not fence["token"]):
        raise UpdateError("original bot readiness fence is unavailable; start remains pending")
    readiness = _native(adapter, row, "svc_activation_bot_ready", root, row["bot_binding"][0],
                        str(fence["ceiling"]), fence["token"], row["bot_binding"][2], timeout=fence["ceiling"] + 30)
    if readiness not in {"bridge-ready", "session-ready"}:
        raise UpdateError("bot readiness outcome is unknown; original start remains pending")
    return _native(adapter, row, "svc_bot_session_observe", *row["bot_binding"], installed, row["target"]) == "ready"


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


def _retain_legacy_parking(store, record, progress, row):
    """Transfer a proved before-image into this update's existing parking slot."""
    saved = row["installed"][0]
    source, parked = Path(saved["path"]), _park_path(store, row)
    if os.path.lexists(source):
        raise UpdateError("original parked native path reappeared")
    blob = Path(row["legacy_parked"]["journal"]).parent / "blobs" / saved["sha256"]
    if (blob.is_symlink() or blob.resolve() != blob or not blob.is_file()
            or blob.stat().st_uid != os.getuid() or hashlib.sha256(blob.read_bytes()).hexdigest() != saved["sha256"]):
        raise UpdateError("original parked native before-image changed")
    if progress.get("park") is None:
        progress["park"] = "pending"
        store.save(record)
    parked.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parked.parent.resolve() != parked.parent:
        raise UpdateError("native parking directory is redirected")
    if not os.path.lexists(parked):
        fd, name = tempfile.mkstemp(prefix=".retained-", dir=parked.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), saved["mode"])
                stream.write(_content(saved))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, parked)
            _sync(parked.parent)
        finally:
            temporary.unlink(missing_ok=True)
    _park(store, record, progress, row)


def _preserved_mask_quiet(row, adapter):
    retained = row["preserved_mask"]
    if os.path.lexists(retained["installed_path"]):
        raise UpdateError("installed definition would shadow the preserved runtime mask")
    _check(retained["snapshot"])
    observed = _native(adapter, row, "svc_inventory_state", retained["installed_path"], row["target"]).split()
    if len(observed) != 3 or observed[:2] != ["masked-runtime", "masked"] or observed[2] not in {"inactive", "failed"}:
        raise UpdateError("preserved runtime mask is no longer the effective quiet native state")
    binding = row["bot_binding"]
    socket = Path(binding[2]) / f"tmux-{os.getuid()}" / binding[1] if binding else None
    return assert_quiescent(adapter, installed_file=Path(row["declaration"]["source"]),
                            target=row["target"], socket_path=socket).details


_PAUSED = "paused, runtime mask preserved; installed publication withheld"


def _paused_bot_decided(store, key, adapter):
    """A bot this update left paused is a health error until someone decides (#2699).

    The update starts nothing and publishes nothing over the mask: nothing proves
    who placed it, or that the bot's session would resume. So the error keeps the
    update pending, and every restore pass (`host update continue` included)
    reads again whether a stop is recorded for the bot (`bot stop`) or it runs
    again. Either one ends it.
    """
    from .stop_record import read_stop
    record, progress, row = _row(store, key)
    fleet, bot = row["declaration"].get("fleet"), row["declaration"].get("bot")
    if read_stop(Path(row["bot_binding"][0])) is not None:
        decided = "the bot's stop is recorded"
    else:
        installed = row["preserved_mask"]["installed_path"]
        running = False
        try:
            state = _native(adapter, row, "svc_inventory_state", installed, key)
            if state.split()[1:] == ["loaded", "active"]:
                session = _native(adapter, row, "svc_bot_session_observe", *row["bot_binding"], installed, key)
                running, state = session == "ready", f"{state}, session {session}"
        except RuntimeEvidenceError as exc:
            state = f"unreadable, {exc}"
        if not running:
            choice = (f"either resume it (claudlobby --fleet {fleet} bot start {bot}) or record that it "
                      f"stays stopped (claudlobby --fleet {fleet} bot stop {bot}), and run "
                      "claudlobby host update continue")
            if state.split()[:2] == ["masked-runtime", "masked"]:
                raise UpdateError(f"this update left bot {fleet}/{bot} paused: {key} keeps its runtime mask "
                                  "and no stop is recorded for the bot, so its unit file was not published. "
                                  f"Clear the mask (systemctl --user unmask --runtime {key}), then {choice}")
            raise UpdateError(f"this update left bot {fleet}/{bot} down: {key} does not read as running "
                              f"(native state: {state}) and no stop is recorded for the bot: {choice}")
        decided = "the bot runs again"
    outcome = f"{_PAUSED}; {decided}"
    if progress.get("outcome") != outcome:
        progress["outcome"] = outcome
        store.save(record)
    return progress


def observe_quiet(store, key, adapter):
    """Fresh final observation; caller holds its exclusive maintenance fence."""
    record, progress, row = _row(store, key)
    if row.get("kind") == "added":
        evidence = observe_added_absence(row, adapter)
    elif row.get("preserved_mask"):
        evidence = _preserved_mask_quiet(row, adapter)
    elif not row["installed"] or is_unit_template(key):
        evidence = {"uninstalled_or_template": True, "intent": _hash(row)}
    else:
        binding = row["bot_binding"]
        socket = (Path(binding[2]) / f"tmux-{os.getuid()}" / binding[1]) if binding else None
        evidence = assert_quiescent(adapter, installed_file=Path(_file(row)), target=key,
                                    socket_path=socket).details
    record["quiet"][key] = {"identity": record["scope"][key], "evidence_sha256": _hash(evidence)}
    store.save(record)
    return evidence


def native_state_matches(row, observed, *, stopping=False):
    """Enrollment and load stay fixed. Scheduled work may finish between observations. At the stop, a timer or
    non-bot service that has gone down to inactive is also as the stop needs it, and so is a failed timer, which
    the pause resets; the restore brings each back as frozen. A failed or activating service is not: the pause
    does not reset a failed service, and one still activating ends failed once it is masked."""
    before, after = row["saved_native"].split(), observed.split()
    if row["scheduled"]:
        return before[:2] == after[:2] and len(after) == 3 and after[2] in {"active", "activating", "inactive", "failed"}
    if observed == row["saved_native"]:
        return True
    down = {"inactive", "failed"} if row["target"].endswith(".timer") else {"inactive"}
    return (stopping and not row["bot_binding"] and before[:2] == after[:2] and len(after) == 3
            and before[2] == "active" and after[2] in down)


_NO_BOT_START = "A bot cannot start while the update's fence is up, so no command clears this"


def _drift_remedy(row, key, observed):
    """What clears a drift. A command is named only where it is known to run no launcher, so it works under the fence."""
    saved, now = row["saved_native"].split(), observed.split()
    step = f'Put {key} back to "{row["saved_native"]}"'
    if row["bot_binding"] and saved[2] == "active" and (len(now) != 3 or now[2] != "active"):
        return _NO_BOT_START
    if len(now) == 3 and row["manager"] == "Linux":
        if now[1] == "masked" and saved[1] != "masked":
            step = f"Run systemctl --user unmask {'--runtime ' if now[0] == 'masked-runtime' else ''}{key}"
        elif now[1] == saved[1]:
            if {now[0], saved[0]} == {"enabled", "disabled"}:
                step = f"Run systemctl --user {saved[0].removesuffix('d')} {key}"
            elif saved[0] == "disabled" and now[0] == "enabled-runtime":
                step = f"Run systemctl --user disable --runtime {key}"
            elif now[2] == "failed":
                step = f"Run systemctl --user reset-failed {key}"
            elif now[2] == "activating" or (saved[2] == "inactive" and now[2] == "active"):
                step = f"Run systemctl --user stop {key}"
    return f"{step}, then run claudlobby host update continue"


def _drifted(subject, key, saved, observed, remedy):
    return UpdateError(f'{subject} drifted before stop: {key} was "{saved}" when this update was prepared '
                       f'and reads "{observed}" now. {remedy}')


def quiesce_consumer(store, key, adapter):
    record, progress, row = _row(store, key)
    if row.get("kind") == "added":
        observe_added_absence(row, adapter)
        progress["stop"] = "done"
        store.save(record)
        return progress
    if row.get("preserved_mask"):
        observe_quiet(store, key, adapter)
        record, progress, row = _row(store, key)
        progress["stop"] = "done"
        store.save(record)
        return progress
    if not row["installed"]:
        progress["stop"] = "done"
        store.save(record)
        return progress
    _check(row["generated"])
    _darwin(adapter, row)
    if not is_unit_template(key) and progress.get("stop") is None and (progress.get("park") is None or row.get("legacy_parked")):
        if not row.get("legacy_parked"):
            _check(row["installed"][0])
        _native(adapter, row, "svc_activation_assert_external", _file(row), key, str(os.getpid()))
        observed = _native(adapter, row, "svc_inventory_state" if row.get("legacy_parked")
                           else "svc_activation_snapshot", _file(row), key)
        if not native_state_matches(row, observed, stopping=True):
            raise _drifted("native state", key, row["saved_native"], observed, _drift_remedy(row, key, observed))
        if row["bot_binding"] and not row.get("legacy_parked"):
            session = _native(adapter, row, "svc_bot_session_observe", *row["bot_binding"], _file(row), key)
            if session not in {"ready", "absent", "dead"}:
                raise UpdateError("bot session disposition is unknown before stop")
            disposition = "running" if session == "ready" else "stopped"
            if disposition != row["disposition"]:
                remedy = (_NO_BOT_START if row["disposition"] == "running"
                          else "Stop its session without stopping its unit, then run claudlobby host update continue")
                raise _drifted("bot session disposition", key, row["disposition"], disposition, remedy)
    if row["manager"] == "Linux":
        if row.get("legacy_parked"):
            _retain_legacy_parking(store, record, progress, row)
        else:
            _park(store, record, progress, row)
    if is_unit_template(key):
        progress["stop"] = "done"
        store.save(record)
        return progress
    if (row.get("legacy_parked", {}).get("paused_native")
            and row["saved_native"].split()[:2] == ["masked-runtime", "masked"]):
        # The old completed producer pause is not repeated. Its before-image is
        # now in this operation's parking slot; freshly prove current quiet.
        observe_quiet(store, key, adapter)
        record, progress, row = _row(store, key)
        progress["stop"] = "done"
        store.save(record)
        return progress
    if progress.get("stop") is None:
        proof = progress.get("checkpoint")
        if (row["bot_binding"] and proof is not None
                and proof.get("kind") == "native-session-v1"):
            from .native_session import verify_live
            verify_live(Path(row["bot_binding"][0]), proof, adapter)
        progress["stop"] = "pending"
        store.save(record)
        capability = ()
        if row["bot_binding"]:
            capability = ("bot-owned", *row["bot_binding"])
        elif row["manager"] == "Linux" and row["scheduled"]:
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
    if row.get("kind") == "added":
        return _restore_added(store, record, progress, row, adapter, current_snapshot)
    if row.get("preserved_mask"):
        if progress.get("restore") != "done":
            _preserved_mask_quiet(row, adapter)
            progress["outcome"] = _PAUSED
            # The update's own work on this unit ends here: it publishes nothing
            # over the mask, so the schedules still restore after it. A bot left
            # paused is reported apart, below.
            progress["restore"] = "done"
            if row["bot_binding"]:
                # Saved with "done", so a pass cut off before the bot is read
                # leaves it to the next pass, whose read replaces or clears this.
                progress["restore_error"] = "this update left the bot paused and has not read it since"
            store.save(record)
        if row["bot_binding"]:
            return _paused_bot_decided(store, key, adapter)
        return progress
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
    held = _held_start(row, key, desired)
    observed = _native(adapter, row, "svc_activation_snapshot", source, key)
    if progress["restore"] == "files":
        _darwin(adapter, restored)
        if row["bot_binding"] and row["disposition"] == "running":
            _prepare_bot_readiness(store, record, progress, row, adapter, current_snapshot["data_root"])
        if held:
            _hold_start(adapter, row, held)
        progress["restore"] = "pending"
        store.save(record)
        _native(adapter, row, "svc_activation_resume", source, key, desired,
                timeout=unit_start_budget(source, source.read_bytes()))
        if held:
            _await_held_elapse(adapter, row, held)
        observed = _native(adapter, row, "svc_activation_snapshot", source, key)
        if (row["manager"] == "Darwin" and not row["scheduled"]
                and (row["disposition"] == "running" or desired.endswith(" active"))
                and not (row["bot_binding"] and (policy.get("RunAtLoad") or policy.get("KeepAlive")))
                and observed.endswith(" inactive")):
            progress["start"] = "pending"
            store.save(record)
            _native(adapter, row, "svc_activation_start", source, key,
                    timeout=unit_start_budget(source, source.read_bytes()))
            observed = _native(adapter, row, "svc_activation_snapshot", source, key)
    if held:
        _release_held_start(adapter, row, source, held)
    expected = observed == desired
    if row["manager"] == "Darwin" and row["scheduled"]:
        expected = observed.split()[:2] == desired.split()[:2]
    if row["bot_binding"] and row["disposition"] == "running":
        expected = _wait_bot_readiness(progress, row, adapter, current_snapshot["data_root"], source)
    if not expected:
        raise UpdateError("native restoration remains pending; observation only on retry")
    _darwin(adapter, restored)
    progress["restore"] = "done"
    store.save(record)
    return progress


# A restored timer must not run its job as catch-up. Started again in the same
# user manager, a timer counts from its previous start on the systemd versions
# upstream issue #31231 names (Debian 12's 252.39 among them), so every
# calendar time since then runs its job at once; a Persistent=true timer counts
# from its last run, so a time missed while the update held it runs at once
# too. So a timer restored to running starts held:
# runtime drop-ins give the timer one elapse at once (OnActiveSec=, without its
# accuracy window or random delay), and give its service an ExecCondition= that
# marks that elapse and skips the job. The elapse becomes the timer's last
# trigger, so its calendar continues from the restore. The hold goes once the
# mark is seen. A hold whose mark never comes stays, so the service skips every
# run, and the restore reports the timer until a pass sees a mark and releases it.
_HELD_START = "claudlobby-update-held-start.conf"
_HELD_START_WAIT_S = 30


def _held_start(row, key, desired):
    """The hold for a Linux timer restored to running; None for anything else."""
    import re
    if row["manager"] != "Linux" or not key.endswith(".timer") or desired.split()[-1] != "active":
        return None
    service = row["declaration"].get("service")
    if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", service):
        raise UpdateError("restored timer names no exact service")
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    if (not re.fullmatch(r"(/[A-Za-z0-9_.@-]+)+", str(runtime)) or runtime.is_symlink()
            or not runtime.is_dir() or runtime.stat().st_uid != os.getuid()):
        raise UpdateError("timer hold runtime directory is not this user's")
    units = runtime / "systemd" / "user"
    return {"service": service, "timer_hold": units / f"{key}.d" / _HELD_START,
            "service_hold": units / f"{service}.d" / _HELD_START,
            "mark": runtime / "claudlobby-update-held-start" / service}


def _drop_ins(adapter, row, unit):
    from .supervision_inventory import _key_values
    props = _key_values(_native(adapter, row, "svc_inventory_properties", unit).splitlines())
    if props.get("Id") != unit or "DropInPaths" not in props:
        raise UpdateError("held unit properties are not exact")
    return props["DropInPaths"].split()


def _write_hold(path, text):
    if path.parent.is_symlink():
        raise UpdateError("timer hold directory is redirected")
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".update-hold-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _hold_start(adapter, row, held):
    """Write both drop-ins and load them before the timer starts."""
    mark = held["mark"]
    if mark.parent.is_symlink():
        raise UpdateError("timer hold mark directory is redirected")
    mark.parent.mkdir(mode=0o700, exist_ok=True)
    mark.unlink(missing_ok=True)
    _write_hold(held["timer_hold"], "[Timer]\nOnActiveSec=1us\nAccuracySec=1us\nRandomizedDelaySec=0\n")
    _write_hold(held["service_hold"], f"[Service]\nExecCondition=\nExecCondition=/bin/sh -c ': >{mark}; exit 1'\n")
    _native(adapter, row, "svc_activation_reload")
    if _drop_ins(adapter, row, held["service"]) != [str(held["service_hold"])]:
        raise UpdateError("restored timer's service did not load its hold")


def _await_held_elapse(adapter, row, held):
    """The started timer's one elapse, marked by its held service."""
    import time
    if _drop_ins(adapter, row, row["target"]) != [str(held["timer_hold"])]:
        raise UpdateError("restored timer has not taken its held start: its hold did not load")
    deadline = time.monotonic() + _HELD_START_WAIT_S
    while not held["mark"].exists():
        if time.monotonic() >= deadline:
            raise UpdateError("restored timer has not taken its held start; its service stays held")
        time.sleep(0.05)


def _release_held_start(adapter, row, source, held):
    """Remove the hold once its elapse is marked, or while the timer is not running."""
    timer_hold, service_hold, mark = held["timer_hold"], held["service_hold"], held["mark"]
    if not (timer_hold.exists() or service_hold.exists()
            or str(service_hold) in _drop_ins(adapter, row, held["service"])
            or str(timer_hold) in _drop_ins(adapter, row, row["target"])):
        return
    if (not mark.exists() and _native(adapter, row, "svc_activation_snapshot", source,
                                      row["target"]).split()[-1] == "active"):
        raise UpdateError("restored timer has not taken its held start; its service stays held")
    for path in (timer_hold, service_hold):
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass  # the directory holds another drop-in
    _native(adapter, row, "svc_activation_reload")
    if str(service_hold) in _drop_ins(adapter, row, held["service"]):
        raise UpdateError("released timer hold is still loaded")
    mark.unlink(missing_ok=True)


def observe_added_absence(row, adapter):
    """Prove an approved new target is absent; no saved state is invented."""
    manager, domain, directories, names, loaded = _catalog(adapter.read("svc_inventory_catalog"))
    source = Path(row["declaration"]["source"])
    installed = Path(row["destination"])
    if manager != row["manager"] or installed.parent not in directories:
        raise UpdateError("added native placement differs from observed manager/search path")
    if manager == "Darwin" and row["target"].rsplit("/", 1)[0] != domain:
        raise UpdateError("added native domain differs from observed domain")
    if any((directory / source.name).exists() or (directory / source.name).is_symlink() for directory in directories):
        raise UpdateError("added native definition already exists")
    if source.name in names or source.name in loaded or (is_unit_template(source.name) and any(
            name.startswith(source.name.split("@", 1)[0] + "@") for name in loaded)):
        raise UpdateError("added native target is already observed")
    if [str(item) for item in directories] != row["search_directories"]:
        raise UpdateError("added native search path changed")
    binding = row["bot_binding"]
    socket = Path(binding[2]) / f"tmux-{os.getuid()}" / binding[1] if binding else None
    return assert_native_absent(adapter, manager=manager, domain=row["domain"], target=row["target"],
                                installed_file=installed, search_directories=directories,
                                socket_path=socket).details


def _freeze_added(snapshot, adapter, directory, declaration, item, content, paired):
    from .stop_record import read_stop
    from .supervision_inventory import _darwin_disabled
    import plistlib

    source = declaration.source
    manager, domain, directories, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
    placement = snapshot.get("supervision")
    if (placement is None or placement != {"manager": manager, "domain": domain, "directory": str(directory)}
            or directory not in directories or not directory.is_dir() or directory.stat().st_uid != os.getuid()
            or directory / source.name == source):
        raise UpdateError("new native consumer requires exact committed placement")
    if hashlib.sha256(content).hexdigest() != item["sha256"]:
        raise UpdateError("added staged native definition changed")
    target = domain + "/" + source.stem if manager == "Darwin" else source.name
    plist = plistlib.loads(content) if manager == "Darwin" else {}
    scheduled = (source.name in paired or source.suffix == ".timer" if manager == "Linux" else
                 any(key in plist for key in ("StartCalendarInterval", "StartInterval")))
    if scheduled and manager == "Darwin" and (plist.get("RunAtLoad") or plist.get("KeepAlive")):
        raise UpdateError("new schedule would execute during native load")
    row = {"kind": "added", "target": target, "manager": manager, "domain": domain, "installed": [],
           "phase": item["phase"], "scheduled": scheduled, "bot_binding": None,
           "disposition": "resident", "declaration": {"source": str(source), "scope": item["scope"],
           "working_directory": item["working_directory"], "environment": list(declaration.environment),
           "fleet": item["fleet"], "bot": item["bot"], "service": item["service"]},
           "destination": str(directory / source.name), "search_directories": [str(item) for item in directories],
           "candidate": {"sha256": item["sha256"],
           "mode": item["mode"], "content": {"base64": base64.b64encode(content).decode()}},
           "enroll": item["enroll"], "enabled": True, "run_at_load": bool(plist.get("RunAtLoad"))}
    if item["scope"] == "bot":
        tmpdir = dict(declaration.environment).get("TMUX_TMPDIR")
        if not tmpdir or not Path(tmpdir).is_absolute():
            raise UpdateError("added bot has no explicit private tmux directory")
        row["bot_binding"] = [item["working_directory"], source.stem, tmpdir]
        row["disposition"] = "uninstalled" if read_stop(Path(item["working_directory"])) else "running"
        if row["disposition"] == "uninstalled":
            row["enroll"] = False
    if manager == "Darwin":
        row["enabled"] = _darwin_disabled(adapter.read("svc_inventory_disabled", domain)).get(source.stem) != "disabled"
    else:
        wanted = _wanted_by(content, source, paired)
        row["enablement"] = _enablement({"installed": row["destination"], "original": False}, wanted, {})
    row["absence"] = observe_added_absence(row, adapter)
    return row


def _restore_added(store, record, progress, row, adapter, snapshot):
    source, installed = Path(row["declaration"]["source"]), Path(row["destination"])
    candidates = current_git_declarations(snapshot, row["manager"])
    declaration = next((item for item in candidates if item.source == source), None)
    candidate = row["candidate"]
    content = _content(candidate)
    if (declaration is None or str(declaration.working_directory) != row["declaration"]["working_directory"]
            or list(declaration.environment) != [tuple(item) for item in row["declaration"]["environment"]]
            or source.read_bytes() != content or hashlib.sha256(content).hexdigest() != candidate["sha256"]
            or source.stat().st_mode & 0o777 != candidate["mode"]):
        raise UpdateError("added native definition differs from committed candidate")
    if not row["enroll"]:
        observe_added_absence(row, adapter)
        progress.update(restore="done", outcome="left uninstalled")
        store.save(record)
        return progress
    if row["target"].endswith(".timer") or row["manager"] == "Darwin" and row["scheduled"]:
        if any(other.get("restore") != "done" for key, other in record["consumers"].items()
               if key != row["target"] and not (key.endswith(".timer") or other["intent"]["manager"] == "Darwin" and other["intent"]["scheduled"])):
            raise UpdateError("restore services before new schedules")
    if progress.get("restore") is None:
        observe_added_absence(row, adapter)
        progress["restore"] = "files"
        store.save(record)
    if installed.exists() or installed.is_symlink():
        if installed.is_symlink() or installed.read_bytes() != content or installed.stat().st_mode & 0o777 != candidate["mode"]:
            raise UpdateError("added installed native definition changed")
    else:
        if progress["restore"] != "files":
            raise UpdateError("published added native definition disappeared")
        fd, name = tempfile.mkstemp(prefix=".update-native-", dir=installed.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content); stream.flush(); os.fchmod(stream.fileno(), candidate["mode"]); os.fsync(stream.fileno())
            os.link(temporary, installed)  # Create only, never overwrite an unexpected node.
            _sync(installed.parent)
        finally:
            temporary.unlink(missing_ok=True)
    enablement = row.get("enablement")
    if enablement:
        link = Path(enablement["path"])
        if link.parent.resolve() != link.parent or link.parent.is_symlink():
            raise UpdateError("added enablement directory is redirected")
        link.parent.mkdir(mode=0o755, exist_ok=True)
        if not (link.exists() or link.is_symlink()):
            if progress["restore"] != "files":
                raise UpdateError("published added enablement disappeared")
            link.symlink_to(installed)
            _sync(link.parent)
        if not link.is_symlink() or os.readlink(link) != str(installed):
            raise UpdateError("added enablement link is foreign")
    if row["manager"] == "Linux":
        _native(adapter, row, "svc_activation_reload")
    # The canonical inventory proves effective bindings after publication too.
    collect_enrollment(Path(snapshot["data_root"]), candidates, adapter=adapter,
                       only_names=frozenset({source.name})).require_complete()
    if is_unit_template(row["target"]):
        progress["restore"] = "done"
        store.save(record); return progress
    observed = _native(adapter, row, "svc_inventory_state", installed, row["target"])
    should_start = row["enabled"] and not (row["manager"] == "Linux" and row["scheduled"] and source.suffix != ".timer")
    if progress["restore"] == "files":
        if row["bot_binding"] and should_start:
            _prepare_bot_readiness(store, record, progress, row, adapter, snapshot["data_root"])
        progress["restore"] = "pending"
        store.save(record)
        if row["manager"] == "Darwin" and row["enabled"]:
            _native(adapter, row, "svc_activation_resume", installed, row["target"], "unchanged loaded inactive",
                    timeout=unit_start_budget(source, content))
        elif should_start:
            _native(adapter, row, "svc_activation_start", installed, row["target"], timeout=unit_start_budget(source, content))
        observed = _native(adapter, row, "svc_inventory_state", installed, row["target"])
    if row["manager"] == "Darwin" and should_start and not row["scheduled"] and not row["run_at_load"] and observed == "unchanged loaded inactive":
        if progress.get("start") is None:
            progress["start"] = "pending"; store.save(record)
            _native(adapter, row, "svc_activation_start", installed, row["target"], timeout=unit_start_budget(source, content))
            observed = _native(adapter, row, "svc_inventory_state", installed, row["target"])
    expected = (observed.endswith(" active") if should_start and not row["scheduled"] else
                observed.startswith("unchanged loaded ") if row["manager"] == "Darwin" and row["enabled"] else
                observed == "unchanged unloaded inactive" if row["manager"] == "Darwin" else
                observed.endswith(" active") if source.suffix == ".timer" else observed.endswith(" inactive"))
    if row["bot_binding"] and should_start:
        expected = _wait_bot_readiness(progress, row, adapter, snapshot["data_root"], installed)
    if not expected:
        raise UpdateError("added consumer restoration pending; retry observes original native action")
    progress["restore"] = "done"
    store.save(record)
    return progress
