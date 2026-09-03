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

# The package twin of this read is queries.LATEST_ASSIGNMENT_BY_REF_SQL (the
# importer's report link and supersession closure ride it); a bash door cannot
# import the package, so the SQL lives twice - keep the two in step.
SQL = (
    "SELECT a.work_item_id, a.assignment_id, a.dispatch_msg_id, i.alias"
    " FROM assignments a"
    " LEFT JOIN identity_registry i ON i.uid = a.assignee_uid"
    " WHERE a.source_ref = ?"
    " ORDER BY a.ingest_seq DESC"
)


def _readers():
    """The stdlib plane readers beside this file — ONE read-only open (schema
    probe + transient retry) for every stdlib door (chunk 6a fold)."""
    import importlib.util
    src = os.path.join(os.path.dirname(os.path.realpath(__file__)), "plane-readers.py")
    spec = importlib.util.spec_from_file_location("plane_readers", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _open_idless(a) -> int:
    """`--open-idless --fleet F --bot B`: `<work_item_id> <assignment_id>` per
    OPEN id-less dispatch of the bot (cutover chunk 6a) — what report-back.sh
    closes on the bot's next terminal report, as the legacy ledger closes
    id-less rows by any later terminal report. Empty = nothing open (rc 0);
    unreachable = rc 3."""
    pr = _readers()
    try:
        conn = pr.connect(a.root)
    except pr.PlaneUnreachable as exc:
        print(f"plane-lookup: {exc} — unreachable, not empty", file=sys.stderr)
        return 3
    try:
        for wi, asg in pr.open_idless_assignments(conn, a.fleet, a.bot):
            print(f"{wi} {asg}")
    except sqlite3.Error as exc:
        print(f"plane-lookup: db unreadable: {exc}", file=sys.stderr)
        return 3
    finally:
        conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--task-id", default=None)
    ap.add_argument("--assignee", default=None,
                    help="bot:<fleet>/<name>; name part compared case-insensitively")
    ap.add_argument("--open-idless", action="store_true",
                    help="list the bot's OPEN id-less assignments (needs --fleet and --bot)")
    ap.add_argument("--fleet", default=None)
    ap.add_argument("--bot", default=None)
    a = ap.parse_args(argv)
    if not a.root:
        # An empty root is CLAUDLOBBY_ROOT unset in the caller: unreachable, not empty.
        print("plane-lookup: --root is empty (CLAUDLOBBY_ROOT unset?) — unreachable",
              file=sys.stderr)
        return 3
    if a.open_idless:
        if not (a.fleet and a.bot):
            ap.error("--open-idless needs --fleet and --bot")
        return _open_idless(a)
    if not a.task_id:
        ap.error("--task-id is required (or --open-idless)")
    pr = _readers()
    try:
        conn = pr.connect(a.root)            # no db / unopenable: unreachable, never empty
    except pr.PlaneUnreachable as exc:
        print(f"plane-lookup: {exc} — unreachable, not empty", file=sys.stderr)
        return 3
    try:
        rows = conn.execute(SQL, (f"dispatch-log:{a.task_id}",)).fetchall()
    except sqlite3.Error as exc:
        print(f"plane-lookup: db unreadable: {exc}", file=sys.stderr)
        return 3
    finally:
        conn.close()
    want = None
    if a.assignee:
        fl, _, name = a.assignee.rpartition("/")
        want = (fl.lower(), name.lower())
    for wi, asg, msg, alias in rows:
        if want:
            # Fail CLOSED: an assignee the registry cannot name is not a match.
            if not alias:
                continue
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
