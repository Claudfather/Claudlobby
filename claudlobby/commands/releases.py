"""Public preparation/diagnostic adapters; lifecycle effects stay in their owners.

Keep module imports stdlib-only so host releases remains available when optional
application dependencies are broken. No command here selects or starts a release.
SQLite read-only queries write no SQL/schema state but may create SQLite's empty
WAL and SHM index sidecars; they do not certify a quiesced filesystem snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys

from ..command_result import CommandFailure, CommandOutput


def _host_root(args) -> Path:
    if args.fleet is not None or args.seed:
        raise CommandFailure("invalid_argument", "invalid argument: host preparation rejects --fleet and --seed")
    value = args.root if args.root is not None else os.environ.get("CLAUDLOBBY_ROOT")
    if not value:
        raise CommandFailure("invalid_argument", "invalid argument: an explicit host data root is required",
                             hint="supply --root PATH or CLAUDLOBBY_ROOT")
    try:
        root = Path(value).expanduser().resolve()
    except (ValueError, RuntimeError) as exc:
        raise CommandFailure("invalid_argument", "invalid argument: invalid host data root") from exc
    if not root.is_dir():
        raise CommandFailure("not_found", f"data root not found: {root}")
    if (root / "state").resolve() != root / "state":
        raise CommandFailure("conflict", "conflict: host state directory is redirected")
    return root


def _executing_release(root: Path) -> str | None:
    from ..releases import ReleaseError, read_release
    from ..resources import selected_cli

    try:
        cli = selected_cli()
    except RuntimeError:
        return None
    store = root / "state/releases"
    if not cli.is_relative_to(store):
        return None
    candidate = cli.relative_to(store).parts[0]
    try:
        release = read_release(root, candidate, verify_files=False)
    except ReleaseError:
        return None  # Diagnosis must work even if this executable has no valid seal.
    return release.release_id if release.cli_path == cli else None


def _release(root: Path, release_id: str):
    from ..releases import ReleaseError, read_release, release_path

    if not re.fullmatch(r"r-[0-9a-f]{64}", release_id):
        raise CommandFailure("invalid_argument", "invalid argument: invalid release ID")
    try:
        return read_release(root, release_id)
    except ReleaseError as exc:
        try:
            absent = not release_path(root, release_id).exists()
        except ReleaseError:
            absent = False
        hint = f"inspect claudlobby --root {shlex.quote(str(root))} host releases"
        if absent:
            raise CommandFailure("not_found", f"release not found: {release_id}", hint=hint) from exc
        raise CommandFailure("conflict", "conflict: release is incomplete or changed", hint=hint) from exc


def _selection(root: Path):
    from ..activation_state import ActivationError, read_selection

    try:
        selected = read_selection(root)
        if selected and not all(isinstance(selected[key], str)
                                for key in ("activation_id", "release_id", "plan_id")):
            raise ActivationError("invalid selection identity")
        return selected
    except ActivationError as exc:
        raise CommandFailure("conflict", "conflict: host release selection cannot be verified") from exc


def _host_releases(args, root: Path) -> CommandOutput:
    from ..activation_state import ActivationError, read_activation
    from ..releases import ReleaseError, read_release

    store = root / "state/releases"
    if store.resolve() != store:
        raise CommandFailure("conflict", "conflict: release store is redirected")
    selection_error = None
    try:
        selected = _selection(root)
    except CommandFailure:
        selected = None
        selection_error = "host release selection cannot be verified"
    items, failed = [], selection_error is not None
    for entry in sorted(store.iterdir()) if store.exists() else ():
        if not entry.name.startswith("r-"):
            continue
        try:
            release = read_release(root, entry.name)
            items.append({"release_id": release.release_id, "verification": "verified",
                          "source_revision": release.inputs.source_revision,
                          "artifact_id": release.inputs.artifact_id,
                          "seal_sha256": release.seal_sha256, "runtime_sha256": release.runtime_sha256,
                          "versions": release.compatibility.to_dict(), "cli": str(release.cli_path),
                          "native": str(release.native_path), "error": None})
        except ReleaseError:
            failed = True
            items.append({"release_id": entry.name, "verification": "failed",
                          "source_revision": None, "artifact_id": None, "seal_sha256": None,
                          "runtime_sha256": None, "versions": None, "cli": None, "native": None,
                          "error": {"code": "conflict", "message": "release is incomplete or changed",
                                    "retryable": False, "hint": None}})
    activations, activation_errors = {}, []
    activation_dir = root / "state/activations"
    try:
        if activation_dir.resolve() != activation_dir:
            raise OSError("redirected activation store")
        names = {path.name for path in activation_dir.iterdir() if path.is_dir()} if activation_dir.exists() else set()
    except OSError:
        names = set()
        activation_errors.append({"activation_id": None, "error": "activation store cannot be read"})
    if selected:
        names.add(selected["activation_id"])
    for name in sorted(names):
        try:
            record = read_activation(root, name)
            if (not isinstance(record.status, str)
                    or record.body["pending"] is not None and not isinstance(record.body["pending"], str)):
                raise ActivationError("invalid activation status")
            activations[name] = {"activation_id": name, "status": record.status,
                                 "pending_step": record.body["pending"], "verification": "verified"}
            if selected and name == selected["activation_id"] and any(
                    record.body["intent"][key] != selected[key] for key in ("release_id", "plan_id")):
                selection_error = "selection does not match its activation intent"
                failed = True
        except (ActivationError, KeyError, TypeError):
            activations[name] = {"activation_id": name, "status": None,
                                 "pending_step": None, "verification": "failed"}
            activation_errors.append({"activation_id": name, "error": "activation record cannot be verified"})
    unfinished = [record for record in activations.values()
                  if record["verification"] == "verified" and record["status"] not in {"active", "rolled_back"}]
    selected_activation = activations.get(selected["activation_id"]) if selected else None
    data = {"root": str(root), "items": items, "next_cursor": None, "selection": selected,
            "selected_activation": selected_activation, "unfinished_activations": unfinished,
            "activation_errors": activation_errors}
    if selected and selected["release_id"] not in {item["release_id"] for item in items}:
        failed = True
        data["selection_error"] = "selected release is absent from the release store"
    else:
        data["selection_error"] = selection_error
    if selected_activation and selected_activation["status"] != "active":
        failed = True
        data["selection_error"] = "selected activation is not verified active"
    if unfinished or activation_errors:
        failed = True
    executing = _executing_release(root)
    selection_line = (f"Selected activation {selected_activation['activation_id']}: "
                      f"{selected_activation['status'] or 'unverified'}."
                      if selected_activation else "No verified selected activation.")
    if failed:
        detail = ("unfinished activations: " + ", ".join(
            f"{item['activation_id']} ({item['status']}, pending {item['pending_step'] or 'none'})"
            for item in unfinished)) if unfinished else "release or activation state cannot be verified"
        raise CommandFailure("conflict", f"conflict: {detail}; {selection_line}",
                             data=data, release_id=executing)
    lines = tuple(f"{item['release_id']}  {item['verification']}" for item in items)
    return CommandOutput(data, executing, (lines or ("No installed releases.",)) + (selection_line,))


def _changes(plan) -> list[dict]:
    def digest(state):
        return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    return [{"path": change.target,
             "before": {"kind": change.before["node"]["kind"], "state_sha256": digest(change.before)},
             "after": {"kind": change.after["kind"], "state_sha256": digest(change.after)}}
            for change in plan.changes]


def _plan_data(plan) -> dict:
    return {"plan_id": plan.plan_id, "release_id": plan.release_id, "release_seal": plan.release_seal,
            "fleets": list(plan.fleets), "changes": _changes(plan),
            "effects": {"restart_bots": plan.effects.get("restart_bots", []),
                        "reload_supervision": plan.effects.get("reload_supervision", False),
                        "coverage": plan.effects.get("coverage")}}


def _config_plan(args, root: Path) -> CommandOutput:
    from ..config_plan import PlanError
    from ..config_staging import stage_configuration
    from ..paths import Paths, _iter_fleet_dirs
    from ..resources import get_resources

    release = _release(root, args.release)
    try:
        package = get_resources()
    except RuntimeError as exc:
        raise CommandFailure("unavailable", "unavailable: installed package resources") from exc
    directories = ([root] if (root / "fleet.yaml").is_file() else [])
    directories.extend(path for path in _iter_fleet_dirs(root / "local") if (path / "fleet.yaml").is_file())
    for selected in args.fleet_path:
        path = Path(selected).expanduser().resolve()
        directory = path.parent if path.name == "fleet.yaml" else path
        if not (directory / "fleet.yaml").is_file():
            raise CommandFailure("not_found", f"fleet declaration not found: {directory / 'fleet.yaml'}")
        directories.append(directory)
    paths = [Paths(root, package=package, fleet_dir=None if directory == root else directory)
             for directory in directories]
    try:
        plan = stage_configuration(paths, release,
                                   log=lambda _: print("configuration warning reported by the validator",
                                                       file=sys.stderr))
    except (PlanError, ValueError) as exc:
        # Validation exceptions can embed secret authored values. The diagnostic
        # command exposes metadata only, including on failures.
        raise CommandFailure("conflict", "conflict: configuration could not be staged from these inputs",
                             hint=f"use the sealed candidate CLI {release.cli_path} and inspect fleet declarations") from exc
    return CommandOutput(_plan_data(plan), _executing_release(root),
                         (f"Configuration plan {plan.plan_id}: {len(plan.changes)} proposed paths.",))


def _config_diff(args, root: Path) -> CommandOutput:
    from ..config_plan import PlanError, read_plan

    if not re.fullmatch(r"p-[0-9a-f]{64}", args.plan_id):
        raise CommandFailure("invalid_argument", "invalid argument: invalid configuration plan ID")
    try:
        plan = read_plan(root, args.plan_id)
    except PlanError as exc:
        if not (root / "state/config-plans" / args.plan_id).exists():
            raise CommandFailure("not_found", f"configuration plan not found: {args.plan_id}") from exc
        raise CommandFailure("conflict", "conflict: configuration plan is incomplete or changed") from exc
    data = _plan_data(plan)
    return CommandOutput(data, _executing_release(root), tuple(
        f"{item['path']}: {item['before']['kind']} -> {item['after']['kind']} "
        f"{item['before']['state_sha256']} -> {item['after']['state_sha256']}" for item in data["changes"]))


def _migration_plan(args, root: Path) -> CommandOutput:
    from ..migration_plan import build_migration_manifest

    source, target = _release(root, args.source_release), _release(root, args.target_release)
    try:
        manifest = build_migration_manifest(root, source, target, initialize_empty=args.initialize_empty)
    except ValueError as exc:
        raise CommandFailure("conflict", "conflict: migration inputs changed or cannot be verified") from exc
    data = {"manifest_id": manifest.manifest_id, "manifest": manifest.payload()}
    executing = _executing_release(root)
    if manifest.blockers:
        raise CommandFailure("conflict", "conflict: migration preview has blockers", data=data,
                             release_id=executing, hint="resolve the named blockers and repeat migration plan under quiescence")
    return CommandOutput(data, executing, (f"Migration preview {manifest.manifest_id}; repeat under quiescence before apply.",))


def _migration_status(args, root: Path) -> CommandOutput:
    import sqlite3

    from ..activation_state import ActivationError, read_activation
    from ..migration_apply import read_migration
    from ..plane.db import connect_ro, db_file
    from ..runtime_versions import SQL_SCHEMA_VERSION

    database = {"path": str(db_file(root)), "state": "absent", "user_version": None}
    blockers, items = [], []
    try:
        conn = connect_ro(db_file(root))
        try:
            database.update(state="ok", user_version=conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except FileNotFoundError:
        blockers.append("database initialization has not been applied")
    except (OSError, sqlite3.Error):
        database["state"] = "unreadable"
        blockers.append("current database version is unavailable")
    if database["state"] == "ok" and database["user_version"] != SQL_SCHEMA_VERSION:
        blockers.append(f"current CLI requires SQL schema {SQL_SCHEMA_VERSION}")
    directory = root / "state/activations"
    if directory.resolve() != directory:
        raise CommandFailure("conflict", "conflict: activation store is redirected")
    if args.activation:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", args.activation):
            raise CommandFailure("invalid_argument", "invalid argument: invalid activation ID")
        if not (directory / args.activation / "activation.json").exists():
            raise CommandFailure("not_found", f"activation not found: {args.activation}")
        names = [args.activation]
    else:
        names = [p.name for p in sorted(directory.iterdir()) if p.is_dir()] if directory.exists() else []
    for name in names:
        try:
            activation = read_activation(root, name)
            migration = read_migration(root, name)
        except (ActivationError, ValueError, RuntimeError):
            blockers.append(f"activation migration evidence cannot be verified: {name}")
            items.append({"activation_id": name, "verification": "failed", "recorded": None,
                          "activation_status": None, "pending_step": None, "migration_step_completed": None})
            continue
        recorded = None if migration is None else {
            "manifest_id": migration["manifest_id"], "source_version": migration["source_version"],
            "target_version": migration["target_version"], "backup": migration["backup"],
            "result": migration["result"]}
        reconciled = "migration_applied" in activation.body["completed"]
        items.append({"activation_id": name, "verification": "verified", "recorded": recorded,
                      "activation_status": activation.status, "pending_step": activation.body["pending"],
                      "migration_step_completed": reconciled})
        if migration and not reconciled:
            blockers.append(f"migration evidence awaits activation reconciliation: {name}")
    data = {"root": str(root), "database": database, "items": items,
            "next_cursor": None, "blockers": blockers}
    executing = _executing_release(root)
    if blockers:
        code = "unavailable" if database["state"] == "unreadable" else "conflict"
        raise CommandFailure(code, f"{code}: migration status has blockers", data=data, release_id=executing)
    return CommandOutput(data, executing, (f"SQL schema {database['user_version']}; {len(items)} activation records inspected.",))


def dispatch(args) -> CommandOutput:
    root = _host_root(args)
    return {"host.releases": _host_releases, "config.plan": _config_plan,
            "config.diff": _config_diff, "migration.plan": _migration_plan,
            "migration.status": _migration_status}[args.public_command](args, root)
