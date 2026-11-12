"""Plane row helpers for task tests, and the reducer's event-scope blockers on both schemas."""

from __future__ import annotations

import sqlite3
import pytest

from claudlobby.plane.migrations import SCHEMA_USER_VERSION, _migration_files, migrate
from claudlobby.task_state import read_tasks


@pytest.fixture(params=[1, SCHEMA_USER_VERSION], ids=["original-kernel", "current-schema"])
def conn(request):
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    if request.param == 1:
        connection.executescript(_migration_files()[0][1])
    else:
        migrate(connection)
    yield connection
    connection.close()


# Four test modules import _insert, _task, _assignment and _event from here:
# tests/fixtures/current_runtime.py, tests/test_migration_apply.py,
# tests/test_operation_context.py and tests/test_plane_cutover_reports.py.
def _insert(conn, table, *, emitter="dispatch-task", **fields):
    seq = conn.execute(
        "INSERT INTO ingest_ledger (event_id, family, ingested_at) VALUES (?, ?, 't')",
        (f"ev_{conn.execute('SELECT COUNT(*) FROM ingest_ledger').fetchone()[0]}", table),
    ).lastrowid
    row = dict(ingest_seq=seq, event_id=f"row_{seq}", schema_version="1.0.0",
               occurred_at="2026-09-01T00:00:00Z", ingested_at="2026-09-01T00:00:00Z",
               host_uid="host_test", emitter=emitter, origin="legacy", **fields)
    conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                 tuple(row.values()))


def _task(conn, tid, *, fleet="fleet_a", ref=None, emitter="dispatch-task"):
    _insert(conn, "work_items", work_item_id=tid, fleet_uid=fleet, source_ref=ref,
            title="Historical work", created_by_uid="actor_not_the_owner", emitter=emitter)


def _assignment(conn, aid, tid, *, fleet="fleet_a", ref=None, worker="actor_worker", emitter="dispatch-task"):
    _insert(conn, "assignments", assignment_id=aid, work_item_id=tid, fleet_uid=fleet,
            source_ref=ref, assignee_uid=worker, assigned_by_uid="actor_not_the_owner", emitter=emitter)


def _event(conn, tid, aid, event, *, emitter="dispatch-task", fleet=None):
    _insert(conn, "events", kind="task", event=event, work_item_id=tid, assignment_id=aid,
            emitter=emitter, fleet_uid=fleet)


def test_out_of_scope_report_events_are_reducer_blockers(conn):
    _task(conn, "wi_report")
    _assignment(conn, "asg_report", "wi_report")
    _event(conn, "wi_report", "asg_report", "progress", emitter="report-back",
           fleet="fleet_b")
    _task(conn, "wi_unlinked")
    _event(conn, "wi_unlinked", None, "progress", emitter="report-back",
           fleet="fleet_a")
    _task(conn, "wi_unscoped")
    _assignment(conn, "asg_unscoped", "wi_unscoped")
    _event(conn, "wi_unscoped", "asg_unscoped", "accepted", emitter="report-back",
           fleet=None)

    assert {issue.code for issue in read_tasks(conn, fleet_uid="fleet_a").blockers} == {
        "cross_fleet_task_event", "unlinked_assignment_event", "unscoped_task_event"}
