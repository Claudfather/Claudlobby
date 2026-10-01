"""Scoped reads against actual SQL and the shared reducer's storage fixtures."""

import sqlite3

import pytest

from claudlobby.plane.migrations import migrate
from claudlobby.task_audit import audit_tasks
from claudlobby.task_queries import (
    AmbiguousTaskReferenceError,
    InvalidTaskCursorError,
    TaskNotFoundError,
    TaskQueryError,
    WrongTaskReferenceError,
    list_tasks,
    list_task_escalations,
    show_assignment,
    show_task,
)
from tests.test_task_state import _assignment, _event, _task


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:", isolation_level=None)
    migrate(db)
    yield db
    db.close()


def _ids(page):
    return [task.task_id for task in page.items]


def test_intake_preserves_unassigned_work_and_legacy_terminal_history(conn):
    _task(conn, "wi_z_first")
    _task(conn, "wi_returned")
    _assignment(conn, "asg_returned", "wi_returned")
    _event(conn, "wi_returned", "asg_returned", "returned_blocked")
    _task(conn, "wi_old", emitter="dispatch-task", source_ref="dispatch-log:old-17")
    _assignment(conn, "asg_old", "wi_old", emitter="dispatch-task", source_ref="dispatch-log:old-17")
    _event(conn, "wi_old", "asg_old", "expired", emitter="report-back")
    _task(conn, "wi_a_last")
    _assignment(conn, "asg_current", "wi_a_last")
    _event(conn, "wi_a_last", "asg_current", "accepted")

    page = list_tasks(conn, fleet_uid="fleet_a")
    assert _ids(page) == ["wi_z_first", "wi_returned", "wi_a_last"]
    assert [task.state for task in page.items] == ["queued", "queued", "active"]
    assert page.next_cursor is None and page.issues == ()
    assert page.items[0].current_assignment is None
    assert page.items[0].created_by_uid == "actor_filer_not_owner"
    assert _ids(list_tasks(conn, fleet_uid="fleet_a", bot_uid="actor_worker")) == ["wi_a_last"]
    assert _ids(list_tasks(conn, fleet_uid="fleet_a", state="all", bot_uid="actor_worker")) == ["wi_old", "wi_a_last"]
    old = show_task(conn, "wi_old", fleet_uid="fleet_a")
    assert old.state == "cancelled" and old.display_ids == ("old-17",)
    assert old.current_assignment is None and old.history[0].event == "expired"
    view = show_assignment(conn, "asg_old", fleet_uid="fleet_a")
    assert view.task == old and view.assignment == old.assignments[0]
    assert view.assignment.state == "closed"


def test_cursor_continues_admission_order_without_duplicates_or_scope_drift(conn):
    for tid in ("wi_z", "wi_y", "wi_x", "wi_w"):
        _task(conn, tid)
    first = list_tasks(conn, fleet_uid="fleet_a", limit=2)
    assert _ids(first) == ["wi_z", "wi_y"] and first.next_cursor
    _assignment(conn, "asg_changed", "wi_z")
    _event(conn, "wi_z", "asg_changed", "accepted")
    _task(conn, "wi_new")
    second = list_tasks(conn, fleet_uid="fleet_a", limit=2, cursor=first.next_cursor)
    third = list_tasks(conn, fleet_uid="fleet_a", limit=2, cursor=second.next_cursor)
    assert _ids(first) + _ids(second) + _ids(third) == ["wi_z", "wi_y", "wi_x", "wi_w", "wi_new"]
    assert third.next_cursor is None
    for changed in ({"fleet_uid": "fleet_b"}, {"state": "all"},
                    {"bot_uid": "actor_worker"}, {"limit": 3}):
        kwargs = {"fleet_uid": "fleet_a", "limit": 2, "cursor": first.next_cursor, **changed}
        with pytest.raises(InvalidTaskCursorError):
            list_tasks(conn, **kwargs)
    with pytest.raises(InvalidTaskCursorError):
        list_tasks(conn, fleet_uid="fleet_a", cursor="not a cursor")


def test_canonical_reads_refuse_wrong_kind_and_display_alias_with_scoped_recovery(conn):
    _task(conn, "wi_local", emitter="dispatch-task", source_ref="dispatch-log:display-7")
    _assignment(conn, "asg_local", "wi_local", source_ref="dispatch-log:display-7")
    _task(conn, "wi_private", fleet_uid="fleet_b", source_ref="dispatch-log:private-display")
    _assignment(conn, "asg_private", "wi_private", fleet_uid="fleet_b")
    for reference in ("asg_local", "display-7"):
        with pytest.raises(WrongTaskReferenceError) as raised:
            show_task(conn, reference, fleet_uid="fleet_a")
        hint = raised.value.hint
        assert hint.next_command == ("task", "show", "wi_local")
        assert (hint.candidates[0].task_id, hint.candidates[0].assignment_id) == ("wi_local", "asg_local")
        mapping = audit_tasks(conn).preview(reference, fleet_uid="fleet_a").mapping
        assert (mapping.task_id, mapping.assignment_id) == ("wi_local", "asg_local")
    with pytest.raises(WrongTaskReferenceError) as raised:
        show_assignment(conn, "wi_local", fleet_uid="fleet_a")
    assert raised.value.hint.next_command == ("assignment", "show", "asg_local")
    for reference in ("wi_private", "asg_private", "private-display", "does-not-exist"):
        for query in (show_task, show_assignment):
            with pytest.raises(TaskNotFoundError) as raised:
                query(conn, reference, fleet_uid="fleet_a")
            assert raised.value.hint.candidates == () and raised.value.hint.total_matches == 0
            assert raised.value.hint.next_command == ("task", "list", "--state", "all")


def test_ambiguous_reference_keeps_closed_candidates_and_bounds_hints(conn):
    for n in range(21):
        tid, aid = f"wi_{n:02}", f"asg_{n:02}"
        _task(conn, tid, source_ref="dispatch-log:repeat")
        _assignment(conn, aid, tid, source_ref="dispatch-log:repeat")
        _event(conn, tid, aid, "completed")
    with pytest.raises(AmbiguousTaskReferenceError) as raised:
        show_task(conn, "repeat", fleet_uid="fleet_a")
    hint = raised.value.hint
    assert hint.total_matches == 21 and len(hint.candidates) == 20 and hint.next_command is None
    assert [candidate.task_id for candidate in hint.candidates] == [f"wi_{n:02}" for n in range(20)]
    assert show_task(conn, "wi_20", fleet_uid="fleet_a").state == "completed"
    # Return-to-queue keeps both the canonical intake and closed assignment
    # mapping. A wrong-kind task ID must not choose its old worker assignment.
    _task(conn, "wi_retry", source_ref="dispatch-log:retry")
    _assignment(conn, "asg_retry", "wi_retry", source_ref="dispatch-log:retry")
    _event(conn, "wi_retry", "asg_retry", "returned_blocked")
    with pytest.raises(AmbiguousTaskReferenceError) as raised:
        show_assignment(conn, "wi_retry", fleet_uid="fleet_a")
    expected = audit_tasks(conn).preview("wi_retry", fleet_uid="fleet_a")
    assert {(r.task_id, r.assignment_id) for r in raised.value.hint.candidates} == {
        (r.task_id, r.assignment_id) for r in expected.candidates}


def test_escalations_follow_current_work_and_expose_broken_history(conn):
    _task(conn, "wi_future", emitter="claudlobby.tasks.v2")
    _task(conn, "wi_double")
    _assignment(conn, "asg_one", "wi_double")
    _assignment(conn, "asg_two", "wi_double")
    _event(conn, "wi_double", None, "escalated", detail='{"question":"unresolved"}')
    _assignment(conn, "asg_orphan", "wi_missing")
    _task(conn, "wi_cross")
    _assignment(conn, "asg_cross", "wi_cross", fleet_uid="fleet_b")
    _task(conn, "wi_queued")
    queued_raise = _event(conn, "wi_queued", None, "escalated", actor_uid="actor_manager",
                          detail='{"by":"manager","question":"Which priority?"}')
    _event(conn, "wi_queued", None, "nudged")

    escalations = list_task_escalations(conn, fleet_uid="fleet_a")
    assert [(row.task_id, row.assignment_id) for row in escalations.items] == [("wi_queued", None)]
    queued = escalations.items[0]
    assert (queued.event_id, queued.actor_uid, queued.by, queued.question) == (
        queued_raise, "actor_manager", "manager", "Which priority?")
    assert queued.occurred_at == "2026-09-01T00:00:00Z"

    _task(conn, "wi_linked")
    _assignment(conn, "asg_old", "wi_linked")
    linked_raise = _event(conn, "wi_linked", "asg_old", "escalated",
                          detail='{"question":"Need review"}')
    assert [(row.task_id, row.event_id) for row in list_task_escalations(
        conn, fleet_uid="fleet_a").items] == [("wi_queued", queued_raise), ("wi_linked", linked_raise)]
    _assignment(conn, "asg_queued", "wi_queued")
    assert [row.task_id for row in list_task_escalations(conn, fleet_uid="fleet_a").items] == [
        "wi_queued", "wi_linked"]
    _event(conn, "wi_queued", "asg_queued", "progress")
    assert [row.task_id for row in list_task_escalations(conn, fleet_uid="fleet_a").items] == ["wi_linked"]
    _event(conn, "wi_queued", "asg_queued", "returned_blocked")
    assert [row.task_id for row in list_task_escalations(conn, fleet_uid="fleet_a").items] == ["wi_linked"]
    _event(conn, "wi_linked", "asg_old", "superseded")
    _assignment(conn, "asg_new", "wi_linked")
    _event(conn, "wi_linked", "asg_old", "escalated", detail='{"question":"stale"}')
    assert list_task_escalations(conn, fleet_uid="fleet_a").items == ()
    replacement_raise = _event(conn, "wi_linked", "asg_new", "escalated")
    replacement = list_task_escalations(conn, fleet_uid="fleet_a").items[0]
    assert (replacement.task_id, replacement.assignment_id, replacement.event_id,
            replacement.by, replacement.question) == ("wi_linked", "asg_new", replacement_raise, None, None)
    _event(conn, "wi_linked", "asg_old", "progress")  # late stale history cannot answer the current raise
    assert list_task_escalations(conn, fleet_uid="fleet_a").items == (replacement,)

    page = list_tasks(conn, fleet_uid="fleet_a")
    assert _ids(page) == ["wi_queued", "wi_linked"]
    assert {issue.code for issue in page.issues} == {
        "unknown_task_producer", "multiple_current_assignments", "dangling_assignment", "cross_fleet_assignment"}
    assert {issue.code for issue in list_task_escalations(conn, fleet_uid="fleet_a").issues} == {
        "unknown_task_producer", "multiple_current_assignments", "dangling_assignment", "cross_fleet_assignment"}
    assert _ids(list_tasks(conn, fleet_uid="fleet_a", state="all")) == [
        "wi_future", "wi_double", "wi_cross", "wi_queued", "wi_linked"]
    future = show_task(conn, "wi_future", fleet_uid="fleet_a")
    assert future.state is None and future.blockers[0].code == "unknown_task_producer"
    view = show_assignment(conn, "asg_one", fleet_uid="fleet_a")
    assert view.task.current_assignment is None and view.task.blockers[0].code == "multiple_current_assignments"
    with pytest.raises(TaskNotFoundError):
        show_assignment(conn, "asg_cross", fleet_uid="fleet_a")
    with pytest.raises(WrongTaskReferenceError) as raised:
        show_assignment(conn, "wi_cross", fleet_uid="fleet_a")
    assert raised.value.hint.next_command == ("task", "show", "wi_cross")
    assert raised.value.hint.candidates[0].assignment_id is None


def test_queries_preserve_read_only_connection_and_validate_bounds(conn):
    _task(conn, "wi_read")
    _assignment(conn, "asg_read", "wi_read")
    before = conn.total_changes
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    list_tasks(conn, fleet_uid="fleet_a", limit=1000)
    list_task_escalations(conn, fleet_uid="fleet_a")
    show_task(conn, "wi_read", fleet_uid="fleet_a")
    show_assignment(conn, "asg_read", fleet_uid="fleet_a")
    assert conn.in_transaction and conn.total_changes == before
    conn.rollback()
    assert list_tasks(conn, fleet_uid="fleet_b").items == ()
    assert not conn.in_transaction
    for invalid in ({"limit": 0}, {"limit": 1001}, {"limit": True}, {"limit": "2"},
                    {"state": "unknown"}, {"fleet_uid": ""}, {"bot_uid": ""}):
        with pytest.raises(TaskQueryError):
            list_tasks(conn, **{"fleet_uid": "fleet_a", **invalid})
    with pytest.raises(TaskQueryError):
        list_task_escalations(conn, fleet_uid="")
