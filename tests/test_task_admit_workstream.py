"""Every new task belongs to a workstream (#1053): admit opens the task's own, or joins an open one.

Admit without --workstream records a one-to-one workstream in the task's own batch, so both land or
neither does. With --workstream it joins, and refuses a workstream that is archived, closed by the
manager, or closed because every task in it has ended, rechecked as the batch's precondition under
the plane's write lock. The cap leaves one-to-one workstreams out, `workstream open --id` refuses
their ID form, the task verbs' results print the task's workstream, and the manager alone is told
to file follow-ups into the open workstream.
"""
from dataclasses import replace
import sqlite3
from uuid import uuid4

import pytest

from claudlobby import task_operations as tasks
from claudlobby.application_tasks import task_outcome
from claudlobby.composer import compose_claude_md
from claudlobby.config import ProjectConfig, WorkstreamsConfig
from claudlobby.plane.db import db_file
from claudlobby.plane.emit_api import emit_batch
from claudlobby.report_payload import ReportPayload
from claudlobby.workstream_operations import (TASK_WORKSTREAM_ID, WorkstreamError, _reader,
                                              apply as workstream_apply, task_workstream_id)
from tests.conftest import install_real_template, load_test_fleet, make_paths
from tests.test_task_operations import _receipt, estate  # noqa: F401 — private real-Plane fixture

RULE = "File a task's related follow-up into its workstream before that task closes"


def _open(ctx, title="Release review", project=None):
    opened = workstream_apply(ctx, "open", {"title": title, "id": None, "project": project,
                                            "owner": None, "next": None}, request_id=str(uuid4()))
    return opened.workstream_ids[0]


def _rows(conn, table, column, value):
    return conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (value,)).fetchall()


def _workstream(conn, wid):
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT w.title, w.project_key, w.source_ref, o.alias AS owner,"
                           " b.alias AS opened_by FROM workstreams w"
                           " JOIN identity_registry o ON o.uid = w.owner_uid"
                           " JOIN identity_registry b ON b.uid = w.opened_by_uid"
                           " WHERE w.workstream_id=?", (wid,)).fetchone()
    finally:
        conn.row_factory = None
    return dict(row) if row else None


def test_admit_records_the_tasks_own_workstream_in_the_same_batch(estate):
    ctx, conn = estate
    worker = replace(ctx, caller=ctx.bots["worker"])
    request = str(uuid4())
    result = tasks.admit(worker, request, title="Fix the flaky test", project_key="shop")
    wid = task_workstream_id(result.task_id)
    assert TASK_WORKSTREAM_ID.fullmatch(wid) and wid == "ws_" + result.task_id[3:]
    assert result.task.workstream_id == wid
    # Owned by the fleet's manager, opened by whoever admitted the task, titled like it.
    assert _workstream(conn, wid) == {"title": "Fix the flaky test", "project_key": "shop",
                                      "source_ref": f"request:{request}",
                                      "owner": "bot:example/manager", "opened_by": "bot:example/worker"}
    task_ref = conn.execute("SELECT workstream_id, source_ref FROM work_items WHERE work_item_id=?",
                            (result.task_id,)).fetchone()
    assert task_ref == (wid, f"request:{request}")
    facts = _receipt(ctx, request).intent.stages[0].facts
    assert [fact.family for fact in facts] == ["workstream", "work_item"]
    # The registry reads it like any other workstream.
    assert _reader(ctx, 14, or_empty=False)["workstreams"][wid]["title"] == "Fix the flaky test"


def test_a_failed_batch_records_neither_the_workstream_nor_the_task(estate):
    ctx, conn = estate
    conn.execute("CREATE TRIGGER refuse_task BEFORE INSERT ON work_items"
                 " BEGIN SELECT RAISE(ABORT, 'refused by the test'); END")
    conn.commit()
    before = conn.execute("SELECT count(*) FROM workstreams").fetchone()[0]
    request = str(uuid4())
    with pytest.raises(tasks.TaskRecordingError):
        tasks.admit(ctx, request, title="Never lands")
    # The batch held the workstream's row, inserted before the task's, and it rolled back with it.
    assert [fact.family for fact in _receipt(ctx, request).intent.stages[0].facts] == ["workstream", "work_item"]
    assert conn.execute("SELECT count(*) FROM workstreams").fetchone()[0] == before
    assert conn.execute("SELECT count(*) FROM work_items").fetchone()[0] == 0


def test_a_replay_records_nothing_new(estate):
    ctx, conn = estate
    request = str(uuid4())
    first = tasks.admit(ctx, request, title="Once")
    assert TASK_WORKSTREAM_ID.fullmatch(first.task.workstream_id or "")
    counts = [conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
              for table in ("workstreams", "work_items", "ingest_ledger")]
    again = tasks.admit(ctx, request, title="Once")
    assert again.replayed and again.task_id == first.task_id
    assert again.task.workstream_id == first.task.workstream_id
    assert [conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("workstreams", "work_items", "ingest_ledger")] == counts


def test_a_join_lands_in_an_open_workstream_and_opens_none(estate):
    ctx, conn = estate
    wid = _open(ctx)
    before = conn.execute("SELECT count(*) FROM workstreams").fetchone()[0]
    joined = tasks.admit(ctx, str(uuid4()), title="Its follow-up", workstream_id=wid)
    assert joined.task.workstream_id == wid
    assert conn.execute("SELECT count(*) FROM workstreams").fetchone()[0] == before
    assert len(_receipt(ctx, joined.request_id).intent.stages[0].facts) == 1
    # A task's own workstream takes a follow-up while that task is open.
    first = tasks.admit(ctx, str(uuid4()), title="First")
    assert TASK_WORKSTREAM_ID.fullmatch(first.task.workstream_id or "")
    second = tasks.admit(ctx, str(uuid4()), title="Second", workstream_id=first.task.workstream_id)
    assert second.task.workstream_id == first.task.workstream_id


def test_admit_refuses_a_workstream_the_manager_closed(estate):
    ctx, conn = estate
    wid = _open(ctx)
    workstream_apply(ctx, "close", {"id": wid, "status": "done"}, request_id=str(uuid4()))
    with pytest.raises(tasks.WorkstreamClosedError, match="workstream is closed"):
        tasks.admit(ctx, str(uuid4()), title="Too late", workstream_id=wid)
    assert _rows(conn, "work_items", "workstream_id", wid) == []


def test_admit_refuses_a_workstream_whose_tasks_have_all_ended(estate):
    ctx, conn = estate
    first = tasks.admit(ctx, str(uuid4()), title="Short-lived")
    wid = first.task.workstream_id
    tasks.withdraw(ctx, str(uuid4()), first.task_id, reason="Not needed")
    with pytest.raises(tasks.WorkstreamClosedError, match="every task in it has ended"):
        tasks.admit(ctx, str(uuid4()), title="Follow-up", workstream_id=wid)
    assert [row[0] for row in conn.execute(
        "SELECT work_item_id FROM work_items WHERE workstream_id=?", (wid,))] == [first.task_id]
    # Completion ends a task the same way.
    done = tasks.admit(ctx, str(uuid4()), title="Done soon")
    assigned = tasks.assign(ctx, str(uuid4()), done.task_id, bot_id="worker")
    worker = replace(ctx, caller=ctx.bots["worker"])
    tasks.accept(worker, str(uuid4()), assigned.assignment_id)
    tasks.complete(worker, str(uuid4()), assigned.assignment_id, ReportPayload("completed", summary="Done"))
    with pytest.raises(tasks.WorkstreamClosedError, match="every task in it has ended"):
        tasks.admit(ctx, str(uuid4()), title="After it", workstream_id=done.task.workstream_id)


def test_admit_refuses_an_archived_workstream(estate):
    ctx, _conn = estate
    wid = _open(ctx)
    workstream_apply(ctx, "close", {"id": wid, "status": "abandoned"}, request_id=str(uuid4()))
    workstream_apply(ctx, "prune", {}, request_id=str(uuid4()))
    with pytest.raises(tasks.WorkstreamClosedError, match="workstream is archived"):
        tasks.admit(ctx, str(uuid4()), title="Too late", workstream_id=wid)


def test_admit_refuses_another_fleets_or_another_projects_workstream(estate):
    ctx, _conn = estate
    emit_batch(ctx.root, [{"event_type": "workstream", "emitter": "t", "fleet": "elsewhere",
                           "payload": {"workstream_id": "ws-elsewhere", "title": "Theirs",
                                       "opened_by": "bot:elsewhere/manager"}}], require_commit=True)
    with pytest.raises(tasks.TaskQueryError, match="does not belong to the selected fleet"):
        tasks.admit(ctx, str(uuid4()), title="Not ours", workstream_id="ws-elsewhere")
    fleet = replace(ctx.context.fleet, projects={
        **ctx.context.fleet.projects, "blog": ProjectConfig("blog", "Blog", repos=["owner/blog"])})
    ctx = replace(ctx, context=replace(ctx.context, fleet=fleet))
    wid = _open(ctx, project="shop")
    with pytest.raises(tasks.TaskQueryError, match="workstream project differs"):
        tasks.admit(ctx, str(uuid4()), title="Wrong project", project_key="blog", workstream_id=wid)


def test_a_close_between_the_read_and_the_write_is_refused_under_the_write_lock(estate, monkeypatch):
    ctx, conn = estate
    wid = _open(ctx)
    real = tasks._require_open_workstream
    calls = []

    def close_after_the_read(op_ctx, check_conn, workstream_id):
        calls.append(workstream_id)
        real(op_ctx, check_conn, workstream_id)
        if len(calls) == 1:  # the read passed; the close lands before the batch is written
            workstream_apply(ctx, "close", {"id": wid, "status": "done"}, request_id=str(uuid4()))

    monkeypatch.setattr(tasks, "_require_open_workstream", close_after_the_read)
    request = str(uuid4())
    with pytest.raises(tasks.WorkstreamClosedError, match="workstream is closed"):
        tasks.admit(ctx, request, title="Raced", workstream_id=wid)
    assert calls == [wid, wid]  # the precondition ran, and it is what refused
    assert _rows(conn, "work_items", "workstream_id", wid) == []
    receipt = _receipt(ctx, request)
    assert receipt.stages[0].status == "unrecorded"  # definitely absent, so a retry is safe
    monkeypatch.setattr(tasks, "_require_open_workstream", real)
    with pytest.raises(tasks.WorkstreamClosedError, match="workstream is closed"):
        tasks.admit(ctx, request, title="Raced", workstream_id=wid)


def test_one_to_one_workstreams_do_not_count_against_the_cap(estate):
    ctx, _conn = estate
    fleet = replace(ctx.context.fleet, workstreams=WorkstreamsConfig(max_active=1, lease_days=14))
    ctx = replace(ctx, context=replace(ctx.context, fleet=fleet))
    admitted = [tasks.admit(ctx, str(uuid4()), title=title) for title in ("One", "Two", "Three")]
    registry = _reader(ctx, 14, or_empty=False)["workstreams"]
    assert all(registry[task.task.workstream_id]["status"] == "active" for task in admitted)
    _open(ctx, "The one opened by hand")  # three active task workstreams, and still under a cap of 1
    with pytest.raises(WorkstreamError, match="at the cap"):
        _open(ctx, "A second one by hand")


def test_workstream_open_refuses_the_task_workstream_id_form(estate):
    ctx, _conn = estate
    with pytest.raises(WorkstreamError, match="task's own workstream") as refused:
        workstream_apply(ctx, "open", {"title": "Squat", "id": "ws_" + "a" * 32, "project": None,
                                       "owner": None, "next": None}, request_id=str(uuid4()))
    assert refused.value.code == "invalid_argument"
    assert _open(ctx, "Plain") == "ws-plain"  # the slug form is untouched


def test_admit_assign_accept_and_the_reports_print_the_tasks_workstream(estate):
    ctx, _conn = estate
    worker = replace(ctx, caller=ctx.bots["worker"])
    admitted = tasks.admit(ctx, str(uuid4()), title="Printed")
    wid = admitted.task.workstream_id
    assigned = tasks.assign(ctx, str(uuid4()), admitted.task_id, bot_id="worker")
    accepted = tasks.accept(worker, str(uuid4()), assigned.assignment_id)
    progressed = tasks.progress(worker, str(uuid4()), assigned.assignment_id,
                                ReportPayload("progress", summary="Halfway"))
    completed = tasks.complete(worker, str(uuid4()), assigned.assignment_id,
                               ReportPayload("completed", summary="Done"))
    for result in (admitted, assigned, accepted, progressed, completed):
        data = task_outcome(result, fleet_name="example", release_id=None).data
        assert data["workstream_id"] == wid, result


def test_the_manager_alone_is_told_to_file_follow_ups_into_the_workstream(fleet_dir):
    install_real_template(fleet_dir)
    fleet = load_test_fleet(fleet_dir)
    paths = make_paths(fleet_dir)
    texts = {bot.bot_id: compose_claude_md(bot, fleet, paths) for bot in fleet.bots.values()}
    assert len(texts) >= 2 and fleet.manager in texts
    for bot_id, text in texts.items():
        if bot_id == fleet.manager:
            assert text.count("\n## Workstreams\n") == 1 and RULE in text
        else:
            assert "\n## Workstreams\n" not in text and RULE not in text, bot_id
