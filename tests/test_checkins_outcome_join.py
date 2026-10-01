# tests/test_checkins_outcome_join.py
"""Canonical check-in assignment outcomes for each decision's
`checkin_dispatch` links. Seeded through the real emit
spine, in the shape `lib/dispatch-task.sh --checkin` lands (work_item +
assignment + one `checkin_dispatch` system event, actor-anchored on the
dispatcher) — see `test_checkin_doors.py::test_dispatch_checkin_appends_the_
join_row_to_the_same_batch` for the real door's own proof of that shape.

Fixtures (`_decision`, `_ago`, `_query`, `F`, `root`) are shared,
imported rather than re-declared — `root` is `autouse`, so importing it into
this module's namespace is enough for pytest to apply it here too."""

from __future__ import annotations

from tests.plane_setup import initialize_plane

import pytest

from claudlobby.commands import checkins as cmd
from claudlobby.plane import queries
from claudlobby.plane.db import open_ro
from claudlobby.plane.emit_api import emit_batch
from tests.test_checkins_cli import F, _ago, _decision, _query, root  # noqa: F401

CK1, CK2, CK3, CK4, CK5, CK6, CK7, CK8 = ("ck_" + c * 32 for c in "abcdefgh")
F2 = "other-fleet"          # a second, equally fake fleet — #526's class

_SEQ = [0]


def _dispatched(root, ck: str, *, task_id, terminal: str | None = None,  # noqa: F811 — imported shared pytest fixture
                dispatcher: str | None = None):
    """One dispatch the way `dispatch-task.sh --checkin` lands it: work_item +
    assignment (dispatch_msg_id minted, unsent by default -- no transmission
    row unless the caller adds one) + the checkin_dispatch system event, in
    ONE emit_batch, actor-anchored on the DISPATCHER (subject_kind: actor,
    normally subject: bot:<F>/mgr) — the real door's own shape (lib/dispatch-task.sh,
    search checkin_dispatch). *terminal*, when given, appends a task event of
    that name in a second emit_batch — any TASK_EVENTS token, not only a
    terminal one. Returns
    (work_item_id, assignment_id, msg_id)."""
    _SEQ[0] += 1
    n = _SEQ[0]
    wi, asg, msg = f"wi_{n:0>32}", f"asg_{n:0>32}", f"msg_{n:0>32}"
    ref = f"dispatch-log:{ck}-{n}"
    ts = _ago(hours=1)
    initialize_plane(root)
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
                     "subject": dispatcher or f"bot:{F}/mgr",
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


def test_a_decision_carries_its_canonical_assignment_outcome(root, capsys):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK1, age_h=1, action="dispatch")
    wi, asg, msg = _dispatched(root, CK1, task_id="t-shop-1", terminal="completed")
    rows = _query(root)
    assert len(rows) == 1
    dispatches = rows[0]["dispatches"]
    assert len(dispatches) == 1
    d = dispatches[0]
    assert d["task_id"] == wi
    assert d["assignment_id"] == asg
    assert d["historical_task_reference"] == "t-shop-1"
    assert d["task_state"] == "completed" and d["assignment_state"] == "closed"
    assert d["terminal_event"] == "completed"
    assert d["outcome"] == "completed"
    assert d["delivery"] == "not_assessed" and d["link_issue"] is None
    assert d["terminal_at"] is not None


def test_canonical_assign_checkin_link_has_no_historical_alias_or_delivery_claim(root, capsys):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK1, age_h=1, action="dispatch")
    wi, asg = "wi_" + "a" * 32, "asg_" + "a" * 32
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "claudlobby.tasks.v1", "fleet": F,
         "payload": {"work_item_id": wi, "title": "fix the feed",
                     "created_by": f"bot:{F}/mgr"}},
        {"event_type": "assignment", "emitter": "claudlobby.tasks.v1", "fleet": F,
         "payload": {"assignment_id": asg, "work_item_id": wi,
                     "assignee": f"bot:{F}/w1", "assigned_by": f"bot:{F}/mgr"}},
        {"event_type": "system", "emitter": "claudlobby.tasks.v1", "fleet": F,
         "payload": {"event": "checkin_dispatch", "subject_kind": "actor",
                     "subject": f"bot:{F}/mgr",
                     "data": {"checkin_id": CK1, "assignment_id": asg,
                              "work_item_id": wi, "task_id": None}}},
    ])
    d = _query(root)[0]["dispatches"][0]
    assert d["task_id"] == wi and d["assignment_id"] == asg
    assert d["historical_task_reference"] is None
    assert d["task_state"] == d["assignment_state"] == "assigned"
    assert d["outcome"] == "open" and d["delivery"] == "not_assessed"


def test_every_terminal_task_event_has_a_bucket():
    assert set(queries.TERMINAL_TASK_EVENTS) <= set(cmd._OUTCOME)


# (shape, task event or None, canonical assignment state, lifecycle bucket)
_VOCAB_CASES = [
    ("task", "completed", "closed", "completed"),
    ("task", "returned_blocked", "closed", "blocked"),
    ("task", "failed", "closed", "failed"),
    ("task", "expired", "closed", "failed"),
    ("task", "cancelled", "closed", "retired"),
    ("task", "superseded", "closed", "retired"),
    ("task", "reassigned", "closed", "retired"),
    ("txfail", None, "assigned", "open"),
    ("task", "progress", "active", "open"),
    ("unsent", None, "assigned", "open"),
]


@pytest.mark.parametrize("shape,event,assignment_state,outcome", _VOCAB_CASES,
                         ids=[c[1] or c[0] for c in _VOCAB_CASES])
def test_the_buckets_map_the_plane_vocabulary(root, capsys, shape, event, assignment_state, outcome):  # noqa: F811 — imported shared pytest fixture
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
    d = _query(root)[0]["dispatches"][0]
    assert d["assignment_state"] == assignment_state and d["outcome"] == outcome
    assert d["terminal_event"] == (event if assignment_state == "closed" else None)
    assert d["delivery"] == "not_assessed"


def test_an_id_less_dispatch_still_resolves_through_the_assignment(root, capsys):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK2, age_h=1, action="dispatch")
    wi, _, _ = _dispatched(root, CK2, task_id=None, terminal="completed")
    d = _query(root)[0]["dispatches"][0]
    assert d["task_id"] == wi and d["historical_task_reference"] is None
    assert d["terminal_event"] == "completed" and d["outcome"] == "completed"


def test_join_uses_recorded_fleet_for_human_dispatcher_and_alias_only_for_legacy_null(root, capsys):  # noqa: F811 — shared pytest fixture
    _decision(root, "mgr", CK2, age_h=1, action="dispatch")
    _, human_asg, _ = _dispatched(root, CK2, task_id=None, dispatcher="human:operator")
    _, legacy_asg, _ = _dispatched(root, CK2, task_id=None)
    # Simulate the historical null-fleet join while retaining its bot alias.
    from claudlobby.plane.db import connect, db_file
    writable = connect(db_file(root))
    try:
        writable.execute("UPDATE events SET fleet_uid=NULL WHERE kind='system' "
                         "AND event='checkin_dispatch' AND json_extract(detail, '$.assignment_id')=?",
                         (legacy_asg,))
        writable.commit()
    finally:
        writable.close()
    dispatches = _query(root)[0]["dispatches"]
    assert {row["assignment_id"] for row in dispatches} == {human_asg, legacy_asg}


def test_a_join_row_naming_no_assignment_is_unjoined_never_open(root, capsys):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK3, age_h=1, action="dispatch")
    ref = f"dispatch-log:{CK3}-orphan"
    emit_batch(root, [{
        "event_type": "system", "emitter": "dispatch-task", "fleet": F,
        "source_ref": ref, "occurred_at": _ago(hours=1),
        "payload": {"event": "checkin_dispatch", "subject_kind": "actor",
                    "subject": f"bot:{F}/mgr",
                    "data": {"checkin_id": CK3, "assignment_id": "asg_" + "0" * 32,
                             "work_item_id": "wi_" + "0" * 32, "task_id": "t-orphan"}}}])
    d = _query(root)[0]["dispatches"][0]
    assert d["assignment_state"] is None and d["outcome"] == "unjoined"
    assert d["link_issue"] == "missing_task"


def test_wrong_or_unresolved_canonical_links_do_not_read_as_open(root, capsys):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK3, age_h=1, action="dispatch")
    wi1, asg1, _ = _dispatched(root, CK3, task_id=None)
    wi2, asg2, _ = _dispatched(root, CK3, task_id=None)
    emit_batch(root, [
        {"event_type": "task", "emitter": "dispatch-task", "fleet": F,
         "payload": {"work_item_id": wi1, "assignment_id": asg2,
                     "event": "accepted", "actor": f"bot:{F}/w1"}},
        {"event_type": "system", "emitter": "dispatch-task", "fleet": F,
         "payload": {"event": "checkin_dispatch", "subject_kind": "actor",
                     "subject": f"bot:{F}/mgr",
                     "data": {"checkin_id": CK3, "assignment_id": asg1,
                              "work_item_id": wi2, "task_id": None}}},
    ])
    dispatches = _query(root)[0]["dispatches"]
    assert len(dispatches) == 3
    by_pair = {(d["task_id"], d["assignment_id"]): d for d in dispatches}
    assert by_pair[wi2, asg1]["outcome"] == "unjoined"
    assert by_pair[wi2, asg1]["link_issue"] == "mismatched_assignment_task"
    assert by_pair[wi1, asg1]["outcome"] == "unknown"
    assert by_pair[wi1, asg1]["link_issue"] == "unresolved_task"


def test_a_dispatch_decision_with_no_join_row_lists_empty(root, capsys):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK4, age_h=1, action="dispatch")
    rows = _query(root)
    assert rows[0]["dispatches"] == []


def test_a_truncated_join_row_does_not_take_out_the_query(root, capsys):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK5, age_h=2, action="dispatch")
    _dispatched(root, CK5, task_id="t-good", terminal="completed")
    _decision(root, "mgr", CK6, age_h=1, action="dispatch")
    _dispatched(root, CK6, task_id="x" * 20000)          # over the 16 KiB DIAGNOSTIC cap
    rows = {r["checkin_id"]: r for r in _query(root)}
    good = rows[CK5]
    assert len(good["dispatches"]) == 1
    d = good["dispatches"][0]
    assert d["historical_task_reference"] == "t-good"
    assert d["terminal_event"] == "completed" and d["outcome"] == "completed"
    assert rows[CK6]["dispatches"] == []      # the truncated join row silently fails to match, never raises


def test_a_truncated_decision_still_joins_through_its_source_ref(root, capsys):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK7, age_h=1, rationale="x" * 20000)   # over cap: checkin_id parses to None
    _dispatched(root, CK7, task_id="t-tr", terminal="completed")
    rows = _query(root)
    assert len(rows) == 1
    r = rows[0]
    assert r["checkin_id"] is None                      # PR 1's own assertion
    assert r["checkin_ref"] == f"checkin:{CK7}"
    assert len(r["dispatches"]) == 1
    assert r["dispatches"][0]["historical_task_reference"] == "t-tr"


def test_another_fleets_join_row_never_attaches(root, capsys):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK8, age_h=1, action="dispatch")
    ref = f"dispatch-log:{CK8}-x"
    emit_batch(root, [{
        "event_type": "system", "emitter": "dispatch-task", "fleet": F2,
        "source_ref": ref, "occurred_at": _ago(hours=1),
        "payload": {"event": "checkin_dispatch", "subject_kind": "actor",
                    "subject": f"bot:{F2}/mgr",
                    "data": {"checkin_id": CK8, "assignment_id": "asg_" + "f" * 32,
                             "work_item_id": "wi_" + "f" * 32, "task_id": "t-other"}}}])
    rows = _query(root)
    assert rows[0]["dispatches"] == []


def test_the_join_batches_canonical_history_not_one_read_per_row(root):  # noqa: F811 — imported shared pytest fixture
    """The page reads one scoped reducer projection, not one show per link."""
    cks = [f"ck_{i:032x}" for i in range(12)]
    for i, ck in enumerate(cks):
        _decision(root, "mgr", ck, age_h=i + 1, action="dispatch")
        _dispatched(root, ck, task_id=f"t-{i}", terminal="completed")
    conn, reason = open_ro(root)
    assert conn is not None, reason
    try:
        real_execute = conn.execute

        class _Counting:
            def __init__(self):
                self.n = 0

            @property
            def in_transaction(self):
                return conn.in_transaction

            def execute(self, *a, **kw):
                self.n += 1
                return real_execute(*a, **kw)

            def rollback(self):
                return conn.rollback()

        wrapped = _Counting()
        out = cmd.collect_checkins(wrapped, F, since=None, bot=None, last=False)
        page_count = wrapped.n
        assert len(out) == 12
        assert all(len(r["dispatches"]) == 1 for r in out)
        wrapped = _Counting()
        one = cmd.collect_checkins(wrapped, F, since=None, bot=None, last=False, limit=1)
        assert len(one) == 1 and page_count <= wrapped.n + 2

        refs = [f"checkin:{ck}" for ck in cks]
        with pytest.raises(cmd.TaskStateError, match="read snapshot"):
            cmd._join_dispatches(_Counting(), F, refs)
        conn.execute("BEGIN")
        try:
            wrapped = _Counting()
            joined = cmd._join_dispatches(wrapped, F, refs)
            page_count = wrapped.n
            assert len(joined) == 12
            wrapped = _Counting()
            assert len(cmd._join_dispatches(wrapped, F, refs[:1])) == 1
            assert page_count <= wrapped.n + 2
        finally:
            conn.rollback()
    finally:
        conn.close()


def test_the_read_projection_names_the_dispatch_and_its_status(root):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK1, age_h=1, action="dispatch")
    wi, asg, msg = _dispatched(root, CK1, task_id="t-shop-1", terminal="completed")
    row = _query(root)[0]["dispatches"][0]
    assert (row["task_id"], row["assignment_id"], row["outcome"]) == (wi, asg, "completed")
    assert row["historical_task_reference"] == "t-shop-1" and row["delivery"] == "not_assessed"


def test_a_dispatch_decision_without_a_join_is_counted_as_unjoined(root):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK2, age_h=3, action="dispatch")
    _decision(root, "mgr", CK3, age_h=2, action="nothing")
    _decision(root, "mgr", CK4, age_h=1, action="ask",
              raise_={"decided": True, "reason": "a fork", "held": []})
    rows = _query(root)
    assert rows[-1]["checkin_id"] == CK2 and rows[-1]["dispatches"] == []
    assert cmd.summarize(rows)["totals"]["dispatch_outcomes"]["unjoined"] == 1


def test_an_id_less_historical_dispatch_retains_its_canonical_id(root):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK5, age_h=1, action="dispatch")
    wi, _, _ = _dispatched(root, CK5, task_id=None, terminal="completed")
    row = _query(root)[0]["dispatches"][0]
    assert row["task_id"] == wi and row["outcome"] == "completed"
    assert row["historical_task_reference"] is None
