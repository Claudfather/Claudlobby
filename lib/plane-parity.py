#!/usr/bin/env python3
"""Dual-write parity: legacy JSONL ledger <-> plane rows (Phase-2 T3, plan
§4.1). The canary's verdict instrument during door rollout and a permanent
reconciliation door afterward. Standalone stdlib (dispatch-overdue.py
precedent) — runs where the package is not installed.

Join contract (the generalization of the plan's per-key list, and the
CONTRACT door shims must honor): every envelope a door emits carries
    source_ref = "<ledger-name>:<legacy-id>"
(e.g. "dispatch-log:tsk_a1b2..."). Parity then needs no per-door schema.

Three mismatch classes, all rc 1 (plan §4.1: missing-in-plane,
missing-in-legacy, field mismatches — plus per-table duplicates, since a
set-collapse would hide a door double-writing one fact):
  presence      an id on one side only
  multiplicity  more than one row for one id in ONE plane table (cross-table
                presence is normal — a dispatch door legitimately lands
                work_item + assignment + communication under one source_ref)
  fields        --field LEDGER_KEY=TABLE.COLUMN pairs compared on matched ids
When no --field is given, that is DISCLOSED (ids + multiplicity only) — the
per-door field maps arrive with the doors themselves.

The prefix match is EXACT (substr comparison), never LIKE: '%' and '_' in a
ledger name are text here, not wildcards — 'dispatch_log' must not match
'dispatchXlog' rows (measured; it did under LIKE).

--since windows the LEDGER (the join driver), but the two sides carry
DIFFERENT writers' clocks, so a window applied naively to each manufactures
mismatches at the boundary (measured: one fact at 11:59:59 legacy /
12:00:01 plane across a 12:00:00 cutoff read as missing-in-legacy plus
"nothing to compare"). Two guards close both directions: the plane side is
read with a pre-window grace (--skew-grace, default 600s) so a plane row
whose clock ran slightly behind its in-window ledger twin still joins; and
a plane-only id is checked against the UNWINDOWED ledger id set before
being declared missing-in-legacy — a twin that exists below the window is
WINDOW-SKEW, disclosed and excluded from the verdict, never a mismatch.

Unreachable is never empty (source_state.py's rule, vocabulary mirrored here
because a lib/ standalone cannot import the package): an ABSENT or unreadable
ledger/db REFUSES at rc 3 — "cannot look" must not read as "nothing to
reconcile" — while an EXISTING ledger with zero rows is a door that has not
fired yet, for which "nothing to compare" is TRUE (rc 0). Malformed ledger
lines and rows missing the id field are counted and disclosed, never silently
dropped. Human-facing stdout; the refusal rides stderr + rc.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PLANE_TABLES = ("communications", "work_items", "assignments",
                "workstreams", "events", "registry_snapshots", "metric_samples")
LIST_CAP = 20


def _refuse(msg: str) -> int:
    print(f"plane-parity: UNREACHABLE — {msg}", file=sys.stderr)
    print("plane-parity: refusing to answer (an unreachable source must not"
          " read as parity-clean)", file=sys.stderr)
    return 3


def _usage_refuse(msg: str) -> None:
    # rc 2, deliberately NOT 1: a bad flag is a malformed CALL, and it must
    # never be readable as "parity broken" (rc 1) or "clean" (rc 0).
    print(f"plane-parity: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _parse_fields(specs: list[str]) -> dict[str, tuple[str, str]]:
    """--field LEDGER_KEY=TABLE.COLUMN -> {ledger_key: (table, column)}."""
    out: dict[str, tuple[str, str]] = {}
    for spec in specs:
        try:
            ledger_key, target = spec.split("=", 1)
            table, column = target.split(".", 1)
        except ValueError:
            _usage_refuse(f"bad --field {spec!r} (want LEDGER_KEY=TABLE.COLUMN)")
        if table not in PLANE_TABLES:
            _usage_refuse(
                f"unknown table in --field {spec!r}"
                f" (know: {', '.join(PLANE_TABLES)})"
            )
        if not column.replace("_", "").isalnum():
            _usage_refuse(f"bad column in --field {spec!r}")
        out[ledger_key] = (table, column)
    return out


def read_legacy(path: Path, id_field: str, ts_field: str, since: str | None,
                field_keys: list[str]) -> tuple[dict, set, int, int, int]:
    """({windowed id: {"count": n, "fields": {key: value}}}, ALL ids
    unwindowed, total_in_window, malformed, unjoinable). The unwindowed set
    is the window-skew discriminator: a plane-only id with a ledger twin
    below --since is clock skew, not a lost write. Fields keep the FIRST
    row's values; a multi-row id is already flagged by multiplicity, where
    field comparison is ill-posed."""
    rows: dict[str, dict] = {}
    all_ids: set[str] = set()
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
            legacy_id = row.get(id_field)
            if legacy_id:
                all_ids.add(str(legacy_id))
            if since and str(row.get(ts_field, "")) < since:
                total -= 1
                continue
            if not legacy_id:
                unjoinable += 1
                continue
            entry = rows.setdefault(str(legacy_id), {"count": 0, "fields": {}})
            entry["count"] += 1
            if entry["count"] == 1:
                entry["fields"] = {k: row.get(k) for k in field_keys}
    return rows, all_ids, total, malformed, unjoinable


def _grace_bound(since: str, grace_s: int) -> str:
    return (datetime.fromisoformat(since) - timedelta(seconds=grace_s)).isoformat()


def read_plane(db: Path, prefix: str, since: str | None, grace_s: int,
               fields: dict[str, tuple[str, str]]) -> dict:
    """{id: {"tables": {table: count}, "fields": {ledger_key: value},
             "min_occurred_at": str}} — prefix matched EXACTLY via substr."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            raise sqlite3.OperationalError("db has no plane schema (user_version 0)")
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        out: dict[str, dict] = {}
        plane_since = _grace_bound(since, grace_s) if since else None
        for table in PLANE_TABLES:
            if table not in present:
                continue
            wanted = [(k, col) for k, (t, col) in fields.items() if t == table]
            cols = "".join(f", {col}" for _, col in wanted)
            sql = (f"SELECT source_ref, occurred_at{cols} FROM {table}"
                   " WHERE substr(source_ref, 1, ?) = ?")
            params: list = [len(prefix) + 1, prefix + ":"]
            if plane_since:
                sql += " AND occurred_at >= ?"
                params.append(plane_since)
            for row in conn.execute(sql, params):
                legacy_id = row[0].split(":", 1)[1]
                entry = out.setdefault(legacy_id, {
                    "tables": {}, "fields": {}, "min_occurred_at": row[1],
                })
                entry["tables"][table] = entry["tables"].get(table, 0) + 1
                if row[1] and (entry["min_occurred_at"] is None
                               or row[1] < entry["min_occurred_at"]):
                    entry["min_occurred_at"] = row[1]
                if entry["tables"][table] == 1:
                    for i, (k, _col) in enumerate(wanted):
                        entry["fields"][k] = row[2 + i]
        return out
    finally:
        conn.close()


def _print_capped(label: str, items: list[str]) -> None:
    print(f"  {label}: {len(items)}")
    for i in items[:LIST_CAP]:
        print(f"    - {i}")
    if len(items) > LIST_CAP:
        print(f"    ... and {len(items) - LIST_CAP} more")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy", required=True, help="Legacy JSONL ledger path")
    ap.add_argument("--ledger-name", required=True,
                    help="source_ref prefix the door stamps (e.g. dispatch-log)")
    ap.add_argument("--id-field", required=True,
                    help="Ledger field carrying the legacy id (e.g. task_id)")
    ap.add_argument("--ts-field", default="ts")
    ap.add_argument("--db", required=True, help="plane.db path")
    ap.add_argument("--since", help="ISO lower bound on the LEDGER window")
    ap.add_argument("--skew-grace", type=int, default=600,
                    help="Seconds of pre-window grace on the plane side"
                         " (different writers' clocks; default 600)")
    ap.add_argument("--field", action="append", default=[],
                    metavar="LEDGER_KEY=TABLE.COLUMN",
                    help="Compare this ledger field against this plane column"
                         " on matched ids (repeatable)")
    args = ap.parse_args()

    fields = _parse_fields(args.field)
    legacy_path = Path(args.legacy)
    db_path = Path(args.db)
    if legacy_path.is_dir():
        return _refuse(f"{legacy_path} is a directory, not a ledger")
    if not legacy_path.exists():
        return _refuse(f"legacy ledger absent: {legacy_path}")
    if not db_path.exists():
        return _refuse(f"plane db absent: {db_path}")
    if args.since:
        try:
            _grace_bound(args.since, args.skew_grace)
        except ValueError as exc:
            return _refuse(f"unparseable --since: {exc}")
    try:
        legacy, ledger_all_ids, total, malformed, unjoinable = read_legacy(
            legacy_path, args.id_field, args.ts_field, args.since,
            list(fields))
    except OSError as exc:
        return _refuse(f"legacy ledger unreadable: {exc}")
    try:
        plane = read_plane(db_path, args.ledger_name, args.since,
                           args.skew_grace, fields)
    except sqlite3.Error as exc:
        return _refuse(f"plane db unreadable: {exc}")

    legacy_ids, plane_ids = set(legacy), set(plane)
    missing_in_plane = sorted(legacy_ids - plane_ids)
    skew_window: list[str] = []
    missing_in_legacy: list[str] = []
    for pid in sorted(plane_ids - legacy_ids):
        if args.since and pid in ledger_all_ids:
            skew_window.append(pid)   # a ledger twin exists BELOW the window:
        else:                          # clock skew, not a lost write
            missing_in_legacy.append(pid)
    matched = sorted(legacy_ids & plane_ids)

    duplicates: list[str] = []
    for pid in matched:
        for table, n in plane[pid]["tables"].items():
            if n > 1:
                duplicates.append(f"{pid}: {n} rows in {table}")
        if legacy[pid]["count"] > 1:
            duplicates.append(f"{pid}: {legacy[pid]['count']} ledger rows")

    field_mismatches: list[str] = []
    for pid in matched:
        for key, (table, column) in fields.items():
            lval = legacy[pid]["fields"].get(key)
            if table not in plane[pid]["tables"]:
                field_mismatches.append(
                    f"{pid} {key}: no {table} row to compare against")
                continue
            pval = plane[pid]["fields"].get(key)
            if (lval is None) != (pval is None) or (
                    lval is not None and str(lval) != str(pval)):
                field_mismatches.append(
                    f"{pid} {key}: ledger {lval!r} != plane {column}={pval!r}")

    scope = f"ledger={legacy_path} prefix={args.ledger_name}:" + (
        f" since={args.since} (plane grace {args.skew_grace}s)"
        if args.since else "")
    print(f"plane-parity: {scope}")
    print(f"  legacy rows: {total} (ids: {len(legacy_ids)}, malformed: {malformed},"
          f" unjoinable[no {args.id_field}]: {unjoinable})")
    print(f"  matched: {len(matched)}")
    _print_capped("missing in plane", missing_in_plane)
    _print_capped("missing in legacy", missing_in_legacy)
    _print_capped("duplicate rows", duplicates)
    if fields:
        print(f"  field comparison: {len(fields)} field(s) over"
              f" {len(matched)} matched id(s)")
        _print_capped("field mismatches", field_mismatches)
    else:
        print("  field comparison: none requested (--field absent —"
              " ids + multiplicity only)")
    if skew_window:
        _print_capped("window-skew (ledger twin below --since; not counted)",
                      skew_window)
    if total == 0:
        print("  nothing to compare — the ledger exists and holds no rows"
              " (a door that has not fired yet)")
    if malformed or unjoinable:
        print(f"  NOTE: {malformed + unjoinable} ledger rows could not be"
              " joined — parity is asserted over the joinable set only")
    broken = bool(missing_in_plane or missing_in_legacy
                  or duplicates or field_mismatches)
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
