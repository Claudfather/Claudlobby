#!/usr/bin/env python3
"""plane-shadow-check.py — the fleet-pulse bridge's question, answered stdlib.

Does any (bot, reader) have a DIVERGED latest recorded shadow comparison?
The fleet's watchdog asks this every sweep on Pi-class hosts, so it must not
import the package (`lib/plane-lookup.py` precedent — a bash door stays
cheap: sqlite3 + a read-only open). The package twin is
``claudlobby.plane.shadow.latest_diverged`` / ``plane shadow --check`` (the
operator's door); the tail read here mirrors ``shadow.TAIL_SQL`` — keep the
two in step.

Output: one ``<bot> <reader> <at>`` line per diverged pair. Exit 0 clean ·
1 diverged · 3 unreachable (no db, unreadable) · 2 usage.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

EVENTS = ("shadow_parity_clean", "shadow_parity_diverged")
KEY = ("COALESCE(CASE WHEN e.detail_truncated = 0 THEN json_extract(e.detail, '$.bot') END,"
       " e.subject_alias)")
READER = ("COALESCE(CASE WHEN e.detail_truncated = 0 THEN json_extract(e.detail, '$.reader') END,"
          " '*')")
LATEST_SQL = (
    f"SELECT {KEY} AS bot, {READER} AS reader, e.event, e.occurred_at FROM events e"
    " WHERE e.kind = 'system' AND e.event IN (?, ?) AND " + KEY + " LIKE ?"
    " ORDER BY e.occurred_at DESC, e.ingest_seq DESC"
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--fleet", required=True)
    a = ap.parse_args(argv)
    if not a.root or not a.fleet:
        print("plane-shadow-check: --root and --fleet must be non-empty", file=sys.stderr)
        return 2
    db = os.path.join(a.root, "state", "plane", "plane.db")
    if not os.path.isfile(db):
        print(f"plane-shadow-check: no plane db at {db} — unreachable, not clean", file=sys.stderr)
        return 3
    prefix = f"bot:{a.fleet}/"
    try:
        import importlib.util
        _src = os.path.join(os.path.dirname(os.path.realpath(__file__)), "plane-readers.py")
        _spec = importlib.util.spec_from_file_location("plane_readers", _src)
        _pr = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_pr)
        conn = _pr.connect(a.root)                                  # ONE ro open (+retry)
        rows = conn.execute(LATEST_SQL, (*EVENTS, prefix + "%")).fetchall()
    except sqlite3.Error as exc:
        print(f"plane-shadow-check: db unreadable: {exc}", file=sys.stderr)
        return 3
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # newest first: the first row seen per (bot, reader) IS the latest. A
    # truncated record ('*') is the latest for EVERY reader of its bot.
    seen: set[tuple[str, str]] = set()
    diverged: list[tuple[str, str, str]] = []
    for bot, reader, event, at in rows:
        if not bot or not bot.startswith(prefix):
            continue
        readers = ("open", "overdue") if reader == "*" else (reader,)
        for r in readers:
            if (bot, r) in seen:
                continue
            seen.add((bot, r))
            if event == "shadow_parity_diverged":
                diverged.append((bot[len(prefix):], r, at))
    for bot, r, at in diverged:
        print(f"{bot} {r} {at}")
    return 1 if diverged else 0


if __name__ == "__main__":
    sys.exit(main())
