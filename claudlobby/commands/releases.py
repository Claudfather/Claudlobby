"""Read-only historical evidence and Git configuration diff adapters.

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
import stat

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


def _verified_activation_journal(root: Path, path: Path) -> bool:
    """Only the two retained ConfigInstall journal owners are not root records."""
    record = path / "activation.json"
    if (record.exists() or record.is_symlink()
            or not re.fullmatch(r"(?:units|enrollment)-[0-9a-f]{64}", path.name)):
        return False
    from ..config_install import read_config_install
    from ..config_plan import PlanError, read_plan

    try:
        journal = read_config_install(root, path.name)
        plan = read_plan(root, journal.plan_id)
        owner = "activation-units-v1" if path.name.startswith("units-") else "activation-enrollment-v1"
        return plan.effects.get("owner") == owner
    except (PlanError, OSError, ValueError):
        return False  # Corrupt or unrelated directories remain visible blockers.


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
        names = set()
        for path in activation_dir.iterdir() if activation_dir.exists() else ():
            if not path.is_dir():
                continue
            # Parking/publication journals share this store with root records.
            if _verified_activation_journal(root, path):
                continue
            names.add(path.name)
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
    from ..config_install import change_effect, review_nodes

    def digest(state):
        return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    # Both digests cover the node shape apply compares, so they are equal
    # exactly when the path is unchanged (#2449).
    items = []
    for change in plan.changes:
        before, after = review_nodes(change)
        items.append({"path": change.target, "change": change_effect(change),
                      "before": {"kind": before["kind"], "state_sha256": digest(before)},
                      "after": {"kind": change.after["kind"], "state_sha256": digest(after)}})
    return items


def _summary(plan, changes: list[dict]) -> dict:
    """Counts by effect, and the bots a content change reaches. A plan that
    records no bot paths (staged by an older release) cannot say: None, not []."""
    summary = {"paths": len(changes), **{effect: sum(item["change"] == effect for item in changes)
                                         for effect in ("content", "mode", "unchanged")},
               "content_bots": None, "content_outside_bots": None}
    owned = plan.effects.get("bot_paths")
    if isinstance(owned, dict):
        roots = {bot: [Path(os.path.abspath(path)) for path in paths] for bot, paths in owned.items()}
        reached, outside = set(), 0
        for item in changes:
            if item["change"] == "content":
                path = Path(os.path.abspath(item["path"]))
                owners = {bot for bot, paths in roots.items()
                          if any(path.is_relative_to(root) for root in paths)}
                reached |= owners
                outside += not owners
        summary.update(content_bots=sorted(reached), content_outside_bots=outside)
    return summary


def _render_data(rendered) -> dict:
    changes = _changes(rendered)
    return {"config_fingerprint": rendered.fingerprint, "source_revision": rendered.revision,
            "fleets": list(rendered.fleets), "changes": changes, "summary": _summary(rendered, changes),
            "effects": {"restart_bots": rendered.effects.get("restart_bots", []),
                        "restart_reason": rendered.effects.get("restart_reason")}}


def _summary_line(data) -> str:
    summary = data["summary"]
    return (f"Configuration {data['config_fingerprint']}: {summary['paths']} reviewed paths "
            f"(content {summary['content']}, mode only {summary['mode']}, unchanged {summary['unchanged']})")


def _review_lines(data) -> tuple[str, ...]:
    """Content and mode changes one per line; unchanged paths are counted, and --json lists them."""
    summary, bots = data["summary"], data["effects"]["restart_bots"]
    lines = [_summary_line(data) + "; --json lists every path."]
    lines.extend(f"{item['change']} {item['path']}: {item['before']['kind']} -> {item['after']['kind']}"
                 for item in data["changes"] if item["change"] != "unchanged")
    if summary["content_bots"] is None:
        lines.append("Bots reached: unknown, since this plan records no bot paths (an older release "
                     "staged it); stage it again to see them.")
    else:
        lines.append(f"Content changes reach {len(summary['content_bots'])} of {len(bots)} bots: "
                     f"{', '.join(summary['content_bots']) or 'none'}; paths outside bot directories: "
                     f"{summary['content_outside_bots']}.")
    reason = data["effects"]["restart_reason"]
    lines.append(f"Declared restart scope: {len(bots)} bots" + (f": {reason}." if reason else "."))
    return tuple(lines)


def _config_diff(args, root: Path) -> CommandOutput:
    from ..config_plan import PlanError, read_rendered
    from ..resources import get_resources
    if args.bot:
        raise CommandFailure("invalid_argument", "invalid argument: --bot applies only to committed drift")
    if not re.fullmatch(r"[0-9a-f]{64}", args.fingerprint):
        raise CommandFailure("invalid_argument", "invalid argument: expected a Git render fingerprint")
    try:
        package = get_resources()
        if package.installation is None or package.installation.data_root != root:
            raise ValueError()
        rendered = read_rendered(root, args.fingerprint)
        if rendered.revision is None:
            raise ValueError()
    except (PlanError, ValueError, RuntimeError, OSError) as exc:
        raise CommandFailure("conflict", "pending Git configuration is unavailable or changed") from exc
    data = _render_data(rendered)
    return CommandOutput(data, None, _review_lines(data))


def _config_current_drift(args) -> CommandOutput:
    from types import SimpleNamespace
    from ..active_config import read_current_config, load_active_context, runtime_read_stamp
    from ..config_plan import ConfigChange, PlanError, path_state
    from ..resources import get_resources
    if args.seed:
        raise CommandFailure("invalid_argument", "committed configuration does not use --seed")
    try:
        package = get_resources()
        if package.installation is None:
            raise ValueError()
        root = Path(args.root).expanduser().resolve() if args.root is not None else package.installation.data_root
        stamp = runtime_read_stamp(root, package=package)
        snapshot = read_current_config(root, installation=package.installation, revision=package.source_revision)
        prefix = None
        if args.bot or args.fleet:
            context = load_active_context(root, args.fleet, bot=args.bot, package=package)
            prefix = context.paths.bot_runtime(args.bot) if args.bot else context.paths.runtime_fleet
        changes = []
        for item in snapshot['owned_outputs']:
            target, desired = Path(item['target']), item['state']
            if prefix is not None and not target.is_relative_to(prefix):
                continue
            info = target.lstat() if target.exists() or target.is_symlink() else None
            if desired['kind'] == 'directory' and info is not None and stat.S_ISDIR(info.st_mode):
                # Never traverse mutable memory/projects beneath ensured dirs.
                before = {'node': {'kind': 'directory', 'mode': stat.S_IMODE(info.st_mode), 'entries': {}}}
            else:
                before = path_state(target)
            changes.append(ConfigChange(str(target), before, desired))
        items = _changes(SimpleNamespace(changes=changes))
        stamp.check(root, package=package)
    except CommandFailure:
        raise
    except (PlanError, ValueError, RuntimeError, OSError) as exc:
        raise CommandFailure("conflict", "committed Git configuration drift is unavailable") from exc
    data = {'mode': 'current', 'fleet': args.fleet, 'bot': args.bot,
            'source_revision': snapshot['revision'], 'config_fingerprint': snapshot['fingerprint'],
            'changes': items}
    lines = tuple(f"{item['change']} {item['path']}: {item['before']['kind']} -> {item['after']['kind']}"
                  for item in items if item['change'] != 'unchanged')
    return CommandOutput(data, None, lines or ('Committed generated outputs are unchanged.',))


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
        names = [p.name for p in sorted(directory.iterdir())
                 if p.is_dir() and not _verified_activation_journal(root, p)] if directory.exists() else []
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
    if args.public_command == "config.diff" and args.fingerprint is None:
        return _config_current_drift(args)
    root = _host_root(args)
    return {"host.releases": _host_releases, "config.diff": _config_diff,
            "migration.status": _migration_status}[args.public_command](args, root)
