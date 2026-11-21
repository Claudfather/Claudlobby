"""Start a paired, linked computer with no team: its own services and timers only.

The card's scope is read from this computer's host jobs, with any planned
settings change applied, before anything is written: no record, render,
database or configuration exists until the card is approved. An approved start
prepares one first install that names no team files and dispatches its updater
job. ``start`` never waits for that job and never continues a record; a record
that stopped is continued with ``host update continue``.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from ..command_result import CommandFailure

WORD = "START-WORKSPACE"
ACTION = "start_workspace"


def _identifiers(record):
    return {
        "request_id": record["request_id"],
        "config_fingerprint": record.get("config_fingerprint"),
        "target": record["target"],
    }


def _stopped(record):
    """True when this unfinished start cannot finish without `host update continue`."""
    dispatch = record.get("job_dispatch")
    return (
        record.get("stage_error") is not None
        or (record["phase"] == "done" and bool(record.get("health_errors")))
        # Never dispatched, or its job ran and ended with the record unfinished.
        or dispatch is None
        or dispatch.get("state") == "finished"
    )


def _planned_jobs(owner_plan):
    """Host jobs with this computer's settings, or with the planned settings in their place."""
    from ..config import (
        _apply_host_override,
        _load_system_defaults,
        _read_host_override,
        host_override_path,
    )

    packaged = (_load_system_defaults().get("host") or {}).get("jobs") or {}
    if owner_plan is None:
        path = host_override_path()
        return _apply_host_override(packaged, path), _read_host_override(path)[1]
    with tempfile.TemporaryDirectory(prefix="claudlobby-start-") as directory:
        path = Path(directory) / "system.yaml"
        path.write_bytes(owner_plan["content"])
        return _apply_host_override(packaged, path), _read_host_override(path)[1]


def start_scope(paths, *, directory, owner_plan=None) -> dict:
    """What an approved start installs: read only, from the host jobs; no render, no record."""
    from ..config import host_unit_name
    from .db import db_file

    jobs, prefix = _planned_jobs(owner_plan)
    enrolled = {
        name: item
        for name, item in jobs.items()
        if item.get("enroll", True) is not False
    }
    scope = {
        "database": str(db_file(paths.root)),
        "native_directory": str(directory),
        "services": sorted(
            host_unit_name(name, prefix=prefix)
            for name, item in enrolled.items()
            if item.get("unit") == "service"
        ),
        "jobs": sorted(
            host_unit_name(name, prefix=prefix)
            for name, item in enrolled.items()
            if item.get("unit") != "service"
        ),
    }
    if owner_plan is not None:
        scope["settings"] = {
            "path": owner_plan["binding"]["override"],
            "sha256": owner_plan["binding"]["sha256"],
        }
    return scope


def _directory(paths, directory):
    if directory is not None:
        return Path(directory).expanduser()
    from ..commands.host_owner_setup import (
        _default_install_directory,
        _native_directories,
    )

    selected = _default_install_directory(_native_directories(paths))
    if selected is None:
        raise CommandFailure(
            "conflict",
            "no persistent user service directory is available on this computer",
        )
    return selected


def start(paths, store, terminal, *, consent=None, owner_plan=None, directory=None):
    """Return ("started", None), ("starting", identifiers) or ("failed", {"line", "request_id"})."""
    from ..commands.host_owner import _consent, _say
    from ..commands.host_owner_setup import _commit_owner_override
    from ..git_update import (
        UpdateStore,
        _prepare_update_locked,
        _unfinished_text,
        apply_or_continue,
        read_update,
    )
    from .owner_access import AccessUnavailable
    from .owner_source import start_state

    package = paths.package
    installation = package.installation
    if installation is None or installation.data_root != paths.root:
        raise CommandFailure(
            "conflict", "a start requires this computer's managed Git installation"
        )
    state = start_state(paths.root, installation)
    if state == "started":
        return "started", None
    if state == "starting":
        record = read_update(installation)
        if _stopped(record):
            return "failed", {
                "line": _unfinished_text(paths.root, record, None),
                "request_id": record["request_id"],
            }
        return "starting", _identifiers(record)
    if state != "fresh":
        raise CommandFailure(
            "conflict", "only a fresh computer can be started; nothing was changed"
        )
    try:
        owner = store.current_grant()
        workspace_state, _binding = store.workspace().status()
    except AccessUnavailable as exc:
        raise CommandFailure(
            "conflict", "a start requires an active owner pairing and linked workspace"
        ) from exc
    if owner is None or not owner.active or workspace_state != "active":
        raise CommandFailure(
            "conflict", "a start requires an active owner pairing and linked workspace"
        )
    selected = _directory(paths, directory)
    scope = start_scope(paths, directory=selected, owner_plan=owner_plan)
    _say(terminal, json.dumps(scope, ensure_ascii=True) + "\n")
    _consent(terminal, WORD, consent, ACTION, scope)
    if owner_plan is not None:
        _commit_owner_override(owner_plan)
    with UpdateStore(installation) as updates:
        record = _prepare_update_locked(
            updates,
            target=package.source_revision,
            initialization={"directory": str(selected), "manifests": []},
            prepare_only=True,
        )
    result = apply_or_continue(installation, expected_request_id=record["request_id"])
    return "starting", _identifiers(result["update"])
