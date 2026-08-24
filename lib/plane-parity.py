#!/usr/bin/env python3
"""Dual-write parity: legacy JSONL ledger <-> plane rows (Phase-2 T3, plan
§4.1). The canary's verdict instrument during door rollout and a permanent
reconciliation door afterward. Standalone stdlib (dispatch-overdue.py
precedent) — runs where the package is not installed.

Join contract (the generalization of the plan's per-key list, and the
CONTRACT door shims must honor): every envelope a door emits carries
    source_ref = "<ledger-name>:<legacy-id>"
(e.g. "dispatch-log:tsk_a1b2..."). Parity then needs no per-door schema: it
derives the expected source_ref set from the legacy ledger and set-diffs both
directions against every plane table's source_ref column.

Unreachable is never empty (source_state.py's rule, vocabulary mirrored here
because a lib/ standalone cannot import the package): an ABSENT or unreadable
ledger/db REFUSES at rc 3 — "cannot look" must not read as "nothing to
reconcile" — while an EXISTING ledger with zero rows is a door that has not
fired yet, for which "nothing to compare" is TRUE (rc 0). Malformed ledger
lines and rows missing the id field are counted and disclosed, never silently
dropped. rc 1 = parity broken (either direction). Human-facing stdout; the
refusal rides stderr + rc.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PLANE_TABLES = ("communications", "work_items", "assignments",
                "workstreams", "events", "registry_snapshots", "metric_samples")


def _refuse(msg: str) -> int:
    print(f"plane-parity: UNREACHABLE — {msg}", file=sys.stderr)
    print("plane-parity: refusing to answer (an unreachable source must not"
          " read as parity-clean)", file=sys.stderr)
    return 3


def read_legacy(path: Path, id_field: str, ts_field: str,
                since: str | None) -> tuple[set, int, int, int]:
    """(ids, total_rows, malformed_lines, unjoinable_rows)"""
    ids: set[str] = set()
    total = malformed = unjoinable = 0
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("row is not an object")
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            if since and str(row.get(ts_field, "")) < since:
                total -= 1
                continue
            legacy_id = row.get(id_field)
            if not legacy_id:
                unjoinable += 1
                continue
            ids.add(str(legacy_id))
    return ids, total, malformed, unjoinable


def read_plane(db: Path, prefix: str, since: str | None) -> set:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            raise sqlite3.OperationalError("db has no plane schema (user_version 0)")
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        refs: set[str] = set()
        for table in PLANE_TABLES:
            if table not in present:
                continue
            sql = (f"SELECT source_ref FROM {table}"
                   " WHERE source_ref LIKE ? || ':%'")
            params: list = [prefix]
            if since:
                sql += " AND occurred_at >= ?"
                params.append(since)
            refs.update(r[0] for r in conn.execute(sql, params))
        return {r.split(":", 1)[1] for r in refs}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy", required=True, help="Legacy JSONL ledger path")
    ap.add_argument("--ledger-name", required=True,
                    help="source_ref prefix the door stamps (e.g. dispatch-log)")
    ap.add_argument("--id-field", required=True,
                    help="Ledger field carrying the legacy id (e.g. task_id)")
    ap.add_argument("--ts-field", default="ts")
    ap.add_argument("--db", required=True, help="plane.db path")
    ap.add_argument("--since", help="ISO lower bound (ledger ts + plane occurred_at)")
    args = ap.parse_args()

    legacy_path = Path(args.legacy)
    db_path = Path(args.db)
    if legacy_path.is_dir():
        return _refuse(f"{legacy_path} is a directory, not a ledger")
    if not legacy_path.exists():
        return _refuse(f"legacy ledger absent: {legacy_path}")
    if not db_path.exists():
        return _refuse(f"plane db absent: {db_path}")
    try:
        ids, total, malformed, unjoinable = read_legacy(
            legacy_path, args.id_field, args.ts_field, args.since)
    except OSError as exc:
        return _refuse(f"legacy ledger unreadable: {exc}")
    try:
        plane_ids = read_plane(db_path, args.ledger_name, args.since)
    except sqlite3.Error as exc:
        return _refuse(f"plane db unreadable: {exc}")

    missing_in_plane = sorted(ids - plane_ids)
    missing_in_legacy = sorted(plane_ids - ids)
    matched = len(ids & plane_ids)

    scope = f"ledger={legacy_path} prefix={args.ledger_name}:" + (
        f" since={args.since}" if args.since else "")
    print(f"plane-parity: {scope}")
    print(f"  legacy rows: {total} (ids: {len(ids)}, malformed: {malformed},"
          f" unjoinable[no {args.id_field}]: {unjoinable})")
    print(f"  matched: {matched}")
    print(f"  missing in plane:  {len(missing_in_plane)}")
    for i in missing_in_plane[:20]:
        print(f"    - {i}")
    if len(missing_in_plane) > 20:
        print(f"    ... and {len(missing_in_plane) - 20} more")
    print(f"  missing in legacy: {len(missing_in_legacy)}")
    for i in missing_in_legacy[:20]:
        print(f"    - {i}")
    if len(missing_in_legacy) > 20:
        print(f"    ... and {len(missing_in_legacy) - 20} more")
    if total == 0:
        print("  nothing to compare — the ledger exists and holds no rows"
              " (a door that has not fired yet)")
    broken = bool(missing_in_plane or missing_in_legacy)
    if malformed or unjoinable:
        print(f"  NOTE: {malformed + unjoinable} ledger rows could not be"
              " joined — parity is asserted over the joinable set only")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
