"""Operator-only exact job de-enrollment. No config mutation or intent store.

Only the recorder, owner server and three health jobs with native-owned children
are supported. Installed absence is retained by the ordinary updater.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import time

from .activation_state import ActivationRefusal
from .host_job_operations import job_effect_lock, _bind_git_job
from .runtime_admission import RuntimeIdentity, mutation_admission
from .supervision_inventory import Adapter, FileSnapshot, _darwin_print
from .supervision_runtime import assert_native_absent, assert_private_endpoint_quiet, assert_quiescent


class JobStopError(RuntimeError):
    def __init__(self, reason, *, data=None, attempted=False):
        self.data = data or {"native_outcome": "unattempted"}
        self.attempted = attempted
        super().__init__(reason)


@dataclass(frozen=True)
class JobStop:
    name: str
    fleet: str | None
    revision: str
    config_fingerprint: str
    members: tuple[dict, ...]
    configuration_changed: bool = False
    enrollment: str = "absent"


def darwin_native_children_owned(text: str, target: str) -> bool:
    """Accept only positively observed native properties, never plist inference.

    False GUI captures and a true system-domain capture establish the token
    grammar. Unknown flags/structure remain unsupported; this is not a generic
    detached-process detector or proof of native teardown.
    """
    observed = _darwin_print(text, target)
    if observed["UnknownFields"] != "[]" or observed["Type"] != "LaunchAgent":
        return False
    lines = [line[len("\tproperties = "):] for line in text.splitlines()
             if line.startswith("\tproperties = ")]
    if len(lines) != 1:
        return False
    tokens = lines[0].split(" | ")
    if (not tokens or len(tokens) != len(set(tokens))
            or "abandon process group" in tokens):
        return False
    # KeepAlive restarts a resident; it does not abandon its native children.
    # Only the non-abandoning GUI flag vocabulary captured live is accepted.
    return bool(set(tokens) <= {"keepalive", "runatload", "inferred program"}
                and "inferred program" in tokens)


def _process_table():
    """Identity metadata only; no argv or environment, no process signalling."""
    result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,pgid=,uid=,lstart="],
                            text=True, capture_output=True, check=True, timeout=5)
    table = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) != 5:
            raise JobStopError("process identity observation is incomplete")
        pid, parent, group, uid = map(int, fields[:4])
        if pid in table:
            raise JobStopError("process identity observation is ambiguous")
        table[pid] = (parent, group, uid, fields[4])
    return table


def _capture_group(pid):
    table = _process_table()
    if pid not in table or table[pid][1] != pid or table[pid][2] != os.getuid():
        raise JobStopError("native job lacks its own attributable process group")
    selected = {p: row for p, row in table.items() if row[1] == pid}
    descendants = {pid}
    while True:
        added = {p for p, row in table.items() if row[0] in descendants} - descendants
        if not added:
            break
        descendants |= added
    if (not descendants <= selected.keys() or os.getpid() in selected
            or any(row[2] != os.getuid() for row in selected.values())):
        raise JobStopError("native job has unsupported descendant ownership")
    return pid, selected


def _group_quiet(witness):
    if witness is None:
        return
    group, selected = witness
    table = _process_table()
    if any(row[1] == group for row in table.values()) or any(
            pid in table and table[pid][3] == row[3] for pid, row in selected.items()):
        raise JobStopError("pre-stop job process witnesses remain")


def _await_group_quiet(witness):
    deadline = time.monotonic() + 10
    while True:
        try:
            _group_quiet(witness)
            return
        except JobStopError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def _node(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or path.parent.resolve() != path.parent):
        raise JobStopError("job definition is redirected or not owned")
    return (info.st_dev, info.st_ino), FileSnapshot.read(path)


def _pin_node(path, pins):
    # Keep the original inode allocated until all checks/effects have finished.
    # A snapshot alone cannot distinguish unlink/recreate with inode reuse.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    pins.callback(os.close, fd)
    opened = os.fstat(fd)
    saved = _node(path)
    if (opened.st_dev, opened.st_ino) != saved[0]:
        raise JobStopError("job definition changed during observation")
    return saved


def _check_node(path, saved):
    if _node(path) != saved:
        raise JobStopError("job definition changed before effect")


def _pin_link(link, target, pins):
    # O_PATH pins the symlink itself on Linux. O_SYMLINK supplies the same
    # ownership check for Linux-catalog fixtures running on macOS.
    if hasattr(os, "O_PATH"):
        flags = os.O_PATH | os.O_NOFOLLOW
    elif hasattr(os, "O_SYMLINK"):
        flags = os.O_RDONLY | os.O_SYMLINK
    else:
        raise JobStopError("symlink identity pinning is unsupported")
    fd = os.open(link, flags)
    pins.callback(os.close, fd)
    opened, current = os.fstat(fd), link.lstat()
    identity = (opened.st_dev, opened.st_ino)
    if (not stat.S_ISLNK(opened.st_mode) or opened.st_uid != os.getuid()
            or identity != (current.st_dev, current.st_ino)
            or link.resolve() != target):
        raise JobStopError("job enablement node changed during observation")
    return link, identity, os.readlink(link)


def _link_matches(link, identity, value):
    current = link.lstat()
    return (stat.S_ISLNK(current.st_mode)
            and (current.st_dev, current.st_ino) == identity
            and os.readlink(link) == value)


def _check_links(links):
    for link, identity, value in links:
        if not _link_matches(link, identity, value):
            raise JobStopError("job enablement node changed before effect")


def _owner_socket(current, service, admission):
    from .installation import parse_git_unit_argv
    from .plane.owner_service import service_command, unit_socket

    installation = current.installation
    expected = {"CLAUDLOBBY_ROOT": str(installation.data_root),
                "CLAUDLOBBY_INSTALLATION": str(installation.directory),
                "CLAUDLOBBY_CLI": str(installation.cli),
                "CLAUDLOBBY_NATIVE_DIR": str(installation.native),
                "CLAUDLOBBY_LIBRARY_DIR": str(installation.source / "library")}
    env = dict(service.environment)
    unit = parse_git_unit_argv(admission["argv"])
    socket = unit_socket(unit)
    if (socket is None or admission["mode"] != "exec" or unit.argv[0] != str(installation.cli)
            or unit.root != installation.data_root or unit.unit != service.source.stem
            or unit.phase != "producers" or unit.mode != "exec"
            or str(service.working_directory) != str(installation.data_root)
            or any(env.get(key) != value for key, value in expected.items())
            or any(key in env for key in ("BOT_ID", "BOT_DIR", "BOT_NAME", "BOT_SERVICE",
                                         "FLEET_ROOT", "FLEET_NAME", "CLAUDLOBBY_FLEET",
                                         "CLAUDLOBBY_TIMER_CONTEXT"))):
        raise JobStopError("owner execution does not bind the current installation")
    options = dict(zip(unit.command[6::2], unit.command[7::2]))
    config = {key: options.get("--" + key.replace("_", "-")) for key in (
        "origin", "tailscale", "tailscale_socket", "socket", "workspace_issuer", "workspace_origin")}
    if (list(unit.command) != admission["command"]
            or list(unit.command) != service_command(installation.data_root, installation.cli, config)):
        raise JobStopError("owner command differs from its canonical service configuration")
    return socket


def _shape(service, members, metadata, manager, name, current):
    """Only canonical execution shapes; flags must agree across platforms."""
    admission = metadata[str(service.source)]["admission"]
    if admission is None:
        raise JobStopError("job has no admitted execution shape")
    if metadata[str(service.source)]["phase"] != ("ingest" if name == "plane-daemon" else "producers"):
        raise JobStopError("job phase differs from supported execution shape")
    command = admission["command"]
    env = dict(service.environment)
    native = env.get("CLAUDLOBBY_NATIVE_DIR", "")
    root = env.get("CLAUDLOBBY_ROOT", "")
    cli = env.get("CLAUDLOBBY_CLI", "")
    owner_socket = None
    if name == "plane-owner":
        try:
            owner_socket = _owner_socket(current, service, admission)
        except (KeyError, TypeError, ValueError, ImportError, ActivationRefusal) as exc:
            raise JobStopError("owner execution configuration is unsupported") from exc
    elif name == "plane-daemon":
        if (admission["mode"] != "exec" or command != [cli, "--root", root, "plane", "serve"]
                or "PLANE_SOCKET" in env):
            raise JobStopError("unsupported recorder execution or socket override")
    else:
        script = {"keepalive": "keepalive-all.sh", "fleet-pulse": "fleet-pulse.sh",
                  "log-rotation": "log-rotate-fleet.sh"}[name]
        expected = ([cli, "--root", root, "--fleet", service.fleet, "fleet", "pulse"]
                    if name == "fleet-pulse" else [str(Path(native) / script), service.fleet])
        if (admission["mode"] != "oneshot" or not native or not service.fleet
                or command != expected):
            raise JobStopError("unsupported health job execution shape")
    # Both definitions exist in committed generated output even on one OS.
    plist = service.source.with_suffix(".plist")
    for source in (plist, service.source.with_suffix(".service")):
        declared = metadata.get(str(source))
        observed = FileSnapshot.read(source)
        if (declared is None or observed.link is not None or observed.sha256 != declared["sha256"]
                or observed.mode != declared["mode"]):
            raise JobStopError("job cross-platform definition differs from committed policy")
    definition = plistlib.loads(plist.read_bytes())
    scheduled = any(key in definition for key in ("StartInterval", "StartCalendarInterval"))
    if ((name in {"plane-daemon", "plane-owner"} and scheduled)
            or (name not in {"plane-daemon", "plane-owner"}
                and (not scheduled or definition.get("KeepAlive", False)))):
        raise JobStopError("job native scheduling policy is unsupported")
    if definition.get("AbandonProcessGroup", False) is not False:
        raise JobStopError("job abandons children; scoped stop is unsupported")
    fields = {}
    section = ""
    for raw in service.source.with_suffix(".service").read_text().splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
        elif section == "[Service]" and "=" in line:
            key, value = line.split("=", 1)
            fields.setdefault(key, []).append(value)
    if (fields.get("KillMode", ["control-group"]) != ["control-group"]
            or fields.get("SendSIGKILL", ["yes"]) != ["yes"]
            or fields.get("KillSignal", ["SIGTERM"]) != ["SIGTERM"]):
        raise JobStopError("job native child-lifetime policy is unsupported")
    # Every selected member must be enrolled in committed configuration.
    if not all(metadata[str(m.source)]["enroll"] for m in members):
        raise JobStopError("job is dormant in committed configuration")
    return owner_socket


def _bind(current, name, fleet, adapter, pins):
    allowed = {"keepalive", "fleet-pulse", "log-rotation"} if fleet else {"plane-daemon", "plane-owner"}
    if name not in allowed or fleet is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", fleet):
        raise JobStopError("job or fleet selector is unsupported")
    adapter, manager, domain, directories, observed, metadata = _bind_git_job(
        current, name, adapter, stop=True, fleet=fleet)
    units = sorted(observed, key=lambda u: (u.declaration.source.suffix != ".timer", u.target))
    service = next(unit.declaration for unit in units if unit.declaration.service is None
                   and unit.declaration.source.suffix != ".timer")
    owner_socket = _shape(service, [unit.declaration for unit in units], metadata, manager, name, current)
    placement = current.snapshot["supervision"]
    saved = []
    for unit in units:
        properties = dict(unit.properties)
        if (len(unit.installed) != 1 or properties.get("LoadState") != "loaded"
                or properties.get("ActiveState") not in {"active", "inactive", "activating"}):
            raise JobStopError("job family is absent, partial or not loaded")
        file = Path(unit.installed[0].path)
        if file.parent != Path(placement["directory"]):
            raise JobStopError("job is outside committed placement")
        node = _pin_node(file, pins)
        if node[1] != unit.installed[0] or node[1].content != FileSnapshot.read(unit.declaration.source).content:
            raise JobStopError("installed job differs from committed generated definition")
        # Preflight exact enablement links for the ENTIRE family before stopping timer.
        links = []
        if manager == "Linux":
            for directory in ("timers.target.wants", "default.target.wants"):
                link = file.parent / directory / file.name
                if link.parent.exists() and link.parent.resolve() != link.parent:
                    raise JobStopError("job enablement directory is redirected")
                if link.exists() or link.is_symlink():
                    if not link.is_symlink() or link.resolve() != file:
                        raise JobStopError("job enablement link is foreign")
                    links.append(_pin_link(link, file, pins))
        saved.append((unit, file, node, links))
    return adapter, manager, domain, directories, saved, owner_socket


def stop_job(root, name, *, fleet=None, adapter=None):
    """Stop one supported complete family, retaining declarations and evidence."""
    with mutation_admission(root, identity=RuntimeIdentity.current()) as current:
        with job_effect_lock(root):
            return _stop(current, name, fleet, adapter or Adapter())


def _owner_listener(socket, pins):
    """Pin this listener's existing lease; never create a socket or lock."""
    from .plane.owner_server import _private_directory

    if socket.parent.resolve() != socket.parent:
        raise JobStopError("owner endpoint directory is redirected")
    parent = None
    if socket.parent.exists():
        parent = _private_directory(socket.parent)
        fd = os.open(socket.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        pins.callback(os.close, fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != parent:
            raise JobStopError("owner endpoint directory changed during observation")
    node = None
    if os.path.lexists(socket):
        info = socket.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise JobStopError("owner endpoint is foreign or not a private socket")
        node = info.st_dev, info.st_ino
    lock = socket.with_name(socket.name + ".lock")
    fd = None
    identity = None
    if os.path.lexists(lock):
        fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        pins.callback(os.close, fd)
        info = os.fstat(fd)
        identity = info.st_dev, info.st_ino
        _owner_lock_node(lock, fd, identity)
        raw = os.read(fd, 513)
        record = json.loads(raw) if raw else None
        if (raw and json.dumps(record).encode() != raw
                or record is not None and (
                    type(record) is not dict or set(record) != {"version", "parent", "socket"}
                    or type(record["version"]) is not int or record["version"] != 1
                    or record["parent"] != list(parent or ())
                    or any(not isinstance(record[key], list) or len(record[key]) != 2
                           or any(type(value) is not int or value < 0 for value in record[key])
                           for key in ("parent", "socket")))
                or node is not None and (record is None or record["socket"] != list(node))):
            raise JobStopError("owner listener lease does not bind its endpoint")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            # An exited or never-ready owner needs no restart to permit cleanup.
            fcntl.flock(fd, fcntl.LOCK_UN)
            assert_private_endpoint_quiet(socket, target="plane-owner")
    elif node is not None:
        raise JobStopError("owner endpoint has no listener lease")
    return socket, parent, node, lock, fd, identity


def _owner_lock_node(path, fd, identity):
    opened, current = os.fstat(fd), path.lstat()
    if (any(not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 512
            for info in (opened, current))
            or (opened.st_dev, opened.st_ino) != identity
            or (current.st_dev, current.st_ino) != identity):
        raise JobStopError("owner listener lease node changed or is not private")


def _check_owner_listener(witness, *, quiet=False):
    from .plane.owner_server import _private_directory

    socket, parent, node, lock, fd, identity = witness
    if ((parent is None and os.path.lexists(socket.parent))
            or parent is not None and (socket.parent.resolve() != socket.parent
                                      or _private_directory(socket.parent) != parent)):
        raise JobStopError("owner endpoint directory changed before cleanup")
    if fd is None:
        if os.path.lexists(lock):
            raise JobStopError("owner listener lease appeared during stop")
    else:
        _owner_lock_node(lock, fd, identity)
    if os.path.lexists(socket):
        info = socket.lstat()
        if (node is None or (info.st_dev, info.st_ino) != node
                or not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise JobStopError("owner endpoint changed during stop")
    if quiet:
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return assert_private_endpoint_quiet(socket, target="plane-owner")


def _stop(current, name, fleet, adapter):
    with ExitStack() as pins:
        return _stop_pinned(current, name, fleet, adapter, pins)


def _stop_pinned(current, name, fleet, adapter, pins):
    adapter, manager, domain, directories, saved, owner_socket = _bind(current, name, fleet, adapter, pins)
    outcomes = [{"target": unit.target, "outcome": "unattempted"} for unit, *_ in saved]
    data = {"name": name, "fleet": fleet, "revision": current.revision,
            "config_fingerprint": current.config_fingerprint, "members": outcomes}
    attempted = False
    recorder_fd = None
    recorder_lock = None
    recorder_node = None
    owner_listener = None
    try:
        # Capture whole-family witnesses before effects. Timer has no process group.
        witnesses = []
        for unit, file, node, links in saved:
            raw = adapter.read("svc_job_witness", str(file), unit.target).splitlines()
            if len(raw) != 2 or not raw[0].startswith("pid\t") or not raw[1].startswith("group\t"):
                raise JobStopError("job process observation is incomplete")
            pid = int(raw[0].split("\t")[1])
            group = raw[1].split("\t", 1)[1]
            if (manager == "Linux" and file.suffix == ".service"
                    and dict(unit.properties)["ActiveState"] in {"active", "activating"}
                    and (pid <= 1 or not group)):
                raise JobStopError("running job lacks native process/cgroup witnesses")
            witness = _capture_group(pid) if manager == "Darwin" and pid else None
            if name == "plane-daemon" and manager == "Darwin" and not pid:
                raise JobStopError("recorder has no live native process witness")
            witnesses.append((pid, group, witness))
            outcomes[len(witnesses)-1]["witnesses"] = {
                "native_pid": pid, "cgroup": group,
                "process_group": witness[0] if witness else None,
                "known_pids": sorted(witness[1]) if witness else [],
                "coverage": "committed non-abandoning native family and captured witnesses"}
        if name == "plane-owner":
            owner_listener = _owner_listener(owner_socket, pins)
        if name == "plane-daemon":
            from .plane.daemon import socket_path, lock_path, probe_daemon_info
            sock = socket_path(current.installation.data_root)
            info = probe_daemon_info(sock)
            pid = witnesses[0][0]
            expected = {"root": str(current.installation.data_root), "pid": pid,
                        "installation": str(current.installation.directory),
                        "source_revision": current.revision, "cli": str(current.installation.cli)}
            if not pid or info is None or any(info.get(k) != value for k, value in expected.items()):
                raise JobStopError("recorder endpoint does not bind the native process and installation")
            recorder_lock = lock_path(sock)
            recorder_node = _node(recorder_lock)
            recorder_fd = os.open(recorder_lock, os.O_RDWR | os.O_NOFOLLOW)
            opened = os.fstat(recorder_fd)
            if (opened.st_dev, opened.st_ino) != recorder_node[0]:
                raise JobStopError("recorder lifetime lock changed during observation")
            try:
                fcntl.flock(recorder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass  # The admitted daemon owns its lifetime lock until exit.
            else:
                raise JobStopError("recorder has no held lifetime lock")
        for index, ((unit, file, node, links), (pid, group, witness)) in enumerate(zip(saved, witnesses)):
            # Revalidate all not-yet-stopped family nodes before each native effect.
            for _, pending_file, pending_node, pending_links in saved[index:]:
                _check_node(pending_file, pending_node)
                _check_links(pending_links)
            if owner_listener is not None:
                _check_owner_listener(owner_listener)
            outcomes[index]["outcome"] = "unknown"
            attempted = True
            result = adapter.call("svc_job_stop_exact", str(unit.declaration.source), str(file), unit.target,
                                  str(pid), timeout=60)
            outcomes[index]["returncode"] = result.returncode
            if result.returncode or result.stdout != "effect-attempted\nstopped\n":
                outcomes[index]["stdout"] = result.stdout[:4000]
                outcomes[index]["stderr"] = result.stderr[:4000]
                if result.returncode and not result.stdout.startswith("effect-attempted\n"):
                    outcomes[index]["outcome"] = "unattempted"
                    attempted = any(row["outcome"] != "unattempted" for row in outcomes)
                raise JobStopError("job native stop was refused or is incomplete")
            assert_quiescent(adapter, installed_file=file, target=unit.target, known_cgroup=group, settle_s=10)
            _await_group_quiet(witness)
            if name == "plane-daemon":
                assert_private_endpoint_quiet(sock, target=unit.target)
                _check_node(recorder_lock, recorder_node)
                fcntl.flock(recorder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                outcomes[index]["recorder_endpoint_quiet"] = True
                outcomes[index]["recorder_lifetime_lock_released"] = True
            if owner_listener is not None:
                outcomes[index]["owner_endpoint_state"] = _check_owner_listener(owner_listener, quiet=True)
                outcomes[index]["owner_endpoint_quiet"] = True
                outcomes[index]["owner_lifetime_lock_released"] = True
            _check_node(file, node)
            for link, identity, value in links:
                # Native disable may remove the owned enablement link.
                if link.exists() or link.is_symlink():
                    if not _link_matches(link, identity, value):
                        raise JobStopError("job enablement node changed")
                    link.unlink()
            file.unlink()
            if manager == "Linux":
                reload = adapter.call("svc_job_reload", timeout=30)
                if reload.returncode or reload.stdout != "reloaded\n":
                    raise JobStopError("native reload is incomplete")
            assert_native_absent(adapter, manager=manager, domain=domain, target=unit.target,
                                 installed_file=file, search_directories=directories)
            _group_quiet(witness)
            outcomes[index]["outcome"] = "absent"
        return JobStop(name, fleet, current.revision, current.config_fingerprint, tuple(outcomes))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise JobStopError(str(exc), data=data, attempted=attempted) from exc
    finally:
        if recorder_fd is not None:
            os.close(recorder_fd)
