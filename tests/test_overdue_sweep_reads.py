"""The overdue sweep's plane reads seek, and answer as they did (#2549).

`dispatch-overdue.py --all` runs the open test once per uid and the bot's last
progress once per bot, on every pulse. Under one `OR`, the open test's three
ways a row closes could seek none of their columns, so it walked every task
event for each assignment the bot had held; with two or more uids, the
progress read walked every system event in the plane. Each rewrite is checked
twice: its plan seeks, and it returns exactly what the statement it replaced
returned, on scenes built for the edges (ties on time, legacy refs, a bot with
no events, closed rows). The replaced statements are kept here as the oracle.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from claudlobby.plane import queries
from claudlobby.plane.emit_api import emit_batch
from claudlobby.task_state import TASK_EMITTER
from tests.plane_fixtures import F, NOW_EPOCH, _stdlib_readers, plane_root, ro

PR = _stdlib_readers()
# The open test before #2549: one NOT EXISTS over an OR of the three ways a row closes.
OLD_CLOSURE = (
    "  AND NOT EXISTS (SELECT 1 FROM events t WHERE t.kind='task'"
    "    AND t.event IN " + PR._TERMINAL +
    " AND (? IS NULL OR t.occurred_at <= ?)"
    "    AND (t.assignment_id = a.assignment_id"
    "      OR (a.source_ref LIKE 'dispatch-log:%' AND a.source_ref NOT LIKE 'dispatch-log:sha:%'"
    "          AND t.assignment_id IN (SELECT s.assignment_id FROM assignments s"
    "            WHERE s.assignee_uid = a.assignee_uid AND s.source_ref = a.source_ref"
    "              AND (? IS NULL OR s.occurred_at <= ?)))"
    "      OR (t.emitter='claudlobby.tasks.v1' AND t.event='cancelled'"
    "          AND t.assignment_id IS NULL AND t.work_item_id=a.work_item_id"
    "          AND t.ingest_seq>a.ingest_seq)))"
)
# The bot's last progress before #2549.
OLD_LAST_PROGRESS_SQL = (
    "SELECT MAX(t) FROM ("
    "SELECT e.occurred_at AS t FROM events e WHERE e.kind = 'task' AND e.event = 'progress'"
    " AND e.actor_uid IN (%s) AND e.occurred_at <= ?"
    " UNION ALL"
    " SELECT e.occurred_at FROM events e WHERE e.kind = 'system' AND e.event = 'report_status'"
    " AND e.subject_uid IN (%s) AND json_extract(e.detail, '$.status') = 'progress' AND e.occurred_at <= ?)"
)
# A step that walks: a scan, or a seek on the kind alone.
WALK = re.compile(r"^SCAN|\(kind=\?\)")


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(NOW_EPOCH + seconds, timezone.utc).isoformat()


def _as_of(sql: str, key: str, at):
    """The binds of a statement whose first `?` is its key (a uid, a fleet uid)
    and every other `?` the as-of instant, as both open statements bind."""
    return (key,) + (at,) * (sql.count("?") - 1)


def _old(sql: str) -> str:
    assert sql.count(PR._SIBLING_CLOSURE) == 1, "the open test is no longer one fragment"
    return sql.replace(PR._SIBLING_CLOSURE, OLD_CLOSURE)


def _steps(conn, sql: str, params, aliases: str) -> list[str]:
    plan = [r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    # "SEARCH t" on SQLite 3.36 and later, "SEARCH TABLE events AS t" before it
    return [d for d in plan if re.match(rf"(SEARCH|SCAN) (TABLE \w+ AS )?[{aliases}]\b", d)]


def _emit(root, events):
    assert all(r.status == "committed" for r in emit_batch(root, events, require_commit=True))


def _assign(n, bot, seconds, *, ref, wi=None, expected_by=None):
    """A work item (unless *wi* names one already landed) and one assignment of
    it to *bot*; *ref* None lands no source_ref, the legacy shape."""
    wi_id = wi or f"wi_{n:0>32}"
    asg = f"asg_{n:0>32}"
    ts = _at(seconds)
    common = {"emitter": "dispatch-task", "fleet": F, "occurred_at": ts,
              **({"source_ref": ref} if ref is not None else {})}
    events = [] if wi else [{"event_type": "work_item", **common,
                             "payload": {"work_item_id": wi_id, "title": "t", "created_by": f"bot:{F}/mgr"}}]
    events.append({"event_type": "assignment", **common,
                   "payload": {"assignment_id": asg, "work_item_id": wi_id, "assignee": f"bot:{F}/{bot}",
                               "assigned_by": f"bot:{F}/mgr",
                               **({"expected_by": _at(expected_by)} if expected_by is not None else {})}})
    return wi_id, asg, events


def _task(wi, asg, event, seconds, *, actor="bot:f/w1", emitter="report-back", **extra):
    return {"event_type": "task", "emitter": emitter, "fleet": F,
            "source_ref": f"report-back:msg_{wi[-6:]}{event}{seconds}", "occurred_at": _at(seconds),
            "payload": {"work_item_id": wi, "assignment_id": asg, "event": event, "actor": actor, **extra}}


def _marker(subject, seconds, data):
    return {"event_type": "system", "emitter": "report-back", "fleet": F,
            "source_ref": f"report:msg_{subject[-3:]}{seconds}", "occurred_at": _at(seconds),
            "payload": {"event": "report_status", "subject_kind": "actor", "subject": subject, "data": data}}


# --- the open test -----------------------------------------------------------------

def _open_scene(tmp_path):
    """Every way a row closes, and the near misses beside each. Returns the
    root and {name: assignment_id}."""
    root = plane_root(tmp_path, initialize=True)
    events, ids = [], {}

    def assign(name, n, bot, seconds, **kw):
        wi, asg, evs = _assign(n, bot, seconds, **kw)
        events.extend(evs)
        ids[name] = asg
        return wi, asg

    assign("open", 1, "w1", -36000, ref="dispatch-log:t-1", expected_by=-3600)
    wi, asg = assign("own_completed", 2, "w1", -35000, ref="dispatch-log:t-2")
    events.append(_task(wi, asg, "completed", -30000))
    # ties on time: two rows dispatched in one instant, one closed in that instant
    wi, asg = assign("tie_failed", 3, "w1", -20000, ref="dispatch-log:t-3")
    events.append(_task(wi, asg, "failed", -20000))
    assign("tie_open", 4, "w1", -20000, ref="dispatch-log:t-4")
    # a re-dispatch under one id'd ref: the second row's terminal closes the first
    assign("sibling_first", 5, "w1", -18000, ref="dispatch-log:t-5")
    wi, asg = assign("sibling_second", 6, "w1", -14400, ref="dispatch-log:t-5")
    events.append(_task(wi, asg, "completed", -12600))
    # the same ref on another assignee closes nothing of w1's
    assign("other_assignee", 7, "w1", -17000, ref="dispatch-log:t-7")
    wi, asg = assign("other_assignee_peer", 8, "w2", -16000, ref="dispatch-log:t-7")
    events.append(_task(wi, asg, "completed", -15000, actor="bot:f/w2"))
    # an id-less ref never closes a sibling
    assign("idless_first", 9, "w1", -16500, ref="dispatch-log:sha:k9")
    wi, asg = assign("idless_second", 10, "w1", -16400, ref="dispatch-log:sha:k9")
    events.append(_task(wi, asg, "completed", -16300))
    # LIKE ignores case, in the old statement and the new
    assign("upper_first", 11, "w1", -16200, ref="DISPATCH-LOG:t-11")
    wi, asg = assign("upper_second", 12, "w1", -16100, ref="DISPATCH-LOG:t-11")
    events.append(_task(wi, asg, "expired", -16000))
    # a work-item cancel closes the rows before it, not the row assigned after it
    wi13, _ = assign("cancelled_before", 13, "w1", -15500, ref="request:wi13")
    events.append(_task(wi13, None, "cancelled", -15400, actor="bot:f/mgr", emitter=TASK_EMITTER))
    assign("assigned_after_cancel", 14, "w1", -15300, ref="request:wi13", wi=wi13)
    # a cancel from another emitter, or on another assignment, is not the work item's
    wi15, _ = assign("foreign_cancel", 15, "w1", -15200, ref="request:wi15")
    events.append(_task(wi15, None, "cancelled", -15100, actor="bot:f/mgr"))
    wi16, asg16 = assign("own_cancel", 16, "w1", -15000, ref="request:wi16")
    assign("own_cancel_peer", 17, "w1", -14900, ref="request:wi16b", wi=wi16)
    events.append(_task(wi16, asg16, "cancelled", -14800, actor="bot:f/mgr", emitter=TASK_EMITTER))
    # non-terminal facts close nothing; a deadline fact moves the deadline column
    wi, asg = assign("busy", 18, "w1", -14000, ref="dispatch-log:t-18", expected_by=-7200)
    events += [_task(wi, asg, "accepted", -13900), _task(wi, asg, "progress", -13800, progress=10),
               _task(wi, asg, "deadline_changed", -13700, actor="bot:f/mgr", emitter=TASK_EMITTER,
                     by="bot:f/mgr", reason="r", deadline=_at(3600))]
    # legacy rows: no source_ref at all
    assign("legacy_open", 19, "w1", -13000, ref=None)
    wi, asg = assign("legacy_closed", 20, "w1", -12900, ref=None)
    events.append(_task(wi, asg, "returned_blocked", -12800))
    wi, asg = assign("reassigned", 21, "w1", -12700, ref="request:wi21")
    events.append(_task(wi, asg, "reassigned", -12600, actor="bot:f/mgr", emitter=TASK_EMITTER))
    # a case-variant alias is a second uid: its row's sibling on the first uid closes nothing
    assign("variant_uid", 22, "W1", -12000, ref="dispatch-log:t-22")
    wi, asg = assign("variant_uid_peer", 23, "w1", -11900, ref="dispatch-log:t-22")
    events.append(_task(wi, asg, "completed", -11800))
    # a terminal fact still to come, as of most instants below
    wi, asg = assign("closes_later", 24, "w1", -11000, ref="dispatch-log:t-24")
    events.append(_task(wi, asg, "superseded", -60, actor="bot:f/mgr", emitter=TASK_EMITTER))
    # w3 holds a row and no event at all
    assign("quiet_bot", 25, "w3", -10000, ref="dispatch-log:t-25")
    _emit(root, events)
    return root, ids


def _instants(conn) -> list:
    """None, and every recorded instant with a microsecond either side."""
    found = {r[0] for r in conn.execute("SELECT occurred_at FROM events UNION SELECT occurred_at FROM assignments")}
    out: set = set()
    for iso in found:
        moment = datetime.fromisoformat(iso).timestamp()
        out.update(datetime.fromtimestamp(moment + d, timezone.utc).isoformat() for d in (-1e-6, 0, 1e-6))
        out.add(iso)
    return [None] + sorted(out)


def test_the_open_test_closes_exactly_what_the_old_statement_closed(tmp_path):
    root, ids = _open_scene(tmp_path)
    with ro(root) as conn:
        roster = PR.roster(conn, F)
        uids = [u for e in roster.values() for u in e["uids"]] + ["actor_nobody"]
        new = queries.OPEN_ASSIGNMENTS_AT_SQL
        old = _old(new)
        # the scene exercises every way a row closes, and every near miss beside each
        now_open = {r[2] for uid in uids for r in conn.execute(old, _as_of(old, uid, None))}
        closed = {"own_completed", "tie_failed", "sibling_first", "sibling_second", "idless_second",
                  "upper_first", "upper_second", "cancelled_before", "own_cancel", "legacy_closed",
                  "reassigned", "variant_uid_peer", "closes_later", "other_assignee_peer"}
        assert {n for n, a in ids.items() if a not in now_open} == closed
        instants = _instants(conn)
        compared = 0
        for uid in uids:
            for at in instants:
                assert conn.execute(new, _as_of(new, uid, at)).fetchall() == \
                    conn.execute(old, _as_of(old, uid, at)).fetchall(), (uid, at)
                compared += 1
        assert compared == len(uids) * len(instants) and len(instants) > 50


def test_the_fleet_open_rows_match_the_old_statement(tmp_path):
    root, _ids = _open_scene(tmp_path)
    with ro(root) as conn:
        fleet_uid = PR.fleet_uid(conn, F)
        new = PR.FLEET_OPEN_SQL
        old = _old(new)
        for at in _instants(conn):
            assert conn.execute(new, _as_of(new, fleet_uid, at)).fetchall() == \
                conn.execute(old, _as_of(old, fleet_uid, at)).fetchall(), at
        rows = PR.fleet_open_rows(conn, F)          # the door's own binds still fit
        assert len(rows) == len(conn.execute(old, _as_of(old, fleet_uid, None)).fetchall()) > 0


@pytest.mark.parametrize("statement", ["OPEN_ASSIGNMENTS_AT_SQL", "FLEET_OPEN_SQL"])
def test_the_open_test_seeks_each_way_a_row_closes(tmp_path, statement):
    """One terminal test per way a row closes, each seeking its own column:
    the row's own events, its siblings' through the assignments that share its
    ref, the work item's cancels after it. None walks the task events by kind."""
    root = plane_root(tmp_path, initialize=True)
    sql = queries.OPEN_ASSIGNMENTS_AT_SQL if statement == "OPEN_ASSIGNMENTS_AT_SQL" else PR.FLEET_OPEN_SQL
    with ro(root) as conn:
        steps = _steps(conn, sql, _as_of(sql, "actor_x", None), "ts")
    walks = [d for d in steps if WALK.search(d)]
    assert len([d for d in steps if re.match(r"\S+ (TABLE events AS )?t\b", d)]) == 3 and not walks, steps


# --- the bot's last progress -----------------------------------------------------------

def _progress_scene(tmp_path):
    root = plane_root(tmp_path, initialize=True)
    events = []
    for n, bot in ((1, "w1"), (2, "W1"), (3, "w2"), (4, "w3")):
        events += _assign(n, bot, -40000, ref=f"dispatch-log:t-{n}")[2]
    w1, w1b, w2, mgr = "bot:f/w1", "bot:f/W1", "bot:f/w2", "bot:f/mgr"
    wi, asg = f"wi_{1:0>32}", f"asg_{1:0>32}"
    events += [
        _task(wi, asg, "progress", -18000, actor=w1, progress=5),
        _task(wi, asg, "progress", -14400, actor=w1b, progress=6),
        _task(wi, asg, "completed", -3600, actor=w1),               # not progress
        _task(wi, asg, "progress", -1800, actor=mgr, progress=7),    # another actor's
        _marker(w1b, -10800, {"status": "progress"}),
        _marker(w1, -7200, {"status": "completed"}),                 # not progress
        _marker(w1, -14400, {"status": "progress"}),                 # ties the task event above
        _marker(w1, -600, {}),                                       # a legacy marker with no status
        _marker(w1, 3600, {"status": "progress"}),                   # still to come
        {"event_type": "system", "emitter": "report-back", "fleet": F, "occurred_at": _at(-300),
         "payload": {"event": "progress", "subject_kind": "actor", "subject": w1}},   # not report_status
        _marker(w2, -21600, {"status": "progress"}),
    ]
    _emit(root, events)
    return root


def test_the_last_progress_is_the_old_statements_answer(tmp_path):
    root = _progress_scene(tmp_path)
    with ro(root) as conn:
        roster = PR.roster(conn, F)
        u1, u2 = roster["w1"]["uids"]
        sets = [[u1], [u2], [u1, u2], [u2, u1], [u1, u2, "actor_nobody"], roster["w2"]["uids"],
                roster["w3"]["uids"], ["actor_nobody"]]
        answers = set()
        for uids in sets:
            marks = ",".join("?" * len(uids))
            for at in _instants(conn)[1:]:
                params = (*uids, at, *uids, at)
                new = conn.execute(PR.LAST_PROGRESS_SQL % (marks, marks), params).fetchone()
                assert new == conn.execute(OLD_LAST_PROGRESS_SQL % (marks, marks), params).fetchone(), (uids, at)
                answers.add(new[0])
        # each arm wins somewhere, and a bot with no progress reads None
        assert {_at(-18000), _at(-14400), _at(-10800), _at(3600), _at(-21600), None} <= answers


@pytest.mark.parametrize("uids", [1, 2, 3, 5])
def test_the_last_progress_read_seeks_each_uid(tmp_path, uids):
    """Both halves seek each uid: the bot's own task events by actor, its
    report markers by subject. Neither walks a kind slice, however many uids
    the bot's aliases minted."""
    root = plane_root(tmp_path, initialize=True)
    marks = ",".join("?" * uids)
    with ro(root) as conn:
        steps = _steps(conn, PR.LAST_PROGRESS_SQL % (marks, marks),
                       ("u",) * uids + (None,) + ("u",) * uids + (None,), "e")
    assert len(steps) == 2 and not [d for d in steps if WALK.search(d)], steps

