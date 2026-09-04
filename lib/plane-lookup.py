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
import json
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


def _with_plane(root: str, fn) -> int:
    """The connect ladder every mode shares: unreachable → rc 3 (never empty),
    a fleet the plane never saw (the readers refuse) → rc 3, a db error → rc 3,
    a window start that is not an instant → rc 2; the connection closed either
    way. *fn(pr, conn)* prints the mode's answer and returns its rc."""
    pr = _readers()
    try:
        conn = pr.connect(root)
    except pr.PlaneUnreachable as exc:
        print(f"plane-lookup: {exc} — unreachable, not empty", file=sys.stderr)
        return 3
    try:
        return fn(pr, conn)
    except pr.PlaneUnreachable as exc:
        print(f"plane-lookup: {exc} — unreachable, not empty", file=sys.stderr)
        return 3
    except sqlite3.Error as exc:
        print(f"plane-lookup: db unreadable: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"plane-lookup: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def _open_idless(a) -> int:
    """`--open-idless --fleet F --bot B`: `<work_item_id> <assignment_id>` per
    OPEN id-less dispatch of the bot (cutover chunk 6a) — what report-back.sh
    closes on the bot's next terminal report, as the legacy ledger closes
    id-less rows by any later terminal report. Empty = nothing open (rc 0);
    unreachable = rc 3."""
    def fn(pr, conn):
        for wi, asg in pr.open_idless_assignments(conn, a.fleet, a.bot):
            print(f"{wi} {asg}")
        return 0
    return _with_plane(a.root, fn)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--task-id", default=None)
    ap.add_argument("--assignee", default=None,
                    help="bot:<fleet>/<name>; name part compared case-insensitively")
    ap.add_argument("--open-idless", action="store_true",
                    help="list the bot's OPEN id-less assignments (needs --fleet and --bot)")
    ap.add_argument("--declared", default=None, metavar="READER",
                    help="print the instant READER was declared cut over to the plane for --fleet"
                    " (cutover_declared), or nothing — the bash readers' half of a flip's two facts")
    ap.add_argument("--retired", action="store_true",
                    help="print the instant the fleet's legacy writes were retired, or nothing"
                    " (needs --fleet; chunk 6b — the doors' second fact)")
    ap.add_argument("--events", action="store_true",
                    help="print the fleet's events as legacy JSONL rows, oldest first (needs --fleet;"
                    " --since <iso> bounds; --bot / --type filter) — Phase B, the bot-events ledger from the plane")
    ap.add_argument("--escalation", action="store_true",
                    help="print '<bot> <type> <latest>' per (bot, critical type) landed strictly after"
                    " --since (needs --fleet; --type narrows to one type) — fleet-pulse's question, ONE read")
    ap.add_argument("--since", default=None,
                    help="an ISO instant; a naive one is the host's local clock (fleet-pulse's window)")
    ap.add_argument("--type", default=None)
    ap.add_argument("--door", default=None, choices=("dispatch", "report", "events", "workstreams"),
                    help="with --retired: the retirement must name this door's flag")
    ap.add_argument("--workstreams", action="store_true",
                    help="print the fleet's workstream registry JSON from the plane (needs --fleet;"
                    " --lease-days for a never-renewed lease) — cutover A2, the door's read side")
    ap.add_argument("--lease-days", type=int, default=14)
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
    if a.workstreams:
        if not a.fleet:
            ap.error("--workstreams needs --fleet")

        def fn(pr, conn):
            print(json.dumps(pr.workstream_registry(conn, a.fleet, lease_days=a.lease_days),
                             separators=(",", ":"), sort_keys=True))
            return 0
        return _with_plane(a.root, fn)
    if a.events or a.escalation:
        if not a.fleet:
            ap.error("--events / --escalation need --fleet")
        if a.escalation and not a.since:
            ap.error("--escalation needs --since <instant>")

        def fn(pr, conn):
            if a.escalation:
                for (bot, ev), at in sorted(pr.escalation(conn, a.fleet, a.since,
                                                          event_type=a.type).items()):
                    print(f"{bot} {ev} {at}")
            else:
                for row in pr.fleet_events(conn, a.fleet, since=a.since, bot=a.bot, event_type=a.type):
                    print(json.dumps(pr.public(row), separators=(",", ":")))
            return 0
        return _with_plane(a.root, fn)
    if a.retired or a.declared:
        if not a.fleet:
            ap.error("--retired / --declared need --fleet")

        def fn(pr, conn):
            at = (pr.retired(conn, a.fleet, a.door) if a.retired
                  else pr.declared(conn, a.fleet, a.declared))
            if at:
                print(at)
            return 0
        return _with_plane(a.root, fn)
    if not a.task_id:
        ap.error("--task-id is required (or --open-idless)")
    want = None
    if a.assignee:
        fl, _, name = a.assignee.rpartition("/")
        want = (fl.lower(), name.lower())

    def fn(pr, conn):
        rows = conn.execute(SQL, (f"dispatch-log:{a.task_id}",)).fetchall()
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
    return _with_plane(a.root, fn)


if __name__ == "__main__":
    sys.exit(main())
