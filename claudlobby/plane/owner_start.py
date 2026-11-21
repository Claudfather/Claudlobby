"""Start a paired, linked computer with no team: its own services and timers only.

The card's scope is read from this computer's host jobs, with any planned
settings change applied, before anything is written: no record, render,
database or configuration exists until the card is approved. An approved start
prepares one first install that names no team files and dispatches its updater
job. ``start`` never waits for that job and never continues a record; a record
that stopped is continued with ``host update continue``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from ..command_result import CommandFailure

WORD = "START-WORKSPACE"
ACTION = "start_workspace"
HINT = "inspect the original operation with host update status; resume it with host update continue"


def _identifiers(record):
    return {
        "request_id": record["request_id"],
        "config_fingerprint": record.get("config_fingerprint"),
        "target": record["target"],
    }


def _failed(root, record):
    from ..git_update import _unfinished_text

    return "failed", {"line": _unfinished_text(root, record, None), "request_id": record["request_id"]}


def _progress(root, installation):
    """`starting` or `failed` for an unfinished start record, by core's own reads. Writes nothing.

    A held update lock is a coordinator or the updater job at work. With the
    lock free, the record and a read-only look at its native job decide. A start
    that finished cleanly since the unlocked read is `started`; a record gone
    since then reads as `start_state` now reads.
    """
    from ..git_update import UpdateBusyError, UpdateError, UpdateStore, _retained_job, read_update
    from .owner_source import start_state

    try:
        with UpdateStore(installation) as updates:
            record = updates.load()
            if record is None:
                return start_state(root, installation), None
            if record["phase"] == "done" and not record.get("health_errors"):
                return "started", None
            if (record.get("stage_error") is not None
                    or (record["phase"] == "done" and record.get("health_errors"))):
                return _failed(root, record)
            dispatch, binding = record.get("job_dispatch"), record.get("updater_job")
            if dispatch is None or dispatch.get("state") == "finished" or binding is None:
                return _failed(root, record)
            try:
                jobs, independent, adapter = _retained_job(updates)
                observed = jobs.observe_update_job(independent, adapter, Path(record["native_install_directory"]),
                                                   native_target=binding["target"])
            except (UpdateError, OSError, ValueError, RuntimeError, KeyError):
                return _failed(root, record)
            if observed.get("state") in {"running", "starting"}:
                return "starting", _identifiers(record)
            return _failed(root, record)
    except UpdateBusyError:
        return "starting", _identifiers(read_update(installation))


def _digest(path):
    """The settings file's sha256, or "absent"."""
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "absent"


def _planned_jobs(owner_plan):
    """Host jobs with this computer's settings, or with the planned settings in their place."""
    from ..config import _apply_host_override, _load_system_defaults, _read_host_override, host_override_path

    packaged = (_load_system_defaults().get("host") or {}).get("jobs") or {}
    if owner_plan is None:
        path = host_override_path()
        return _apply_host_override(packaged, path), _read_host_override(path)[1]
    with tempfile.TemporaryDirectory(prefix="claudlobby-start-") as directory:
        path = Path(directory) / "system.yaml"
        path.write_bytes(owner_plan["content"])
        return _apply_host_override(packaged, path), _read_host_override(path)[1]


def _catalog(paths):
    from ..supervision_inventory import Adapter, _catalog

    manager, _domain, directories, _names, _loaded = _catalog(Adapter(paths.package).read("svc_inventory_catalog"))
    return manager, tuple(directories)


def start_scope(paths, *, directory, manager, owner_plan=None) -> dict:
    """Everything an approved start installs, read without rendering or writing a record.

    Services and jobs use the composer's own rule for which host jobs compose a
    unit. The updater's job unit and the installation's programs use the names
    the installers use.
    """
    from ..composer import _host_job_armed
    from ..config import host_override_path, host_unit_name
    from ..update_job import update_job_label
    from .db import db_file

    installation = paths.package.installation
    jobs, prefix = _planned_jobs(owner_plan)
    armed = {name: item for name, item in jobs.items() if _host_job_armed(name, item)}
    suffix = {"Linux": ".service", "Darwin": ".plist"}.get(manager)
    if suffix is None:
        raise CommandFailure("conflict", "a start requires a Linux or macOS user service manager")
    job = update_job_label(installation) + suffix
    return {
        "database": str(db_file(paths.root)),
        "native_directory": str(directory),
        "native_directory_created": not os.path.lexists(directory),
        "services": sorted(host_unit_name(name, prefix=prefix)
                           for name, item in armed.items() if item.get("unit") == "service"),
        "jobs": sorted(host_unit_name(name, prefix=prefix)
                       for name, item in armed.items() if item.get("unit") != "service"),
        "updater_job": str(Path(directory) / job),
        "installation_files": [str(installation.cli), str(installation.bin / "claudlobby-update"),
                               str(installation.bin / job)],
        "settings": ({"path": owner_plan["binding"]["override"], "sha256": owner_plan["binding"]["sha256"]}
                     if owner_plan is not None else
                     {"path": str(host_override_path()), "sha256": _digest(host_override_path())}),
    }


def _directory(directory, directories):
    """A native directory the user manager searches: a real directory, or absent.

    An absent one is normal on a fresh account; the start creates it after approval.
    """
    if directory is None:
        from ..commands.host_owner_setup import _default_install_directory

        directory = _default_install_directory(directories)
        if directory is None:
            raise CommandFailure("conflict", "no persistent user service directory is available on this computer")
    directory = Path(directory).expanduser()
    if directory not in directories:
        raise CommandFailure("conflict", "the native directory is not one this computer's service manager reads")
    if os.path.lexists(directory) and (directory.is_symlink() or not directory.is_dir()):
        raise CommandFailure("conflict", "the native directory is not a real directory; nothing was changed")
    return directory


def _admit(store, owner_plan):
    """Paired, linked, no guided setup retained, and any planned settings for this binding."""
    from ..config import host_override_path
    from .owner_access import AccessDenied, AccessUnavailable
    from .owner_setup import OwnerSetup

    refused = "a start requires an active owner pairing and linked workspace"
    try:
        owner = store.current_grant()
        workspace_state, binding = store.workspace().status()
        retained = OwnerSetup(store).retained_state()
    except (AccessUnavailable, AccessDenied) as exc:
        raise CommandFailure("conflict", refused) from exc
    if owner is None or not owner.active or workspace_state != "active" or binding is None:
        raise CommandFailure("conflict", refused)
    if retained.state != "absent":
        raise CommandFailure("conflict", "a guided first-team setup is retained on this computer; nothing was changed",
                             data={"state": retained.state})
    if owner_plan is not None:
        planned = owner_plan["binding"]
        if Path(planned["override"]) != host_override_path():
            raise CommandFailure("conflict", "the planned settings are not this computer's settings file")
        if (binding.issuer != planned["config"].get("workspace_issuer")
                or binding.operational_origin != planned["config"].get("workspace_origin")):
            raise CommandFailure("conflict", "the planned settings name a different workspace than the one linked")


def _approved(paths, store, installation, *, owner_plan, selected, scope, done):
    """Under one update lock: admit again, refuse any record, settle settings, prepare, dispatch.

    `done` records how far this call got, for the caller's report of a failure.
    """
    from ..commands.host_owner_setup import _commit_owner_override
    from ..git_update import UpdateBusyError, UpdateStore, _dispatch_locked, _prepare_update_locked

    try:
        updates = UpdateStore(installation).__enter__()
    except UpdateBusyError as exc:
        raise CommandFailure("conflict", "another update is running; nothing was changed") from exc
    try:
        _admit(store, owner_plan)
        if updates.load() is not None:
            raise CommandFailure("conflict", "a start refuses an existing update operation", hint=HINT)
        if owner_plan is None and _digest(scope["settings"]["path"]) != scope["settings"]["sha256"]:
            raise CommandFailure("conflict", "this computer's settings changed after approval; nothing was started")
        selected.mkdir(parents=True, exist_ok=True)
        if selected.is_symlink() or not selected.is_dir():
            raise CommandFailure("conflict", "the native directory is not a real directory; nothing was started")
        if owner_plan is not None:
            _commit_owner_override(owner_plan)
            done["committed"] = True
        record = _prepare_update_locked(updates, target=paths.package.source_revision,
                                        initialization={"directory": str(selected), "manifests": []},
                                        prepare_only=True)
        done["request_id"] = record["request_id"]
        return _dispatch_locked(updates, record)[1]
    finally:
        updates.__exit__(None, None, None)


def _after_approval(installation, done, exc):
    """The failure of an approved start, said as it stands: a record, saved settings, or nothing."""
    from ..git_update import read_update

    request_id, readable = done["request_id"], True
    if request_id is None:
        try:
            current = read_update(installation)
        except Exception:
            current, readable = None, False
        request_id = current["request_id"] if current is not None else None
    if request_id is not None or not readable:
        return CommandFailure("conflict", "the start did not complete; retained state requires inspection",
                              data={"request_id": request_id} if request_id is not None else {}, hint=HINT)
    if done["committed"]:
        return CommandFailure("conflict", "this computer's settings were saved and nothing was started; "
                                          "build the settings plan again and call again")
    return CommandFailure("conflict", "the start did not complete; nothing was started")


def start(paths, store, terminal, *, consent=None, owner_plan=None, directory=None):
    """Return ("started", None), ("starting", identifiers) or ("failed", {"line", "request_id"}).

    A start record that vanished while this call read it returns what
    `start_state` then says, with None. After a failure that follows the
    settings commit, rebuild `owner_plan` from the current settings file before
    calling again: the old plan object is refused as changed.
    """
    from ..commands.host_owner import _consent, _say
    from ..git_update import _run_dispatched, read_update
    from .owner_source import start_state

    package = paths.package
    installation = package.installation
    if installation is None or installation.data_root != paths.root:
        raise CommandFailure("conflict", "a start requires this computer's managed Git installation")
    state = start_state(paths.root, installation)
    if state == "started":
        return "started", None
    if state == "starting":
        return _progress(paths.root, installation)
    if state != "fresh":
        raise CommandFailure("conflict", "only a fresh computer can be started; nothing was changed")
    _admit(store, owner_plan)
    manager, directories = _catalog(paths)
    selected = _directory(directory, directories)
    scope = start_scope(paths, directory=selected, manager=manager, owner_plan=owner_plan)
    _say(terminal, json.dumps(scope, ensure_ascii=True) + "\n")
    _consent(terminal, WORD, consent, ACTION, scope)
    done = {"request_id": None, "committed": False}
    try:
        dispatch = _approved(paths, store, installation, owner_plan=owner_plan, selected=selected,
                             scope=scope, done=done)
        if dispatch is not None:
            _run_dispatched(installation, dispatch, done["request_id"])
    except CommandFailure:
        raise
    except Exception as exc:
        raise _after_approval(installation, done, exc) from exc
    return "starting", _identifiers(read_update(installation))
