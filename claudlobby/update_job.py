"""Fixed installation-owned native job for the independent updater.

Outer callers validate operator authority and prepare the current UpdateStore
operation first. Dispatch must run after releasing its process/runtime locks.
This job executes only the installed independent updater's private ``_run``
route; it neither imports mutable source nor joins the fleet consumer roster.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import tempfile

from .git_update import UpdateError, _sync, read_update
from .installation import read_installation, unit_systemd_command
from .supervision_inventory import (
    _catalog, _properties, _darwin_print, _darwin_binding, _darwin_disabled,
)


@dataclass(frozen=True)
class UpdateJob:
    manager: str
    target: str
    source: Path
    installed: Path
    content: bytes


def _plain(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise UpdateError("updater job paths must be absolute and unredirected")
    return path


def update_job_label(installation):
    """One deterministic name codec; a label alone never proves ownership."""
    return "com.claudlobby.update." + hashlib.sha256(str(installation.directory).encode()).hexdigest()[:16]


def update_job_definition(installation, *, manager, domain, install_directory, native_target=None):
    """Deterministic definition, independent of active_config and Git revision."""
    current = read_installation(installation.directory)
    if (current.directory, current.data_root) != (installation.directory, installation.data_root):
        raise UpdateError("updater job installation record differs")
    # Copied recovery support loads the canonical dataclass in a separate
    # module. Exact recorded paths bind it; nominal Python class identity does not.
    installation = current
    installed_directory = _plain(install_directory)
    if not installed_directory.is_dir() or installed_directory.stat().st_uid != os.getuid():
        raise UpdateError("updater native directory is not owned by the current user")
    executable = _plain(installation.bin / "claudlobby-update")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise UpdateError("independent installed updater is unavailable")
    label = update_job_label(installation)
    if native_target is not None:
        allowed = ({label + ".service"} if manager == "Linux" else
                   {f"gui/{os.getuid()}/{label}", f"user/{os.getuid()}/{label}"} if manager == "Darwin" else set())
        if not isinstance(native_target, str) or native_target not in allowed:
            raise UpdateError("updater exact native target differs from its installation or current user")
        if manager == "Darwin":
            domain = native_target.rsplit("/", 1)[0]
    # The private route derives data_root from installation.json. No mutable
    # runtime arguments, environment, source paths or per-operation unit name.
    argv = [str(executable), "_run"]
    if manager == "Linux":
        name, target = label + ".service", label + ".service"
        directory = str(installation.directory)
        if any(character in directory for character in "\n\r\0"):
            raise UpdateError("updater directory has invalid native characters")
        working_directory = '"' + directory.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
        content = ("[Unit]\nDescription=Claudlobby independent update\n[Service]\nType=exec\n"
                   + "WorkingDirectory=" + working_directory + "\n"
                   + "ExecStart=" + unit_systemd_command(argv) + "\n"
                   + "Restart=no\nKillMode=control-group\nTimeoutStopSec=30\n").encode()
    elif manager == "Darwin":
        if domain not in {f"gui/{os.getuid()}", f"user/{os.getuid()}"}:
            raise UpdateError("updater job launchd domain differs from current user")
        name, target = label + ".plist", domain + "/" + label
        content = plistlib.dumps({"Label": label, "ProgramArguments": argv,
            "WorkingDirectory": str(installation.directory), "RunAtLoad": False, "KeepAlive": False}, sort_keys=True)
    else:
        raise UpdateError("unsupported updater native manager")
    return UpdateJob(manager, target, installation.bin / name, installed_directory / name, content)


def _definition(installation, adapter, install_directory, *, native_target=None):
    manager, domain, directories, names, loaded = _catalog(adapter.read("svc_inventory_catalog"))
    if Path(install_directory) not in directories:
        raise UpdateError("updater native directory is absent from the observed search path")
    job = update_job_definition(installation, manager=manager, domain=domain, install_directory=install_directory,
                                native_target=native_target)
    if not job.installed.exists() and job.installed.name in loaded:
        raise UpdateError("loaded updater name has no owned installed definition")
    if manager == "Darwin":
        disabled = _darwin_disabled(adapter.read("svc_inventory_disabled", job.target.rsplit("/", 1)[0]))
        if disabled.get(job.installed.stem) == "disabled":
            raise UpdateError("updater job has an explicit disabled native override")
    for directory in set(directories):
        other = directory / job.installed.name
        if other != job.installed and (other.exists() or other.is_symlink()):
            raise UpdateError("another native definition shadows the updater job")
    return job


def _check_file(path, content):
    if (path.is_symlink() or not path.is_file() or path.read_bytes() != content
            or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o777 != 0o644):
        raise UpdateError("updater native definition differs from the fixed installation job")


def _publish_missing(path, content):
    _plain(path.parent)
    if path.exists() or path.is_symlink():
        _check_file(path, content)
        return
    fd, name = tempfile.mkstemp(prefix=".update-job-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)  # create-only; never replace a foreign node
        except FileExistsError:
            _check_file(path, content)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _check_linux_exec(text, executable):
    """Validate typed ExecStart, preserving actual argument boundaries.

    systemctl show joins argv with spaces, so that projection cannot establish
    exact argv. busctl get-property JSON preserves the D-Bus a(sasbttttuii)
    structure: systemd v252 src/busctl/busctl.c get_property and
    json_transform_variant. See official busctl(1), --json/get-property.
    This is a schema floor, not a claim of native acceptance on either host.
    """
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate native JSON field")
            result[key] = value
        return result
    try:
        payload = json.loads(text, object_pairs_hook=object_pairs)
        if (type(payload) is not dict or set(payload) != {"type", "data"}
                or payload["type"] != "a(sasbttttuii)" or type(payload["data"]) is not list
                or len(payload["data"]) != 1):
            raise ValueError()
        command = payload["data"][0]
        if (type(command) is not list or len(command) != 10
                or command[0] != str(executable) or command[1] != [str(executable), "_run"]
                or command[2] is not False
                or any(type(value) is not int or not 0 <= value < 2**64 for value in command[3:7])
                or type(command[7]) is not int or not 0 <= command[7] < 2**32
                or any(type(value) is not int or not -(2**31) <= value < 2**31 for value in command[8:])):
            raise ValueError()
    except (ValueError, TypeError, KeyError) as exc:
        raise UpdateError("updater effective executable or argv differs or is unverified") from exc


def _observe(job, installation, adapter, *, observed_properties=None):
    _check_file(job.source, job.content)
    _check_file(job.installed, job.content)
    props = _properties(adapter.read("svc_inventory_properties", job.target)) if job.manager == "Linux" else None
    if props is not None and props["LoadState"] == "not-found":
        if (props["Id"] != job.target or props["ActiveState"] != "inactive" or props["FragmentPath"]
                or props["DropInPaths"] or props["WorkingDirectory"] or props["Environment"] or props["ExecStart"]):
            raise UpdateError("absent updater native binding is unknown")
        return "unloaded"
    if observed_properties is None:
        state = adapter.read("svc_inventory_state", job.installed, job.target).strip().split()
    else:
        # Inventory may positively observe a same-UID launchd job outside the
        # caller's control domain. This proves only its exact loaded binding;
        # control callers still require the native state/control-domain check.
        if job.manager != "Darwin":
            raise UpdateError("print observation requires launchd")
        state = ["unchanged", "loaded", observed_properties["ActiveState"]]
    if len(state) != 3:
        raise UpdateError("updater native state is unknown")
    if job.manager == "Linux":
        if (props["Id"] != job.target or props["LoadState"] != "loaded"
                or props["FragmentPath"] != str(job.installed) or props["DropInPaths"]
                or props["NeedDaemonReload"] != "no" or props["Triggers"] or props["TriggeredBy"]
                or props["WorkingDirectory"] != str(installation.directory) or props["Environment"]
                or state[1] != "loaded" or props["ActiveState"] != state[2]):
            raise UpdateError("updater effective native definition differs")
        _check_linux_exec(adapter.read("svc_update_job_exec", job.installed, job.target),
                          installation.bin / "claudlobby-update")
    else:
        disabled = _darwin_disabled(adapter.read("svc_inventory_disabled", job.target.rsplit("/", 1)[0]))
        props = observed_properties if observed_properties is not None else (
            _darwin_print(adapter.read("svc_inventory_properties", job.target), job.target) if state[1] == "loaded" else {})
        bound = _darwin_binding(job.installed.name, job.content, str(job.installed),
            str(installation.directory), {}, props, state[1] == "loaded", disabled)
        if bound["EnabledState"] != "enabled" or bound["ActiveState"] != state[2]:
            raise UpdateError("updater launchd state or disabling differs")
    if state[1] == "unloaded" and state[2] == "inactive":
        return "unloaded"
    if state[1] != "loaded" or state[2] not in {"active", "activating", "inactive", "failed"}:
        raise UpdateError("updater native state is unknown")
    return {"active": "running", "activating": "starting", "inactive": "inactive", "failed": "failed"}[state[2]]


def observe_update_job(installation, adapter, install_directory, *, native_target=None):
    """Fresh exact state; an unreadable/refused observation never means inactive."""
    job = _definition(installation, adapter, install_directory, native_target=native_target)
    return {"target": job.target, "state": _observe(job, installation, adapter)}


def _native(adapter, operation, job):
    result = adapter.call(operation, job.installed, job.target, timeout=30)
    if result.returncode:
        raise UpdateError("updater native request refused or outcome unknown; reobserve the same job")
    return result.stdout.strip()


def prepare_update_job(installation, adapter, install_directory):
    """Create exact missing definitions and enroll without starting/bouncing."""
    job = _definition(installation, adapter, install_directory)
    if job.source.exists() and job.installed.exists():
        state = _observe(job, installation, adapter)
        if state in {"running", "starting", "inactive", "failed"}:
            return {"target": job.target, "state": state}
    _publish_missing(job.source, job.content)
    _publish_missing(job.installed, job.content)
    # No enable, disable, bootout, restart or kickstart belongs to enrollment.
    outcome = _native(adapter, "svc_update_job_enroll_exact", job)
    if outcome not in {"enrolled", "already-enrolled", "already-running"}:
        raise UpdateError("updater enrollment was not acknowledged")
    return {"target": job.target, "state": _observe(job, installation, adapter)}


def run_update_job(installation, adapter, install_directory, *, native_target=None):
    """Start/resume only the current prepared operation; attach if already active."""
    record = read_update(installation)
    if (record is None or record["phase"] == "prepare" or not record.get("finalized", True)
            or record["phase"] == "done" and not record.get("health_errors")):
        raise UpdateError("no prepared update requires the independent job")
    job = _definition(installation, adapter, install_directory, native_target=native_target)
    state = _observe(job, installation, adapter)
    if state in {"running", "starting"}:
        return {"request_id": record["request_id"], "target": job.target, "state": state}
    if state not in {"inactive", "failed"}:
        raise UpdateError("updater job is not enrolled and inactive")
    outcome = _native(adapter, "svc_update_job_run_exact", job)
    if outcome not in {"run-requested", "already-running"}:
        raise UpdateError("updater native launch was not acknowledged")
    return {"request_id": record["request_id"], "target": job.target,
            "state": _observe(job, installation, adapter), "native_acknowledgement": outcome}


def bind_update_job(store, adapter, install_directory):
    """Retain exact independent native placement in the same operation record."""
    store._assert_locked()
    record = store.load()
    if record is None or record["phase"] not in {"prepare", "quiesce"}:
        raise UpdateError("updater job binding requires the current prepared operation")
    job = _definition(store.installation, adapter, install_directory)
    _observe(job, store.installation, adapter)
    binding = {"manager": job.manager, "target": job.target, "source": str(job.source),
        "installed": str(job.installed), "sha256": hashlib.sha256(job.content).hexdigest(),
        "content_base64": base64.b64encode(job.content).decode("ascii")}
    if record.get("updater_job") not in (None, binding):
        raise UpdateError("independent updater native binding changed")
    record["updater_job"] = binding
    store.save(record)
    return binding


def verify_update_job_runner(installation, adapter, install_directory, *, native_target=None):
    """Require actual native job membership; argv/environment never authorize it.

    The copied stdlib runner uses the same retained definition and canonical
    supervisor ancestry operation when mutable source/environment are absent.
    Return code one means this exact job hosts the caller, not native failure.
    """
    job = _definition(installation, adapter, install_directory, native_target=native_target)
    if _observe(job, installation, adapter) not in {"running", "starting"}:
        raise UpdateError("independent updater is not running in its native job")
    result = adapter.call("svc_activation_assert_external", job.installed, job.target,
                          str(os.getpid()), timeout=30)
    if result.returncode != 1:
        raise UpdateError("updater caller is external to its exact native job or membership is unknown")
    return {"target": job.target, "native_member": True}
