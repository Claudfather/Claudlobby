"""A1 reducer against the real SQLite schema, without runtime writers/imports."""

from __future__ import annotations

import sqlite3

import pytest

from claudlobby.plane.migrations import _migration_files, migrate
from claudlobby.task_state import TASK_EMITTER, TaskStateError, UnresolvedTaskError, read_tasks


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("PRAGMA foreign_keys=ON")
    migrate(db)
    yield db
    db.close()


def _insert(conn, table, *, emitter=TASK_EMITTER, fleet_uid="fleet_a", **fields):
    seq = conn.execute("INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                       " VALUES (?, ?, 't')", (f"ledger_{conn.total_changes}", table)).lastrowid
    row = dict(ingest_seq=seq, event_id=f"event_{seq}", schema_version="1.0.0",
               occurred_at="2026-09-01T00:00:00Z", ingested_at="2026-09-01T00:00:01Z",
               host_uid="host_test", emitter=emitter, fleet_uid=fleet_uid,
               origin="legacy" if emitter == "dispatch-task" else "live", **fields)
    conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
    return row["event_id"]


def _task(conn, tid, **fields):
    _insert(conn, "work_items", work_item_id=tid, title="Keep my title", body="authored body",
            created_by_uid="actor_filer_not_owner", project_key="shop", workstream_id="ws_goal", **fields)


def _assignment(conn, aid, tid, **fields):
    _insert(conn, "assignments", assignment_id=aid, work_item_id=tid,
            assignee_uid="actor_worker", assigned_by_uid="actor_previous_manager",
            expected_by="2026-09-02T00:00:00Z", **fields)


def _event(conn, tid, aid, event, **fields):
    return _insert(conn, "events", kind="task", work_item_id=tid, assignment_id=aid, event=event, **fields)


def _read(conn, tid):
    return read_tasks(conn, fleet_uid="fleet_a").get(tid)


def test_intake_and_idless_assignment_preserve_identity_provenance_and_fleet_ownership(conn):
    _task(conn, "wi_intake", source_ref="operator-request:abc")
    _task(conn, "wi_idless", emitter="dispatch-task", source_ref="dispatch-log:sha:original")
    _assignment(conn, "asg_idless", "wi_idless", emitter="dispatch-task", source_ref="dispatch-log:sha:original")
    _task(conn, "wi_other_fleet", fleet_uid="fleet_b")
    snapshot = read_tasks(conn, fleet_uid="fleet_a")
    assert {task.task_id for task in snapshot.tasks} == {"wi_intake", "wi_idless"}
    intake = snapshot.get("wi_intake")
    assert intake.state == "queued" and intake.current_assignment is None and intake.history == ()
    assert (intake.title, intake.body, intake.project_key, intake.workstream_id) == (
        "Keep my title", "authored body", "shop", "ws_goal")
    assert intake.created_by_uid == "actor_filer_not_owner" and intake.fleet_uid == "fleet_a"
    task = snapshot.get("wi_idless")
    assert task.state == "assigned" and task.display_ids == ()
    assert task.source_ref == "dispatch-log:sha:original" and task.origin == "legacy"
    assert task.current_assignment.assignment_id == "asg_idless"
    assert task.current_assignment.assigned_by_uid == "actor_previous_manager"
    assert task.host_uid == "host_test" and task.schema_version == "1.0.0"
    assert snapshot.get("wi_other_fleet") is None and snapshot.get("sha:original") is None
    assert not snapshot.issues


def test_receipt_is_not_acceptance_and_attention_metadata_cannot_erase_lifecycle(conn):
    _task(conn, "wi_delivery")
    _assignment(conn, "asg_delivery", "wi_delivery", dispatch_msg_id="msg_delivery")
    for receipt in ("pane_submitted", "recipient_acknowledged"):
        _insert(conn, "events", kind="transmission", event=receipt, msg_id="msg_delivery",
                carrier="tmux", attempt_no=1)
    assert _read(conn, "wi_delivery").state == "assigned"
    _event(conn, "wi_delivery", "asg_delivery", "accepted", actor_uid="actor_worker")
    _event(conn, "wi_delivery", "asg_delivery", "escalated", detail='{"question":"which repo?"}')
    _event(conn, "wi_delivery", "asg_delivery", "nudged")
    assert _read(conn, "wi_delivery").state == "active"
    _event(conn, "wi_delivery", "asg_delivery", "blocked_waiting")
    _event(conn, "wi_delivery", "asg_delivery", "nudged")
    assert _read(conn, "wi_delivery").state == "blocked"
    _event(conn, "wi_delivery", "asg_delivery", "progress", detail='{"progress":40}')
    task = _read(conn, "wi_delivery")
    assert task.state == "active"
    assert [e.event for e in task.history] == ["accepted", "escalated", "nudged", "blocked_waiting", "nudged", "progress"]
    assert task.history[1].detail == '{"question":"which repo?"}'


def test_new_assignment_release_queues_work_and_successor_uses_its_own_acceptance(conn):
    _task(conn, "wi_retry", source_ref="dispatch-log:old-display")
    for number, release in enumerate(("returned_blocked", "rejected", "expired", "cancelled", "superseded", "reassigned")):
        aid = f"asg_{number}"
        _assignment(conn, aid, "wi_retry", source_ref="dispatch-log:old-display")
        _event(conn, "wi_retry", aid, "accepted")
        released = _event(conn, "wi_retry", aid, release)
        task = _read(conn, "wi_retry")
        assert task.state == "queued" and task.current_assignment is None and not task.blockers
        assert task.assignments[-1].terminal_event.event_id == released
    _assignment(conn, "asg_successor", "wi_retry")
    _event(conn, "wi_retry", "asg_0", "accepted")  # stale ack cannot accept the replacement
    task = _read(conn, "wi_retry")
    assert task.state == "assigned" and task.current_assignment.assignment_id == "asg_successor"
    assert task.display_ids == ("old-display",) and len(task.assignments) == 7
    _event(conn, "wi_retry", "asg_successor", "failed")
    _event(conn, "wi_retry", "asg_successor", "progress")
    assert _read(conn, "wi_retry").state == "failed"


def test_historical_terminal_work_stays_closed_in_original_schema_then_current_schema():
    with sqlite3.connect(":memory:", isolation_level=None) as db:
        db.executescript(_migration_files()[0][1])
        for token in ("completed", "returned_blocked", "expired", "superseded", "reassigned", "failed", "cancelled"):
            tid, aid = f"wi_{token}", f"asg_{token}"
            _task(db, tid, emitter="dispatch-task", source_ref=f"dispatch-log:{token}")
            _assignment(db, aid, tid, emitter="dispatch-task", source_ref=f"dispatch-log:{token}")
            _event(db, tid, aid, token, emitter="report-back")
            _event(db, tid, aid, "progress", emitter=TASK_EMITTER)
        before = read_tasks(db, fleet_uid="fleet_a")
        assert before.schema_version == 1 and not before.blockers
        assert all(not task.open and task.current_assignment is None for task in before.tasks)
        assert {task.state for task in before.tasks} == {"completed", "failed", "cancelled"}
        assert before.get("wi_returned_blocked").terminal_event.event == "returned_blocked"
        migrate(db)
        after = read_tasks(db, fleet_uid="fleet_a")
        assert after.tasks == before.tasks and after.issues == before.issues


def test_terminal_work_does_not_reopen_and_work_cancellation_closes_current_assignment(conn):
    _task(conn, "wi_done")
    _assignment(conn, "asg_done", "wi_done")
    _event(conn, "wi_done", "asg_done", "completed")
    _assignment(conn, "asg_illegal", "wi_done")
    done = _read(conn, "wi_done")
    assert done.state == "completed" and done.current_assignment is None
    with pytest.raises(UnresolvedTaskError, match="current_assignment_on_terminal_task"):
        done.require_resolved()
    _task(conn, "wi_cancel")
    _assignment(conn, "asg_cancel", "wi_cancel")
    cancel = _event(conn, "wi_cancel", None, "cancelled")
    task = _read(conn, "wi_cancel")
    assert task.state == "cancelled" and task.current_assignment is None and not task.blockers
    assert task.assignments[0].terminal_event.event_id == cancel


def test_ambiguous_current_and_legacy_display_history_never_select_the_latest(conn):
    _task(conn, "wi_double")
    _assignment(conn, "asg_first", "wi_double")
    _assignment(conn, "asg_latest", "wi_double")
    task = _read(conn, "wi_double")
    assert task.state is None and task.current_assignment is None
    assert [a.assignment_id for a in task.assignments if a.current] == ["asg_first", "asg_latest"]
    with pytest.raises(UnresolvedTaskError, match="multiple_current_assignments"):
        task.require_resolved()
    for suffix in ("closed", "live", "other"):
        _task(conn, "wi_" + suffix, emitter="dispatch-task", source_ref="dispatch-log:repeat")
        _assignment(conn, "asg_" + suffix, "wi_" + suffix, emitter="dispatch-task", source_ref="dispatch-log:repeat")
    _event(conn, "wi_closed", "asg_closed", "returned_blocked", emitter="report-back")
    closed = _read(conn, "wi_closed")
    assert closed.state == "cancelled" and not closed.blockers
    live = _read(conn, "wi_live")
    assert {issue.code for issue in live.blockers} == {"legacy_display_closure_disagreement", "ambiguous_active_display_id"}
    assert live.state is None and live.current_assignment is None


def test_cross_fleet_dangling_and_future_producer_issues_block_without_ownership_guesses(conn):
    _task(conn, "wi_cross")
    _assignment(conn, "asg_cross", "wi_cross", fleet_uid="fleet_b")
    _assignment(conn, "asg_orphan", "wi_missing")
    _task(conn, "wi_broken")
    _event(conn, "wi_broken", "asg_missing", "accepted")
    _task(conn, "wi_future", emitter="claudlobby.tasks.v2")
    _task(conn, "wi_future_event")
    _assignment(conn, "asg_future_event", "wi_future_event")
    _event(conn, "wi_future_event", "asg_future_event", "completed", emitter="claudlobby.tasks.v2")
    _task(conn, "wi_private", fleet_uid="fleet_b")
    _assignment(conn, "asg_private", "wi_private", fleet_uid="fleet_b")
    _task(conn, "wi_unscoped", fleet_uid=None)
    report = read_tasks(conn, fleet_uid="fleet_a")
    assert {issue.code for issue in report.blockers} == {
        "cross_fleet_assignment", "dangling_assignment", "dangling_task_event", "unknown_task_producer"}
    assert report.get("wi_private") is None and report.get("wi_unscoped") is None
    assert all(task.state is None and task.current_assignment is None for task in report.tasks)
    _event(conn, "wi_cross", "asg_cross", "completed", emitter="report-back")
    history = _read(conn, "wi_cross")
    assert history.state == "completed" and not history.blockers
    assert [issue.code for issue in history.issues] == ["cross_fleet_assignment"]
    _event(conn, "wi_missing", "asg_orphan", "failed", emitter="report-back")
    _event(conn, "wi_missing", "asg_orphan", "progress", emitter="report-back")
    _task(conn, "wi_closed_broken")
    _event(conn, "wi_closed_broken", "asg_never_existed", "failed", emitter="report-back")
    closed_report = read_tasks(conn, fleet_uid="fleet_a")
    assert not [issue for issue in closed_report.blockers
                if issue.task_id in {"wi_missing", "wi_closed_broken"}]
    assert {issue.code for issue in closed_report.issues if issue.task_id == "wi_missing"} == {
        "dangling_assignment", "dangling_task_event"}


def test_wrong_task_event_link_cannot_certify_mutation_safety(conn):
    _task(conn, "wi_actual")
    _assignment(conn, "asg_actual", "wi_actual")
    _task(conn, "wi_wrong")
    _task(conn, "wi_foreign", fleet_uid="fleet_b")
    _event(conn, "wi_wrong", "asg_actual", "failed", emitter="report-back")
    snapshot = read_tasks(conn, fleet_uid="fleet_a")
    assert snapshot.get("wi_actual").state is None
    assert snapshot.get("wi_actual").assignments[0].state == "closed"  # retain old assignment closure
    for task in snapshot.tasks:
        with pytest.raises(UnresolvedTaskError, match="mismatched_task_event"):
            task.require_resolved()
    for tid, aid in (("wi_legacy_live", "asg_legacy_live"),
                     ("wi_legacy_peer", "asg_legacy_peer"),
                     ("wi_legacy_closed", "asg_legacy_closed")):
        _task(conn, tid, emitter="dispatch-task", source_ref="dispatch-log:shared")
        _assignment(conn, aid, tid, emitter="dispatch-task", source_ref="dispatch-log:shared")
    _event(conn, "wi_legacy_closed", "asg_legacy_closed", "returned_blocked",
           emitter="report-back")
    snapshot = read_tasks(conn, fleet_uid="fleet_a")
    selected = read_tasks(conn, fleet_uid="fleet_a",
                          task_ids=["wi_actual", "wi_legacy_live", "wi_foreign", "wi_actual"])
    wanted = {"wi_actual", "wi_legacy_live"}
    assert selected.tasks == tuple(task for task in snapshot.tasks if task.task_id in wanted)
    assert selected.issues == tuple(issue for issue in snapshot.issues if issue.task_id in wanted)
    assert [event.task_id for event in selected.get("wi_actual").assignments[0].history] == ["wi_wrong"]
    assert {issue.code for issue in selected.get("wi_legacy_live").blockers} == {
        "legacy_display_closure_disagreement", "ambiguous_active_display_id"}
    _task(conn, "wi_selected_link")
    _assignment(conn, "asg_selected_link", "wi_selected_link")
    _task(conn, "wi_off_page_owner")
    _assignment(conn, "asg_off_page_owner", "wi_off_page_owner")
    _event(conn, "wi_selected_link", "asg_off_page_owner", "accepted")
    _event(conn, "wi_off_page_owner", None, "cancelled")
    whole = read_tasks(conn, fleet_uid="fleet_a").get("wi_selected_link")
    subset = read_tasks(conn, fleet_uid="fleet_a", task_ids=["wi_selected_link"]).get("wi_selected_link")
    assert subset == whole
    assert subset.state == "assigned"
    assert any(issue.code == "mismatched_task_event" and not issue.blocking
               for issue in subset.issues)
    empty = read_tasks(conn, fleet_uid="fleet_a", task_ids=[])
    assert empty.tasks == empty.issues == ()


def test_snapshot_is_read_only_keeps_caller_transaction_and_batches_history_queries(conn):
    def measured():
        statements = []
        conn.set_trace_callback(statements.append)
        result = read_tasks(conn, fleet_uid="fleet_a")
        conn.set_trace_callback(None)
        return result, sum(s.startswith("SELECT") for s in statements)
    _task(conn, "wi_0")
    _assignment(conn, "asg_0", "wi_0")
    _event(conn, "wi_0", "asg_0", "accepted")
    one, count_one = measured()
    for i in range(1, 100):
        _task(conn, f"wi_{i}")
        _assignment(conn, f"asg_{i}", f"wi_{i}")
        _event(conn, f"wi_{i}", f"asg_{i}", "accepted")
    conn.execute("BEGIN")
    before = tuple(conn.iterdump())
    changes = conn.total_changes
    denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE}
    conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
    many, count_many = measured()
    assert len(many.tasks) == 100 and count_many == count_one
    assert conn.in_transaction and conn.total_changes == changes and tuple(conn.iterdump()) == before
    assert many.tasks[0] == one.tasks[0]
    # Passing None to disable the authorizer is supported only since 3.11.
    conn.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
    conn.rollback()
    with pytest.raises(ValueError, match="fleet_uid"):
        read_tasks(conn, fleet_uid="")
    conn.execute("PRAGMA user_version=999")
    with pytest.raises(TaskStateError, match="unsupported task schema"):
        read_tasks(conn, fleet_uid="fleet_a")
    assert not conn.in_transaction
