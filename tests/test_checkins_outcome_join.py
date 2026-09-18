# tests/test_checkins_outcome_join.py
"""`claudlobby checkins` — the outcome join (manager check-in PR 3, task 1):
each decision row gains its `checkin_dispatch` join rows, resolved to the
dispatched task's status through the plane's own `TASK_STATUS_SQL` and
bucketed into a small reader-facing vocabulary. Seeded through the real emit
spine, in the shape `lib/dispatch-task.sh --checkin` lands (work_item +
assignment + one `checkin_dispatch` system event, actor-anchored on the
dispatcher) — see `test_checkin_doors.py::test_dispatch_checkin_appends_the_
join_row_to_the_same_batch` for the real door's own proof of that shape.

Fixtures (`_Args`, `_decision`, `_ago`, `_out`, `F`, `root`) are PR 1's own,
imported rather than re-declared — `root` is `autouse`, so importing it into
this module's namespace is enough for pytest to apply it here too."""

from __future__ import annotations

import pytest

from claudlobby.commands import checkins as cmd
from claudlobby.plane import queries
from claudlobby.plane.db import open_ro
from claudlobby.plane.emit_api import emit_batch
from tests.test_checkins_cli import F, _Args, _ago, _decision, _out, root  # noqa: F401

CK1, CK2, CK3, CK4, CK5, CK6, CK7, CK8 = ("ck_" + c * 32 for c in "abcdefgh")
F2 = "other-fleet"          # a second, equally fake fleet — #526's class

_SEQ = [0]


def _dispatched(root, ck: str, *, task_id, terminal: str | None = None):
    """One dispatch the way `dispatch-task.sh --checkin` lands it: work_item +
    assignment (dispatch_msg_id minted, unsent by default -- no transmission
    row unless the caller adds one) + the checkin_dispatch system event, in
    ONE emit_batch, actor-anchored on the DISPATCHER (subject_kind: actor,
    subject: bot:<F>/mgr) — the real door's own shape (lib/dispatch-task.sh,
    search checkin_dispatch). *terminal*, when given, appends a task event of
    that name in a second emit_batch — any TASK_EVENTS token, not only a
    terminal one: `progress` rides the same path, since TASK_STATUS_SQL falls
    back to the newest task event when none is terminal. Returns
    (work_item_id, assignment_id, msg_id)."""
    _SEQ[0] += 1
    n = _SEQ[0]
    wi, asg, msg = f"wi_{n:0>32}", f"asg_{n:0>32}", f"msg_{n:0>32}"
    ref = f"dispatch-log:{ck}-{n}"
    ts = _ago(hours=1)
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "dispatch-task", "fleet": F,
         "source_ref": ref, "occurred_at": ts,
         "payload": {"work_item_id": wi, "title": "fix the feed",
                     "created_by": f"bot:{F}/mgr"}},
        {"event_type": "assignment", "emitter": "dispatch-task", "fleet": F,
         "source_ref": ref, "occurred_at": ts,
         "payload": {"assignment_id": asg, "work_item_id": wi,
                     "assignee": f"bot:{F}/w1", "assigned_by": f"bot:{F}/mgr",
                     "dispatch_msg_id": msg}},
        {"event_type": "system", "emitter": "dispatch-task", "fleet": F,
         "source_ref": ref, "occurred_at": ts,
         "payload": {"event": "checkin_dispatch", "subject_kind": "actor",
                     "subject": f"bot:{F}/mgr",
                     "data": {"checkin_id": ck, "assignment_id": asg,
                              "work_item_id": wi, "task_id": task_id}}},
    ])
    if terminal:
        emit_batch(root, [
            {"event_type": "task", "emitter": "dispatch-task", "fleet": F,
             "source_ref": ref, "occurred_at": _ago(minutes=30),
             "payload": {"work_item_id": wi, "assignment_id": asg,
                         "event": terminal, "actor": f"bot:{F}/w1"}},
        ])
    return wi, asg, msg


def test_a_decision_carries_its_dispatch_with_the_planes_own_status(root, capsys):
    _decision(root, "mgr", CK1, age_h=1, action="dispatch")
    wi, asg, msg = _dispatched(root, CK1, task_id="t-shop-1", terminal="completed")
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = _out(capsys)["checkins"]
    assert len(rows) == 1
    dispatches = rows[0]["dispatches"]
    assert len(dispatches) == 1
    d = dispatches[0]
    assert d["task_id"] == "t-shop-1"
    assert d["assignment_id"] == asg
    assert d["work_item_id"] == wi
    assert d["status"] == "completed"
    assert d["outcome"] == "completed"
    assert d["terminal_at"] is not None


def test_every_terminal_task_event_has_a_bucket():
    assert set(queries.TERMINAL_TASK_EVENTS) <= set(cmd._OUTCOME)


# (shape, task-event name or None, the raw status TASK_STATUS_SQL resolves, the bucket)
_VOCAB_CASES = [
    ("task", "completed", "completed", "completed"),
    ("task", "returned_blocked", "returned_blocked", "blocked"),
    ("task", "failed", "failed", "failed"),
    ("task", "expired", "expired", "failed"),
    ("task", "cancelled", "cancelled", "retired"),
    ("task", "superseded", "superseded", "retired"),
    ("task", "reassigned", "reassigned", "retired"),
    ("txfail", None, "dispatch_failed", "failed"),
    ("task", "progress", "progress", "open"),
    ("unsent", None, "created_not_sent", "open"),
]


@pytest.mark.parametrize("shape,event,status,outcome", _VOCAB_CASES,
                         ids=[c[2] for c in _VOCAB_CASES])
def test_the_buckets_map_the_plane_vocabulary(root, capsys, shape, event, status, outcome):
    ck = "ck_" + "9" * 32
    _decision(root, "mgr", ck, age_h=1, action="dispatch")
    if shape == "task":
        _dispatched(root, ck, task_id="t-v", terminal=event)
    elif shape == "txfail":
        wi, asg, msg = _dispatched(root, ck, task_id="t-v")
        emit_batch(root, [{
            "event_type": "transmission", "emitter": "dispatch-task", "fleet": F,
            "occurred_at": _ago(minutes=30),
            "payload": {"msg_id": msg, "attempt_no": 1, "carrier": "tmux",
                        "destination": "w1", "state": "failed"}}])
    else:  # unsent -- an assignment with no transmission row at all
        _dispatched(root, ck, task_id="t-v")
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    d = _out(capsys)["checkins"][0]["dispatches"][0]
    assert d["status"] == status and d["outcome"] == outcome


def test_an_id_less_dispatch_still_resolves_through_the_assignment(root, capsys):
    _decision(root, "mgr", CK2, age_h=1, action="dispatch")
    _dispatched(root, CK2, task_id=None, terminal="completed")
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    d = _out(capsys)["checkins"][0]["dispatches"][0]
    assert d["task_id"] is None
    assert d["status"] == "completed" and d["outcome"] == "completed"


def test_a_join_row_naming_no_assignment_is_unjoined_never_open(root, capsys):
    _decision(root, "mgr", CK3, age_h=1, action="dispatch")
    ref = f"dispatch-log:{CK3}-orphan"
    emit_batch(root, [{
        "event_type": "system", "emitter": "dispatch-task", "fleet": F,
        "source_ref": ref, "occurred_at": _ago(hours=1),
        "payload": {"event": "checkin_dispatch", "subject_kind": "actor",
                    "subject": f"bot:{F}/mgr",
                    "data": {"checkin_id": CK3, "assignment_id": "asg_" + "0" * 32,
                             "work_item_id": "wi_" + "0" * 32, "task_id": "t-orphan"}}}])
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    d = _out(capsys)["checkins"][0]["dispatches"][0]
    assert d["status"] is None and d["outcome"] == "unjoined"


def test_a_dispatch_decision_with_no_join_row_lists_empty(root, capsys):
    _decision(root, "mgr", CK4, age_h=1, action="dispatch")
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = _out(capsys)["checkins"]
    assert rows[0]["dispatches"] == []


def test_a_truncated_join_row_does_not_take_out_the_query(root, capsys):
    _decision(root, "mgr", CK5, age_h=2, action="dispatch")
    _dispatched(root, CK5, task_id="t-good", terminal="completed")
    _decision(root, "mgr", CK6, age_h=1, action="dispatch")
    _dispatched(root, CK6, task_id="x" * 20000)          # over the 16 KiB DIAGNOSTIC cap
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = {r["checkin_id"]: r for r in _out(capsys)["checkins"]}
    good = rows[CK5]
    assert len(good["dispatches"]) == 1
    d = good["dispatches"][0]
    assert d["task_id"] == "t-good" and d["status"] == "completed" and d["outcome"] == "completed"
    assert rows[CK6]["dispatches"] == []      # the truncated join row silently fails to match, never raises


def test_a_truncated_decision_still_joins_through_its_source_ref(root, capsys):
    _decision(root, "mgr", CK7, age_h=1, rationale="x" * 20000)   # over cap: checkin_id parses to None
    _dispatched(root, CK7, task_id="t-tr", terminal="completed")
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = _out(capsys)["checkins"]
    assert len(rows) == 1
    r = rows[0]
    assert r["checkin_id"] is None                      # PR 1's own assertion
    assert r["checkin_ref"] == f"checkin:{CK7}"
    assert len(r["dispatches"]) == 1 and r["dispatches"][0]["task_id"] == "t-tr"


def test_another_fleets_join_row_never_attaches(root, capsys):
    _decision(root, "mgr", CK8, age_h=1, action="dispatch")
    ref = f"dispatch-log:{CK8}-x"
    emit_batch(root, [{
        "event_type": "system", "emitter": "dispatch-task", "fleet": F2,
        "source_ref": ref, "occurred_at": _ago(hours=1),
        "payload": {"event": "checkin_dispatch", "subject_kind": "actor",
                    "subject": f"bot:{F2}/mgr",
                    "data": {"checkin_id": CK8, "assignment_id": "asg_" + "f" * 32,
                             "work_item_id": "wi_" + "f" * 32, "task_id": "t-other"}}}])
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = _out(capsys)["checkins"]
    assert rows[0]["dispatches"] == []


def test_the_join_is_two_queries_not_one_per_row(root):
    cks = [f"ck_{i:032x}" for i in range(12)]
    for i, ck in enumerate(cks):
        _decision(root, "mgr", ck, age_h=i + 1, action="dispatch")
        _dispatched(root, ck, task_id=f"t-{i}", terminal="completed")
    conn, reason = open_ro(root)
    assert conn is not None, reason
    try:
        real_execute = conn.execute
        calls = {"n": 0}

        class _Counting:
            def execute(self, *a, **kw):
                calls["n"] += 1
                return real_execute(*a, **kw)

        refs = [f"checkin:{ck}" for ck in cks]
        joined = cmd._join_dispatches(_Counting(), F, refs)
        assert calls["n"] == 2
        assert len(joined) == 12
    finally:
        conn.close()
