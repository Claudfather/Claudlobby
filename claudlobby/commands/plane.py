"""claudlobby emit / claudlobby plane — the kernel's CLI surface."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ._helpers import _resolve_paths
from ..plane.contracts import ContractViolation, export_schemas
from ..plane.db import connect, db_path
from ..plane.emit_api import emit, emit_batch
from ..plane.identity import provisional_actors
from ..plane.ids import ensure_host_uid
from ..plane.migrations import DowngradeError, migrate
from ..plane.spool import (
    SpoolWriteError, drain, quarantine_dir, spool_dir, spool_entries,
)

_FAMILY_COUNTS = {
    "communication": ("communications", None),
    "transmission": ("events", "transmission"),
    "work_item": ("work_items", None),
    "assignment": ("assignments", None),
    "task": ("events", "task"),
}


def _read_request(args) -> dict:
    raw = sys.stdin.read() if args.json == "-" else Path(args.json).read_text()
    request = json.loads(raw)
    request["event_type"] = args.event_type
    return request


def cmd_emit(args) -> int:
    root = _resolve_paths(args).root
    try:
        request = _read_request(args)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"emit: unreadable request: {exc}", file=sys.stderr)
        return 2
    try:
        outcome = emit(root, request)
    except ContractViolation as exc:
        first = exc.errors[0] if exc.errors else {}
        print(f"emit: contract violation: {first}", file=sys.stderr)
        return 2
    except SpoolWriteError as exc:
        print(f"emit: TOTAL FAILURE — {exc}", file=sys.stderr)
        return 3
    except DowngradeError as exc:
        # Never spooled (round-2 F6): a newer db is an operator condition,
        # not transient infrastructure — retrying it forever helps no one.
        print(f"emit: REFUSED — {exc}", file=sys.stderr)
        return 4
    print(outcome.event_id)
    if outcome.status == "spooled":
        print(f"plane: db unavailable — spooled {outcome.detail}", file=sys.stderr)
    return 0


def cmd_emit_batch(args) -> int:
    """One atomic unit of work: {"events": [...]} or a bare JSON array (F4)."""
    root = _resolve_paths(args).root
    try:
        raw = sys.stdin.read() if args.json == "-" else Path(args.json).read_text()
        parsed = json.loads(raw)
        requests = parsed["events"] if isinstance(parsed, dict) else parsed
        assert isinstance(requests, list) and requests
    except (OSError, json.JSONDecodeError, KeyError, AssertionError) as exc:
        print(f"emit-batch: unreadable request: {exc}", file=sys.stderr)
        return 2
    try:
        outcomes = emit_batch(root, requests)
    except ContractViolation as exc:
        first = exc.errors[0] if exc.errors else {}
        print(f"emit-batch: contract violation: {first}", file=sys.stderr)
        return 2
    except SpoolWriteError as exc:
        print(f"emit-batch: TOTAL FAILURE — {exc}", file=sys.stderr)
        return 3
    except DowngradeError as exc:
        print(f"emit-batch: REFUSED — {exc}", file=sys.stderr)
        return 4
    for o in outcomes:
        print(o.event_id)
    if outcomes and outcomes[0].status == "spooled":
        print(f"plane: db unavailable — spooled {outcomes[0].detail}", file=sys.stderr)
    return 0


def cmd_plane_status(args) -> int:
    root = _resolve_paths(args).root
    path = db_path(root)
    print(f"db: {path} ({'present' if path.exists() else 'absent'})")
    if path.exists():
        conn = connect(path)
        try:
            migrate(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            print(f"schema user_version: {version}")
            top = conn.execute(
                "SELECT COALESCE(MAX(ingest_seq), 0) FROM ingest_ledger"
            ).fetchone()[0]
            print(f"ingest_seq high-water: {top}")
            for family, (table, kind) in _FAMILY_COUNTS.items():
                if kind is None:
                    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                else:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)
                    ).fetchone()[0]
                print(f"  {family}: {n}")
            prov = provisional_actors(conn)
            print(f"provisional actors: {len(prov)}")
        finally:
            conn.close()
    entries = spool_entries(root)
    oldest = ""
    if entries and entries[0].get("spooled_at"):
        age = datetime.now(timezone.utc) - datetime.fromisoformat(
            entries[0]["spooled_at"]
        )
        oldest = f", oldest {int(age.total_seconds())}s"
    print(f"spool: {len(entries)} pending{oldest}")
    print(f"quarantine: {len(list(quarantine_dir(root).glob('*.json')))}")
    return 0


def cmd_plane_spool(args) -> int:
    root = _resolve_paths(args).root
    if args.spool_action == "list":
        for e in spool_entries(root):
            print(f"{e['_file']}  events={e.get('event_ids')}  attempts={e.get('attempts')}")
        return 0
    if args.spool_action == "retry":
        conn = connect(db_path(root))
        try:
            migrate(conn)
            host = ensure_host_uid(root / "state")
            report = drain(root, conn, host)
        finally:
            conn.close()
        print(
            f"ingested={report.ingested} duplicates={report.duplicates}"
            f" quarantined={report.quarantined} remaining={report.remaining}"
        )
        return 0
    if args.spool_action == "quarantine":
        import re as _re

        if not _re.fullmatch(r"ev_[0-9a-f]{32}\.json", args.name or ""):
            # Round-2 F9: the name is a filesystem operand — only validated
            # spool basenames, never path components.
            print(f"invalid spool entry name: {args.name!r}", file=sys.stderr)
            return 1
        src = spool_dir(root) / args.name
        if not src.exists():
            print(f"no such spool entry: {args.name}", file=sys.stderr)
            return 1
        import os

        from ..plane.spool import quarantine_entry

        quarantine_entry(root, src, "operator")
        print(f"quarantined {args.name}")
        return 0
    return 1


def cmd_plane_schema(args) -> int:
    print(json.dumps(export_schemas(), indent=2, sort_keys=True))
    return 0
