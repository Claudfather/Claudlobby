"""Read-only Plane health summary with explicit unknown queue states."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from ..command_result import CommandFailure, CommandOutput
from ..context import resolve_paths
from ..plane.db import connect_ro, db_file
from ..plane.health import staged_summary
from ..plane.identity import provisional_actors
from ..plane.migrations import DowngradeError
from ..plane.schema_state import PendingMigrationError, require_current_schema
from ..plane.spool import oldest_spooled_at, scan_spool

_FAMILY_COUNTS = {
    "communication": ("communications", None),
    "transmission": ("events", "transmission"),
    "work_item": ("work_items", None),
    "assignment": ("assignments", None),
    "task": ("events", "task"),
}


def dispatch(args) -> CommandOutput:
    try:
        root = resolve_paths(root=args.root, fleet=args.fleet, seed=args.seed).root
        path = db_file(root)
        database = {"path": str(path), "state": "present" if path.exists() else "absent",
                    "schema_user_version": None, "ingest_seq_high_water": None,
                    "counts": None, "provisional_actors": None}
        lines = [f"db: {path} ({database['state']})"]
        if path.exists():
            with closing(connect_ro(path)) as conn:
                require_current_schema(conn)
                database["schema_user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
                database["ingest_seq_high_water"] = conn.execute(
                    "SELECT COALESCE(MAX(ingest_seq), 0) FROM ingest_ledger").fetchone()[0]
                counts = {}
                for family, (table, kind) in _FAMILY_COUNTS.items():
                    counts[family] = (conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                                      if kind is None else conn.execute(
                                          "SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)).fetchone()[0])
                database["counts"] = counts
                database["provisional_actors"] = len(provisional_actors(conn))
            lines.extend((f"schema user_version: {database['schema_user_version']}",
                          f"ingest_seq high-water: {database['ingest_seq_high_water']}"))
            lines.extend(f"  {family}: {count}" for family, count in counts.items())
            lines.append(f"provisional actors: {database['provisional_actors']}")

        scan = scan_spool(root)
        spool = {"state": scan.spool_state, "pending": None, "oldest_spooled_at": None,
                 "oldest_age_s": None}
        if scan.spool_state == "unreadable":
            lines.append("spool: unreadable — cannot count (a gap, not a zero)")
        else:
            oldest = oldest_spooled_at(scan.pending)
            spool["pending"] = len(scan.pending)
            spool["oldest_spooled_at"] = oldest
            if oldest:
                spool["oldest_age_s"] = int((datetime.now(timezone.utc)
                                             - datetime.fromisoformat(oldest)).total_seconds())
            age = f", oldest {spool['oldest_age_s']}s" if oldest else ""
            lines.append(f"spool: {spool['pending']} pending{age}")
        staged = staged_summary(root)
        if staged["state"] == "unreadable":
            lines.append("staged: unreadable — cannot count (a gap, not a zero)")
        else:
            lines.append(f"staged: {staged['pending']} pending, NOT committed"
                         f" ({staged['bytes']} bytes, oldest {staged['oldest_age_s']}s)")
        quarantine = {"state": scan.quarantine_state, "count": None}
        if scan.quarantine_state == "unreadable":
            lines.append("quarantine: unreadable — cannot count")
        else:
            quarantine["count"] = len(scan.quarantined)
            lines.append(f"quarantine: {quarantine['count']}")
        return CommandOutput({"database": database, "spool": spool,
                              "staged": staged, "quarantine": quarantine}, lines=tuple(lines))
    except PendingMigrationError as exc:
        raise CommandFailure("migration_required", f"Plane status refused: {exc}") from exc
    except DowngradeError as exc:
        raise CommandFailure("downgrade", f"Plane status refused: {exc}") from exc
    except sqlite3.Error as exc:
        raise CommandFailure("unavailable", "Plane status database is unavailable") from exc
