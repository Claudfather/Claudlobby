#!/usr/bin/env python3
"""plane-lookup.py — the plane answered by LEGACY TASK ID (cutover chunk 1).

Every dispatch stamps ``source_ref = "dispatch-log:<task_id>"`` on the
plane work_item and assignment it emits, so the plane can answer a legacy
task id today — and two doors need exactly that instead of grepping
``dispatch-log.jsonl``: ``report-back.sh`` recovers the work_item /
assignment ids for its own emission (the cutover's hard precondition —
retire the dispatch log's write and every report row would unlink), and
``dispatch-task.sh --supersedes`` needs the superseded dispatch's plane
ids to set ``supersedes_msg_id`` and emit a terminal ``superseded`` event.

Stdlib-only, like ``dispatch-overdue.py`` — a bash door must not pay the
package import on every call. Read-only (``mode=ro`` + ``query_only``).

Output: ``<work_item_id> <assignment_id> <dispatch_msg_id>`` for the LATEST
matching assignment (by ingest order), or nothing. Exit codes follow the
unreachable ≠ empty rule (source_state): 0 found · 0 not-found (empty
stdout, a note on stderr — a stamped id is NOT proof the row exists, so
the caller keeps its legacy fallback until cutover) · 3 unreachable (no
db, or unopenable) · 2 usage.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

SQL = (
    "SELECT a.work_item_id, a.assignment_id, a.dispatch_msg_id, i.alias"
    " FROM assignments a"
    " LEFT JOIN identity_registry i ON i.uid = a.assignee_uid"
    " WHERE a.source_ref = ?"
    " ORDER BY a.ingest_seq DESC"
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--assignee", default=None,
                    help="bot:<fleet>/<name>; name part compared case-insensitively")
    a = ap.parse_args(argv)
    db = os.path.join(a.root, "state", "plane", "plane.db")
    if not os.path.isfile(db):
        print(f"plane-lookup: no plane db at {db} — unreachable, not empty", file=sys.stderr)
        return 3
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only = 1")
        rows = conn.execute(SQL, (f"dispatch-log:{a.task_id}",)).fetchall()
    except sqlite3.Error as exc:
        print(f"plane-lookup: db unreadable: {exc}", file=sys.stderr)
        return 3
    finally:
        try:
            conn.close()
        except Exception:
            pass
    want = None
    if a.assignee:
        fl, _, name = a.assignee.rpartition("/")
        want = (fl.lower(), name.lower())
    for wi, asg, msg, alias in rows:
        if want and alias:
            fl, _, name = alias.rpartition("/")
            if (fl.lower(), name.lower()) != want:
                continue
        print(f"{wi} {asg} {msg or ''}".rstrip())
        return 0
    print(f"plane-lookup: no plane row for dispatch-log:{a.task_id}"
          f"{' / ' + a.assignee if a.assignee else ''} — not found (legacy fallback applies)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
