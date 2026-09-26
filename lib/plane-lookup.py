#!/usr/bin/env python3
"""plane-lookup.py — the plane answered by TASK ID.

Every dispatch stamps ``source_ref = "dispatch-log:<task_id>"`` on the
plane work_item and assignment it emits — a stable ref shape, the only
thing the name still means — and two doors resolve through it:
``report-back.sh`` recovers the work_item / assignment ids for its own
emission, and ``dispatch-task.sh --supersedes`` needs the superseded
dispatch's plane ids to set ``supersedes_msg_id`` and emit a terminal
``superseded`` event, and ``task-act.sh`` resolves the row a manager is
withdrawing or escalating (``--all-open``, which refuses to pick for it).

Stdlib-only, like ``dispatch-overdue.py`` — a bash door must not pay the
package import on every call. Read-only (``mode=ro`` + ``query_only``).

Output: ``<work_item_id> <assignment_id> <dispatch_msg_id>`` for the LATEST
matching assignment (by ingest order), or nothing. Exit codes follow the
unreachable ≠ empty rule (source_state): 0 found · 0 not-found (empty
stdout, a note on stderr — a stamped id is NOT proof the row exists; the
caller says so and carries on) · 3 unreachable (no db, or unopenable) ·
2 usage.
"""

from __future__ import annotations

import argparse
import os
import json
import sqlite3
import sys
import time

# The package twin of this read went with the importer (F18 R3); the dispatch
# door's supersession closure and the report door's link ride this stdlib read
# (a bash door cannot import the package).
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


def _assignee_key(alias):
    """The (fleet, name) an `--assignee` value and a registry alias compare
    by — lowercased, the legacy grep's own case-insensitivity. None for an
    absent value; an alias the registry could not name is NOT a match (fail
    CLOSED), which the caller gets by comparing None against a real key."""
    if not alias:
        return None
    fl, _, name = alias.rpartition("/")
    return (fl.lower(), name.lower())


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


def _all_open(a) -> int:
    """`--task-id <id> --all-open`: `<work_item_id> <assignment_id>
    <dispatch_msg_id|-> <assignee_alias|-> <fleet_alias|->` for EVERY open
    assignment carrying the id, newest first (chunk M-A, #1481).

    The plain `--task-id` mode answers the LATEST match and stops, which is
    right for the two doors that already know the assignee. `task-act.sh`
    does not — a manager holds task ids, not the roster — so it needs the
    whole open set to refuse an ambiguous id by NAMING the rows instead of
    silently acting on the newest. Empty = nothing open under that id, which
    is not the same fact as no such id; the caller says which.

    `--assignee` NARROWS it, the same fail-closed rule the plain mode applies
    (an assignee the registry cannot name is not a match) — the fold's F5:
    the flag was accepted and silently ignored here, so a caller that thought
    it had disambiguated got the whole ambiguous set back. The acts' own
    `--assignment <asg_id>` narrows THIS list in the caller rather than
    querying by assignment, so the row acted on provably carries the task id
    the caller named."""
    def fn(pr, conn):
        rows = pr.open_assignments_for_task(conn, a.task_id)
        want = _assignee_key(a.assignee)
        for r in rows:
            if want and _assignee_key(r["assignee"]) != want:
                continue
            print(f"{r['work_item_id']} {r['assignment_id']}"
                  f" {r['dispatch_msg_id'] or '-'} {r['assignee'] or '-'}"
                  f" {r['fleet'] or '-'}")
        return 0
    return _with_plane(a.root, fn)


def _by_assignment(a) -> int:
    """`--by-assignment <asg_id> [--any-state]`: `<work_item_id>
    <assignment_id> <dispatch_msg_id|-> <assignee|-> <fleet|-> <source_ref|->`
    for the one assignment, or nothing. OPEN only unless `--any-state`.

    The acts' asg-first door (#1492): the re-check digest hands a manager
    `asg_` ids, and an id-less row has no task id to name — its real key is
    the content hash in `source_ref`. `task-act.sh` resolves the `asg_` id to
    that key here and acts through the by-task-id path, so the act stamps the
    row's real `dispatch-log:sha:<hex>` rather than a fabricated
    `dispatch-log:asg_...`; the refusal reads `--any-state` to name the sha
    key even for a row it will not act on. Empty = no such (open) assignment
    (rc 0); unreachable = rc 3."""
    def fn(pr, conn):
        row = pr.assignment_by_id(conn, a.by_assignment, open_only=not a.any_state)
        if row is not None:
            print(f"{row['work_item_id']} {row['assignment_id']}"
                  f" {row['dispatch_msg_id'] or '-'} {row['assignee'] or '-'}"
                  f" {row['fleet'] or '-'} {row['source_ref'] or '-'}")
        return 0
    return _with_plane(a.root, fn)


def _checkin_id(a) -> int:
    """`--checkin-id ck_<32hex>`: print the id when a `checkin_decision` system
    event carries `source_ref = checkin:<id>` (manager check-in spec §7), else
    nothing plus a stderr note -- the `--task-id` contract: a stamped id is not
    proof the row exists, the caller says so and carries on. Unreachable = rc 3."""
    def fn(pr, conn):
        row = conn.execute(
            "SELECT 1 FROM events WHERE kind = 'system' AND event = 'checkin_decision'"
            " AND source_ref = ? LIMIT 1", (f"checkin:{a.checkin_id}",)).fetchone()
        if row is None:
            print(f"plane-lookup: no checkin_decision with id {a.checkin_id}", file=sys.stderr)
            return 0
        print(a.checkin_id)
        return 0
    return _with_plane(a.root, fn)


def _received(a) -> int:
    """`--received <msg_id> --destination <bot> [--wait S]`: has the RECEIVER
    recorded this tracked send? plane-dispatch-in.sh writes the `received` row
    only when the prompt is actually SUBMITTED, so a payload held in the input
    box leaves none (#1099). Polls up to S seconds: rc 0 once the row lands
    addressed to <bot> (fold F3, queries.DELIVERY_STATUS_SQL's rule: a prompt
    that merely QUOTES the trailer files it under another bot), rc 1 when none
    has by then, rc 4 when <bot> has never recorded a receipt
    at all (its hook is not armed, so an absence proves nothing), rc 3
    unreachable.

    --verdict (#1876) also prints `<verdict> <sender>` once the receipt is
    found: the plane's delivery verdict for this msg id and the alias that
    RECORDED sending it. It is what a receiver checks before treating a
    paste-framed dispatch as its manager's: a trailer is plain text and the hook
    records a receipt for any prompt that ends in one, so only a recorded send
    whose verdict is `delivered` proves the bytes are the sender's. The verdict
    is waited on too, within the same --wait: most receipts land BEFORE the
    sender's own wire proof (193 of 247 on the Pi), and until it lands there is
    nothing to compare. `unknown -` = no send recorded under this id; a verdict
    still unconfirmed when the wait runs out prints as it stands. No --verdict,
    no output: pane_await_receipt reads the exit code alone."""
    deadline = time.monotonic() + a.wait

    def say(pr, conn):
        v = None
        while True:
            row = conn.execute(pr.DELIVERY_SQL.format(ph="?"), (a.received,)).fetchone()
            v = row[3] if row else None
            if v in ("delivered", "truncated", "altered") or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        who = conn.execute("SELECT sender_alias FROM communications WHERE msg_id = ?",
                           (a.received,)).fetchone()
        print(f"{v or 'unknown'} {who[0] if who else '-'}")
        return 0

    def fn(pr, conn):
        sql = ("SELECT 1 FROM events WHERE kind = 'transmission' AND event = 'received'"
               " AND json_extract(detail, '$.destination') = ? {} ORDER BY ingest_seq DESC LIMIT 1")

        def got():
            return conn.execute(sql.format("AND msg_id = ?"), (a.destination, a.received)).fetchone()
        if got():
            return say(pr, conn) if a.verdict else 0
        if not conn.execute(sql.format(""), (a.destination,)).fetchone():
            return 4
        while time.monotonic() < deadline:
            time.sleep(0.25)
            if got():
                return say(pr, conn) if a.verdict else 0
        return 1
    return _with_plane(a.root, fn)


def _escalated(a) -> int:
    """`--escalated --fleet F`: `<assignment_id> <task_id> <by> <occurred_at>
    <question>` per OPEN escalation, oldest first, TAB-separated so a question
    containing spaces survives the read; newlines are stripped for the same
    reason. Empty = nothing escalated (rc 0); unreachable = rc 3."""
    def fn(pr, conn):
        for row in pr.escalated_rows(conn, a.fleet):
            question = " ".join((row["question"] or "").split())
            print("\t".join((row["assignment_id"], row["task_id"], row["by"],
                             row["occurred_at"], question)))
        return 0
    return _with_plane(a.root, fn)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--task-id", default=None)
    ap.add_argument("--assignee", default=None,
                    help="bot:<fleet>/<name>; name part compared case-insensitively")
    ap.add_argument("--all-open", action="store_true",
                    help="--task-id: print EVERY open assignment carrying the id, not just the"
                    " latest (the task-loop acts refuse an ambiguous id rather than guessing);"
                    " --assignee narrows it")
    ap.add_argument("--escalated", action="store_true",
                    help="print the fleet's OPEN escalations, tab-separated (needs --fleet) —"
                    " fleet-pulse.sh's `_task_escalations` reads this for the `escalated` task"
                    " event and pages the operator once per assignment (chunk M-B)")
    ap.add_argument("--open-idless", action="store_true",
                    help="list the bot's OPEN id-less assignments (needs --fleet and --bot)")
    ap.add_argument("--by-assignment", default=None,
                    help="resolve ONE assignment by its asg id to '<wi> <asg> <msg|-> <assignee|->"
                    " <fleet|-> <source_ref|->' (the acts' asg-first door #1492); OPEN only unless"
                    " --any-state")
    ap.add_argument("--any-state", action="store_true",
                    help="--by-assignment: match a CLOSED assignment too (the refusal reads this to"
                    " name the sha key of a row it will not act on)")
    ap.add_argument("--checkin-id", default=None,
                    help="print the id when a checkin_decision carries source_ref checkin:<id>, else"
                    " nothing + a note (dispatch-task.sh --checkin asks before it joins)")
    ap.add_argument("--events", action="store_true",
                    help="print the fleet's events as legacy JSONL rows, oldest first (needs --fleet;"
                    " --since <iso> bounds; --bot / --type filter) — Phase B, the bot-events ledger from the plane")
    ap.add_argument("--escalation", action="store_true",
                    help="print '<bot> <type> <latest>' per (bot, critical type) landed strictly after"
                    " --since (needs --fleet; --type narrows to one type) — fleet-pulse's question, ONE read")
    ap.add_argument("--since", default=None,
                    help="an ISO instant; a naive one is the host's local clock (fleet-pulse's window)")
    ap.add_argument("--type", default=None)
    ap.add_argument("--workstreams", action="store_true",
                    help="print the fleet's workstream registry JSON from the plane (needs --fleet;"
                    " --lease-days for a never-renewed lease) — cutover A2, the door's read side")
    ap.add_argument("--lease-days", type=int, default=14)
    ap.add_argument("--or-empty", action="store_true",
                    help="--workstreams: a fleet the plane holds no identity for renders the EMPTY"
                         " registry instead of refusing (the writer's first open of a fresh fleet)")
    ap.add_argument("--received", default=None,
                    help="rc 0 once the receiver's `received` row for this msg id is on the plane, rc 1"
                    " when none by --wait, rc 4 when --destination never recorded one (#1099)")
    ap.add_argument("--destination", default=None)
    ap.add_argument("--verdict", action="store_true",
                    help="--received: also print `<verdict> <sender>` for a found receipt — the"
                    " check a receiver runs before trusting a paste-framed dispatch (#1876)")
    ap.add_argument("--wait", type=float, default=0)
    ap.add_argument("--fleet", default=None)
    ap.add_argument("--bot", default=None)
    a = ap.parse_args(argv)
    if not a.root:
        # An empty root is CLAUDLOBBY_ROOT unset in the caller: unreachable, not empty.
        print("plane-lookup: --root is empty (CLAUDLOBBY_ROOT unset?) — unreachable",
              file=sys.stderr)
        return 3
    if a.checkin_id:
        return _checkin_id(a)
    if a.received:
        return _received(a)   # no --destination matches no receipt: rc 4, no verdict
    if a.escalated:
        if not a.fleet:
            ap.error("--escalated needs --fleet")
        return _escalated(a)
    if a.open_idless:
        if not (a.fleet and a.bot):
            ap.error("--open-idless needs --fleet and --bot")
        return _open_idless(a)
    if a.by_assignment:
        return _by_assignment(a)
    if a.workstreams:
        if not a.fleet:
            ap.error("--workstreams needs --fleet")
        if a.or_empty and not os.path.exists(os.path.join(a.root, "state", "plane", "plane.db")):
            # The WRITER's first verb on a host whose plane has not been created
            # yet — the first emission creates it, under this same root, so the
            # writer trusts the root exactly as far as its own emit will: an
            # existing directory renders the empty registry, anything else refuses.
            if os.path.isdir(a.root):
                print(json.dumps(_readers().EMPTY_REGISTRY, separators=(",", ":"), sort_keys=True))
                return 0
            print(f"plane-lookup: {a.root} is not a directory — unreachable, not empty", file=sys.stderr)
            return 3

        def fn(pr, conn):
            print(json.dumps(pr.workstream_registry(conn, a.fleet, lease_days=a.lease_days, or_empty=a.or_empty),
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
    if not a.task_id:
        ap.error("--task-id is required (or --open-idless / --escalated)")
    if a.all_open:
        return _all_open(a)
    want = _assignee_key(a.assignee)

    def fn(pr, conn):
        rows = conn.execute(SQL, (f"dispatch-log:{a.task_id}",)).fetchall()
        for wi, asg, msg, alias in rows:
            if want:
                # Fail CLOSED: an assignee the registry cannot name is not a match.
                if _assignee_key(alias) != want:
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
