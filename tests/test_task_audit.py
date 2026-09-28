"""A0 inventories real historical/current SQLite schemas, without emit/imports."""

from __future__ import annotations

import sqlite3
import pytest

from claudlobby.plane.db import db_file
from claudlobby.plane.migrations import _migration_files, migrate
from claudlobby.task_audit import TaskAuditError, audit_root, audit_tasks


@pytest.fixture(params=[1, 12], ids=["original-kernel", "current-schema"])
def conn(request):
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    if request.param == 1:
        connection.executescript(_migration_files()[0][1])
    else:
        migrate(connection)
    yield connection
    connection.close()


def _insert(conn, table, **fields):
    seq = conn.execute(
        "INSERT INTO ingest_ledger (event_id, family, ingested_at) VALUES (?, ?, 't')",
        (f"ev_{conn.execute('SELECT COUNT(*) FROM ingest_ledger').fetchone()[0]}", table),
    ).lastrowid
    row = dict(ingest_seq=seq, event_id=f"row_{seq}", schema_version="1.0.0",
               occurred_at="2026-09-01T00:00:00Z", ingested_at="2026-09-01T00:00:00Z",
               host_uid="host_test", emitter="dispatch-task", origin="legacy", **fields)
    conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                 tuple(row.values()))


def _task(conn, tid, *, fleet="fleet_a", ref=None):
    _insert(conn, "work_items", work_item_id=tid, fleet_uid=fleet, source_ref=ref,
            title="Historical work", created_by_uid="actor_not_the_owner")


def _assignment(conn, aid, tid, *, fleet="fleet_a", ref=None, worker="actor_worker"):
    _insert(conn, "assignments", assignment_id=aid, work_item_id=tid, fleet_uid=fleet,
            source_ref=ref, assignee_uid=worker, assigned_by_uid="actor_not_the_owner")


def _event(conn, tid, aid, event):
    _insert(conn, "events", kind="task", event=event, work_item_id=tid, assignment_id=aid)


def test_intake_idless_and_scoped_historical_reference_mapping(conn):
    _task(conn, "wi_intake")
    _task(conn, "wi_idless", ref="dispatch-log:sha:abcd")
    _assignment(conn, "asg_idless", "wi_idless", ref="dispatch-log:sha:abcd")
    _task(conn, "wi_named", ref="dispatch-log:old-ticket")
    _assignment(conn, "asg_named", "wi_named", ref="dispatch-log:old-ticket")
    # A different fleet's use of the same display ID must not enter the hint.
    _task(conn, "wi_private", fleet="fleet_b", ref="dispatch-log:old-ticket")
    _assignment(conn, "asg_private", "wi_private", fleet="fleet_b", ref="dispatch-log:old-ticket")

    report = audit_tasks(conn)
    assert report.schema_version == conn.execute("PRAGMA user_version").fetchone()[0]
    assert report.counts["scoped_tasks"] == 4
    assert report.counts["unassigned_tasks"] == 1
    assert report.counts["current_assignments"] == 3
    assert not report.blockers
    intake = report.preview("wi_intake", fleet_uid="fleet_a").mapping
    assert intake.assignment_id is None and intake.resumable
    idless = report.preview("asg_idless", fleet_uid="fleet_a").mapping
    assert (idless.task_id, idless.assignment_id, idless.display_ids) == ("wi_idless", "asg_idless", ())
    assert report.preview("sha:abcd", fleet_uid="fleet_a").status == "not_found"
    named = report.preview("old-ticket", fleet_uid="fleet_a").mapping
    assert (named.task_id, named.assignment_id) == ("wi_named", "asg_named")
    assert report.preview("asg_private", fleet_uid="fleet_a").total_matches == 0
    with pytest.raises(ValueError, match="fleet_uid"):
        report.preview("old-ticket", fleet_uid="")


def test_old_terminal_work_never_reenters_intake_after_later_progress(conn):
    for terminal in ("completed", "returned_blocked", "expired", "superseded",
                     "failed", "cancelled", "reassigned"):
        tid, aid = f"wi_{terminal}", f"asg_{terminal}"
        _task(conn, tid)
        _assignment(conn, aid, tid)
        _event(conn, tid, aid, terminal)
        _event(conn, tid, aid, "progress")
    _task(conn, "wi_cancelled_intake")
    _event(conn, "wi_cancelled_intake", None, "cancelled")

    report = audit_tasks(conn)
    assert report.counts["closed_tasks"] == 8
    assert report.counts["closed_assignments"] == 7
    assert report.counts["active_tasks"] == report.counts["current_assignments"] == 0
    assert not report.blockers
    assert all(not r.active and not r.resumable for r in report.references)
    assert report.preview("asg_returned_blocked", fleet_uid="fleet_a").mapping.task_id == "wi_returned_blocked"
    assert report.preview("wi_expired", fleet_uid="fleet_a", active_only=True).status == "not_found"


def test_unscoped_dangling_and_cross_fleet_links_separate_active_from_history(conn):
    _task(conn, "wi_unscoped", fleet=None)
    _assignment(conn, "asg_unscoped", "wi_unscoped")
    for suffix in ("active", "closed"):
        _task(conn, f"wi_cross_{suffix}")
        _assignment(conn, f"asg_cross_{suffix}", f"wi_cross_{suffix}", fleet="fleet_b")
        _assignment(conn, f"asg_dangling_{suffix}", f"wi_missing_{suffix}")
    _event(conn, "wi_cross_closed", "asg_cross_closed", "completed")
    _event(conn, "wi_missing_closed", "asg_dangling_closed", "failed")

    report = audit_tasks(conn)
    assert report.counts["scoped_tasks"] == 2
    assert report.counts["unscoped_tasks"] == 1
    assert report.counts["dangling_assignments"] == report.counts["cross_fleet_assignments"] == 2
    assert {i.assignment_ids[0] for i in report.blockers
            if i.code in ("dangling_assignment", "cross_fleet_assignment")} == {
                "asg_dangling_active", "asg_cross_active"}
    historical = [i for i in report.issues if i.code in ("dangling_assignment", "cross_fleet_assignment") and not i.blocking]
    assert len(historical) == 2
    # Historical cross-fleet links expose exact IDs, never silently rehome them.
    cross = report.preview("asg_cross_active", fleet_uid="fleet_a").mapping
    assert (cross.task_id, cross.fleet_uid, cross.assignment_fleet_uid) == ("wi_cross_active", "fleet_a", "fleet_b")
    assert cross.blockers == ("cross_fleet_assignment",) and not cross.resumable
    assert report.preview("asg_cross_active", fleet_uid="fleet_b").status == "not_found"
    assert report.preview("asg_unscoped", fleet_uid="fleet_a").status == "not_found"


def test_multiple_current_assignments_and_display_collisions_never_choose_latest(conn):
    _task(conn, "wi_multi", ref="dispatch-log:repeat")
    _assignment(conn, "asg_first", "wi_multi", ref="dispatch-log:repeat")
    _assignment(conn, "asg_second", "wi_multi", ref="dispatch-log:repeat")
    _task(conn, "wi_other", ref="dispatch-log:repeat")
    _assignment(conn, "asg_other", "wi_other", ref="dispatch-log:repeat")

    report = audit_tasks(conn)
    assert report.counts["multiple_current_tasks"] == 1
    assert report.counts["ambiguous_active_display_ids"] == 1
    preview = report.preview("repeat", fleet_uid="fleet_a", limit=1)
    assert preview.status == "ambiguous" and preview.total_matches == 3
    assert len(preview.candidates) == 1 and preview.mapping is None
    assert report.preview("wi_multi", fleet_uid="fleet_a").mapping is None
    exact = report.preview("asg_first", fleet_uid="fleet_a").mapping
    assert "multiple_current_assignments" in exact.blockers and not exact.resumable


def test_legacy_redispatch_closure_disagreement_blocks_without_reopening(conn):
    for suffix in ("closed", "current"):
        _task(conn, f"wi_{suffix}", ref="dispatch-log:redispatch")
        _assignment(conn, f"asg_{suffix}", f"wi_{suffix}", ref="dispatch-log:redispatch")
    _event(conn, "wi_closed", "asg_closed", "returned_blocked")

    report = audit_tasks(conn)
    assert report.counts["current_assignments"] == report.counts["closed_assignments"] == 1
    assert report.counts["ambiguous_active_display_ids"] == 0
    assert [i.code for i in report.blockers] == ["legacy_display_closure_disagreement"]
    assert report.preview("redispatch", fleet_uid="fleet_a").status == "ambiguous"
    current = report.preview("redispatch", fleet_uid="fleet_a", active_only=True).mapping
    assert current.assignment_id == "asg_current" and not current.resumable
    assert not report.preview("asg_closed", fleet_uid="fleet_a").mapping.active


def test_existing_terminal_task_with_another_open_assignment_is_unresolved(conn):
    _task(conn, "wi_terminal")
    _assignment(conn, "asg_closed", "wi_terminal")
    _event(conn, "wi_terminal", "asg_closed", "expired")
    _assignment(conn, "asg_current", "wi_terminal")
    report = audit_tasks(conn)
    assert report.counts["closed_tasks"] == 1
    assert report.preview("asg_current", fleet_uid="fleet_a").mapping.blockers == (
        "current_assignment_on_terminal_task",)


def test_terminal_event_with_wrong_task_link_does_not_certify_parent_closed(conn):
    _task(conn, "wi_actual")
    _task(conn, "wi_wrong")
    _assignment(conn, "asg_actual", "wi_actual")
    _event(conn, "wi_wrong", "asg_actual", "failed")
    report = audit_tasks(conn)
    assert report.counts["closed_assignments"] == 1
    assert [issue.code for issue in report.blockers] == ["unresolved_task_event"]
    assert report.preview("asg_actual", fleet_uid="fleet_a").mapping.blockers == (
        "unresolved_task_event",)


def test_audit_is_repeatable_read_only_and_preserves_caller_transaction(conn):
    _task(conn, "wi_intake")
    _insert(conn, "events", kind="system", event="reports_acked", detail='{"acked_through_seq":17}')
    conn.execute("BEGIN")
    _assignment(conn, "asg_pending", "wi_intake")
    before = tuple(conn.iterdump())
    changes = conn.total_changes
    # Authorizer denies every durable change, while allowing the read-only
    # schema-version pragma. The caller's uncommitted row must stay uncommitted.
    denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE}
    conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
    first = audit_tasks(conn)
    assert first == audit_tasks(conn)
    assert conn.in_transaction and conn.total_changes == changes
    assert tuple(conn.iterdump()) == before
    # Passing None to disable the authorizer is supported only since 3.11.
    conn.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
    conn.rollback()
    assert audit_tasks(conn).counts["assignments"] == 0


def test_root_reader_never_creates_missing_database_and_keeps_file_bytes(tmp_path):
    root = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        audit_root(root)
    assert not root.exists()
    db = db_file(root)
    db.parent.mkdir(parents=True)
    connection = sqlite3.connect(db)
    migrate(connection)
    _task(connection, "wi_present")
    connection.close()
    before = db.read_bytes()
    assert audit_root(root).counts["tasks"] == 1
    assert db.read_bytes() == before


def test_empty_and_unrecognized_formats_are_not_migrated():
    with sqlite3.connect(":memory:") as connection:
        empty = audit_tasks(connection)
        assert empty.schema_version == 0 and empty.counts["tasks"] == 0
        assert not connection.in_transaction
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
        connection.execute("PRAGMA user_version=13")
        with pytest.raises(TaskAuditError, match="schema: 13"):
            audit_tasks(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13


def test_new_producer_is_not_reinterpreted_as_historical_terminal_work(conn):
    _task(conn, "wi_new")
    conn.execute("UPDATE work_items SET emitter='claudlobby.tasks.v1'")
    with pytest.raises(TaskAuditError, match="task-state reducer"):
        audit_tasks(conn)
