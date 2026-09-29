"""Public Plane spool and retention operations; kernel modules own the writes."""

from __future__ import annotations

import json
import os
import re
import sqlite3

from ..command_result import CommandFailure, CommandOutput
from ..activation_state import ActivationError
from ..context import resolve_paths
from ..plane.db import connect, connect_ro, db_file
from ..plane.ids import ensure_host_uid
from ..plane.migrations import DowngradeError
from ..plane.schema_state import PendingMigrationError, preflight_schema, require_current_schema
from ..plane.spool import drain, quarantine_dir, quarantine_entry, spool_dir, spool_entries
from ..runtime_admission import RuntimeIdentity, mutation_admission


_SPOOL_NAME = re.compile(r"ev_[0-9a-f]{32}\.json")


def _root(args):
    try:
        return resolve_paths(root=args.root, fleet=args.fleet, seed=args.seed).root
    except (OSError, ValueError, RuntimeError) as exc:
        raise CommandFailure("invalid_argument", "invalid Plane root or fleet selector") from exc


def _schema_failure(exc):
    if isinstance(exc, PendingMigrationError):
        return CommandFailure("migration_required", f"REFUSED: {exc}")
    return CommandFailure("downgrade", f"REFUSED: {exc}")


def spool(args) -> CommandOutput:
    root = _root(args)
    action = args.spool_action
    if action == "list":
        entries = spool_entries(root)
        summary = [{"name": entry["_file"], "event_ids": entry.get("event_ids"),
                    "attempts": entry.get("attempts"), "spooled_at": entry.get("spooled_at")}
                   for entry in entries]
        return CommandOutput({"entries": summary}, lines=tuple(
            f"{entry['_file']}  events={entry.get('event_ids')}  attempts={entry.get('attempts')}"
            for entry in entries))
    if action in {"inspect", "quarantine"}:
        name = args.name or ""
        if not _SPOOL_NAME.fullmatch(name):
            raise CommandFailure("invalid_argument", "invalid spool entry name")
        if action == "inspect":
            source = spool_dir(root) / name
            if not source.exists():
                source = quarantine_dir(root) / name
            if not source.exists():
                raise CommandFailure("not_found", f"no such spool entry: {name}")
            try:
                entry = json.loads(source.read_text())
                reason_file = source.with_name(source.name + ".reason")
                reason = reason_file.read_text().strip() if reason_file.exists() else None
            except (OSError, json.JSONDecodeError) as exc:
                raise CommandFailure("unavailable", "spool entry is unreadable") from exc
            return CommandOutput({"name": name, "entry": entry, "quarantine_reason": reason},
                                 lines=tuple(filter(None, (f"quarantined: {reason}" if reason else "",
                                                     json.dumps(entry, indent=2, sort_keys=True, default=str)))))
        try:
            with mutation_admission(root, identity=RuntimeIdentity.current(),
                                    expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
                source = spool_dir(root) / name
                if not source.exists():
                    raise CommandFailure("not_found", f"no such spool entry: {name}")
                try:
                    quarantine_entry(root, source, "operator")
                except OSError as exc:
                    # The reason sidecar may already have been written. The
                    # move is unproved; inspect before any explicit retry.
                    raise CommandFailure("commit_unknown", "quarantine outcome unknown; inspect the entry before retrying",
                                         data={"name": name, "quarantined": "unknown"},
                                         release_id=release.release_id) from exc
                return CommandOutput({"name": name, "quarantined": True},
                                     release_id=release.release_id, lines=(f"quarantined {name}",))
        except ActivationError as exc:
            raise CommandFailure("release_mismatch", f"spool quarantine refused: {exc}") from exc
    if action != "retry":
        raise CommandFailure("invalid_argument", "unknown spool action")
    try:
        with mutation_admission(root, identity=RuntimeIdentity.current(),
                                expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
            preflight_schema(root)
            conn = connect(db_file(root))
            try:
                require_current_schema(conn)
                host = ensure_host_uid(root / "state")
                report = drain(root, conn, host)
            finally:
                conn.close()
    except ActivationError as exc:
        raise CommandFailure("release_mismatch", f"spool retry refused: {exc}") from exc
    except (PendingMigrationError, DowngradeError) as exc:
        raise _schema_failure(exc) from exc
    except (sqlite3.Error, OSError) as exc:
        # Drain commits per entry. A prior entry may already be recorded.
        raise CommandFailure("commit_unknown", "spool drain outcome unknown; inspect spool and Plane before retrying",
                             data={"drain": "unknown"}) from exc
    data = {"ingested": report.ingested, "duplicates": report.duplicates,
            "quarantined": report.quarantined, "remaining": report.remaining,
            "attempted": report.attempted, "refused": list(report.refused),
            "limited": report.limited}
    line = (f"ingested={report.ingested} duplicates={report.duplicates}"
            f" quarantined={report.quarantined} remaining={report.remaining}")
    if report.quarantined or report.refused:
        raise CommandFailure("conflict", "spool drain quarantined or refused entries; inspect them",
                             data=data)
    if report.remaining or report.limited:
        raise CommandFailure("spooled", "spool entries remain pending; inspect before retrying", data=data)
    return CommandOutput(data, release_id=release.release_id, lines=(line,))


def prune(args) -> CommandOutput:
    from ..plane.retention import DEFAULT_RETENTION_DAYS

    root = _root(args)
    days = args.days if args.days is not None else DEFAULT_RETENTION_DAYS
    if days < 0:
        raise CommandFailure("invalid_argument", "retention days cannot be negative")
    if args.dry_run:
        return _prune_under_scope(root, args, days, None)
    try:
        with mutation_admission(root, identity=RuntimeIdentity.current(),
                                expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
            return _prune_under_scope(root, args, days, release.release_id)
    except ActivationError as exc:
        raise CommandFailure("release_mismatch", f"prune refused: {exc}") from exc


def _prune_under_scope(root, args, days: int, release_id: str | None) -> CommandOutput:
    from ..plane.retention import (PRUNABLE_SYSTEM_EVENTS, prune_metric_samples,
                                   prune_system_events)

    path = db_file(root)
    if not path.exists():
        return CommandOutput({"dry_run": args.dry_run, "days": days, "db": "absent",
                              "metric_samples": 0, "system_events": None},
                             release_id=release_id,
                             lines=(f"prune: no plane db at {path} — nothing to age out",))
    system_on = os.environ.get("PLANE_PRUNE_SYSTEM_EVENTS_ENABLED", "").strip() == "1"
    try:
        preflight_schema(root)
        conn = connect_ro(path) if args.dry_run else connect(path)
        try:
            require_current_schema(conn)
            if not args.dry_run:
                # Both retention lanes and their watermarks form one verdict.
                conn.execute("BEGIN IMMEDIATE")
            try:
                res = prune_metric_samples(conn, days=days, dry_run=args.dry_run)
                system_count = None
                if system_on:
                    if args.dry_run:
                        marks = ",".join("?" for _ in PRUNABLE_SYSTEM_EVENTS)
                        system_count = conn.execute(
                            f"SELECT COUNT(*) FROM events WHERE kind='system'"
                            f" AND event IN ({marks}) AND ingested_at < ?",
                            (*sorted(PRUNABLE_SYSTEM_EVENTS), res.cutoff)).fetchone()[0]
                    else:
                        system_count = prune_system_events(conn, days=days)
                if not args.dry_run:
                    conn.commit()
            except Exception:
                if not args.dry_run:
                    conn.rollback()
                raise
        finally:
            conn.close()
    except (PendingMigrationError, DowngradeError) as exc:
        raise _schema_failure(exc) from exc
    except (sqlite3.Error, OSError) as exc:
        raise CommandFailure("commit_unknown" if not args.dry_run else "unavailable",
                             "prune outcome unknown; inspect Plane before retrying" if not args.dry_run
                             else "Plane retention scan unavailable") from exc
    count = res.candidates if args.dry_run else res.deleted
    verb = "would delete" if args.dry_run else "deleted"
    lines = [f"metric_samples: {verb} {count} rows older than {days}d (cutoff {res.cutoff})"]
    if system_count is None:
        lines.append("system events: lane OFF — arm with PLANE_PRUNE_SYSTEM_EVENTS_ENABLED=1"
                     " in this host's .env (it deletes data; see claudlobby host doctor --switches)")
    else:
        lines.append(f"system events: {verb} {system_count} row(s) older than {days}d,"
                     f" of {sorted(PRUNABLE_SYSTEM_EVENTS)} — every other event type is kept")
    return CommandOutput({"dry_run": args.dry_run, "days": days, "cutoff": res.cutoff,
                          "metric_samples": count, "system_events": system_count,
                          "system_events_enabled": system_on},
                         release_id=release_id, lines=tuple(lines))
