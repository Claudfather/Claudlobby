"""The workstream backfill files each open task that has no open workstream into its own (#1053).

A task admitted before every task had a workstream has none, and a close once left its open tasks
in the workstream it closed. The backfill gives each such task its own workstream, the one `task
admit` now opens for every new task: one batch records, for each task, that workstream's row (the
row admit writes) and a move into it (the row `task move` writes). A task in an open workstream
stays there and an ended task is never filed. A task the task reducer cannot resolve, or whose own
workstream already exists, is left and named. The batch's precondition rereads the tasks under the
plane's write lock, and the backfill holds each task's lock through its write. Only the fleet's
manager or a local person runs it. Its command is `migration workstream-backfill`
(`test_workstream_backfill_cli.py`).
"""
from contextlib import contextmanager
from dataclasses import replace
from unittest import mock
from uuid import uuid4

import pytest

from claudlobby import task_operations as tasks
from claudlobby import workstream_operations as ws
from claudlobby.plane import emit_api
from claudlobby.plane.ids import mint_assignment_id, mint_work_item_id
from claudlobby.request_receipts import locked_request
from claudlobby.task_state import TASK_EMITTER, read_tasks
from tests.test_task_operations import _receipt, estate  # noqa: F401 — private real-Plane fixture
from tests.test_workstream_close import replace_lock_after_taking


@contextmanager
def before_a_live_task_needed_a_workstream():
    """Rows written as the plane took them before it refused a live task that names no
    workstream (#2852), which is how every task the backfill files was recorded. The
    contract's other checks still run: the row is validated as legacy, which the rule
    exempts, and stored live, as those tasks were."""
    real = emit_api.validate_request

    def validate(raw):
        if raw.get("event_type") == "work_item" and raw.get("origin", "live") == "live":
            env, payload = real({**raw, "origin": "legacy"})
            return env.model_copy(update={"origin": "live"}), payload
        return real(raw)

    with mock.patch.object(emit_api, "validate_request", validate):
        yield


def _legacy(ctx, title="Before workstreams", project=None):
    """A task as admit recorded it before every task had a workstream: a live row that names none."""
    task_id = mint_work_item_id()
    with before_a_live_task_needed_a_workstream():
        emit_api.emit_batch(ctx.root, [{
            "event_type": "work_item", "emitter": TASK_EMITTER, "fleet": "example",
            "payload": {"work_item_id": task_id, "title": title, "created_by": "bot:example/manager",
                        **({"project_key": project} if project else {})}}], require_commit=True)
    return task_id


def _closed_the_old_way(ctx, wid):
    """A close as it was recorded before a close checked its tasks: one row, which left the
    workstream's open tasks in it."""
    emit_api.emit_batch(ctx.root, [{
        "event_type": "workstream_event", "emitter": "workstream-operation", "fleet": "example",
        "payload": {"workstream_id": wid, "actor": "bot:example/manager", "event": "closed",
                    "disposition": "abandoned"}}], require_commit=True)


def _open(ctx, title="Release review"):
    return ws.apply(ctx, "open", {"title": title, "id": None, "project": None, "owner": None,
                                  "next": None}, request_id=str(uuid4())).workstream_ids[0]


def _backfill(ctx, request_id=None):
    return ws.apply(ctx, "backfill", {}, request_id=request_id or str(uuid4()))


def _task(ctx, conn, task_id):
    task, = read_tasks(conn, fleet_uid=ctx.fleet_uid, task_ids=[task_id]).tasks
    return task


def _registry(ctx):
    return ws._reader(ctx, 14, or_empty=False)["workstreams"]


def _rows_of(conn, request_id):
    """Every row the request recorded: each workstream's own row lands in `workstreams`, and each
    move in `events`."""
    ref = (f"request:{request_id}",)
    return (conn.execute("SELECT count(*) FROM workstreams WHERE source_ref=?", ref).fetchone()[0]
            + conn.execute("SELECT count(*) FROM events WHERE source_ref=?", ref).fetchone()[0])


def _accepted(ctx, task_id):
    routed = tasks.assign(ctx, str(uuid4()), task_id, bot_id="worker")
    tasks.accept(replace(ctx, caller=ctx.bots["worker"]), str(uuid4()), routed.assignment_id)
    return routed.assignment_id


def _batches(monkeypatch):
    """Record every batch handed to the plane, then write it."""
    seen, real = [], emit_api.emit_batch

    def recorded(root, raws, **kwargs):
        seen.append([dict(raw) for raw in raws])
        return real(root, raws, **kwargs)

    monkeypatch.setattr(emit_api, "emit_batch", recorded)
    return seen


def test_the_plan_names_each_open_task_with_no_open_workstream_and_no_other(estate):
    ctx, conn = estate
    none = _legacy(ctx, "No workstream")
    ended = _legacy(ctx, "Ended")
    tasks.withdraw(ctx, str(uuid4()), ended, reason="Not needed")
    stale = _open(ctx, "Closed long ago")
    stranded = tasks.admit(ctx, str(uuid4()), title="Stranded", workstream_id=stale).task_id
    _closed_the_old_way(ctx, stale)
    live = _open(ctx, "Still open")
    tasks.admit(ctx, str(uuid4()), title="Staying", workstream_id=live)
    tasks.admit(ctx, str(uuid4()), title="Admitted with its own")
    plan = ws.backfill_plan(conn, fleet_uid=ctx.fleet_uid)
    assert [(item.task_id, item.from_workstream_id, item.workstream_id) for item in plan.tasks] == sorted([
        (none, None, ws.task_workstream_id(none)), (stranded, stale, ws.task_workstream_id(stranded))])
    assert plan.left == ()
    # An archived workstream reads closed too.
    tasks.withdraw(ctx, str(uuid4()), none, reason="Not needed either")
    ws.apply(ctx, "prune", {}, request_id=str(uuid4()))
    assert [(item.task_id, item.from_workstream_id) for item in
            ws.backfill_plan(conn, fleet_uid=ctx.fleet_uid).tasks] == [(stranded, stale)]


def test_the_backfill_files_each_task_into_its_own_workstream_in_one_batch(estate, monkeypatch):
    ctx, conn = estate
    queued = _legacy(ctx, "Queued before workstreams", project="shop")
    active = _legacy(ctx, "Active before workstreams")
    assignment_id = _accepted(ctx, active)
    stale = _open(ctx, "Abandoned plan")
    stranded = tasks.admit(ctx, str(uuid4()), title="Stranded", workstream_id=stale).task_id
    _closed_the_old_way(ctx, stale)
    live = _open(ctx, "Live plan")
    staying = tasks.admit(ctx, str(uuid4()), title="Staying", workstream_id=live).task_id
    batches = _batches(monkeypatch)
    request = str(uuid4())
    result = _backfill(ctx, request)
    filed = sorted([(queued, None), (active, None), (stranded, stale)])
    assert [(task.task_id, task.from_workstream_id) for task in result.filed_tasks] == filed
    assert result.workstream_ids == tuple(ws.task_workstream_id(task_id) for task_id, _ in filed)
    assert [task.workstream_id for task in result.filed_tasks] == list(result.workstream_ids)
    assert (result.recording, result.replayed, result.withdrawn_task_ids) == ("committed", False, ())
    # One batch: for each task, the row admit writes for its own workstream, then a move into it.
    raws, = batches
    assert [(raw["event_type"], raw["emitter"]) for raw in raws] == [
        ("workstream", TASK_EMITTER), ("system", TASK_EMITTER)] * 3
    assert _rows_of(conn, request) == 6
    for (task_id, left), row, move in zip(filed, raws[::2], raws[1::2]):
        own = ws.task_workstream_id(task_id)
        task = _task(ctx, conn, task_id)
        assert row["payload"] == {"workstream_id": own, "title": task.title,
                                  "owner": "bot:example/manager", "opened_by": "bot:example/manager",
                                  "project_key": task.project_key}
        assert move["payload"]["event"] == "task_moved"
        assert move["payload"]["data"] == {"task_id": task_id, "from_workstream_id": left,
                                           "to_workstream_id": own, "actor": "bot:example/manager",
                                           "reason": ws.BACKFILL_REASON}
        # Every read takes the new workstream: the task, the registry and admit's join check.
        assert task.workstream_id == own
        entry = _registry(ctx)[own]
        assert (entry["status"], entry["task_ids"]) == ("active", [task_id])
        tasks._require_open_workstream(ctx, conn, own)
    assert _task(ctx, conn, queued).project_key == "shop"
    assert stranded not in _registry(ctx)[stale]["task_ids"]
    # The active task keeps its assignment; the task in an open workstream stays there.
    kept = _task(ctx, conn, active)
    assert (kept.state, kept.current_assignment.assignment_id) == ("active", assignment_id)
    assert _task(ctx, conn, staying).workstream_id == live
    # The same request replays its result and writes nothing more.
    again = _backfill(ctx, request)
    assert again.replayed and again.recording == "committed"
    assert (again.workstream_ids, again.filed_tasks) == (result.workstream_ids, result.filed_tasks)
    assert len(batches) == 1 and _rows_of(conn, request) == 6
    # Run again later, it finds nothing to file, prepares no request and records nothing.
    after = _backfill(ctx)
    assert (after.recording, after.filed_tasks, after.request_persisted) == ("not_attempted", (), False)
    assert _receipt(ctx, after.request_id) is None and len(batches) == 1


def test_a_task_the_reducer_cannot_resolve_is_left_and_named(estate):
    ctx, conn = estate
    task_id = _legacy(ctx, "Unreadable")
    # An assignment from a producer the reducer does not know leaves the task unresolved.
    emit_api.emit_batch(ctx.root, [{
        "event_type": "assignment", "emitter": "claudlobby.tasks.v2", "fleet": "example",
        "payload": {"assignment_id": mint_assignment_id(), "work_item_id": task_id,
                    "assignee": "bot:example/worker", "assigned_by": "bot:example/manager"}}],
        require_commit=True)
    plan = ws.backfill_plan(conn, fleet_uid=ctx.fleet_uid)
    (left_id, why), = plan.left
    assert plan.tasks == () and left_id == task_id
    assert why.startswith("the task reducer cannot resolve it: ") and "unknown_task_producer" in why
    result = _backfill(ctx)
    assert result.recording == "not_attempted" and _task(ctx, conn, task_id).workstream_id is None


def test_a_task_whose_own_workstream_exists_is_left_and_named(estate):
    ctx, conn = estate
    admitted = tasks.admit(ctx, str(uuid4()), title="Moved, then stranded")
    plan_id = _open(ctx, "A plan")
    tasks.move(ctx, str(uuid4()), admitted.task_id, workstream_id=plan_id, reason="Belongs there")
    _closed_the_old_way(ctx, plan_id)
    own = admitted.task.workstream_id
    plan = ws.backfill_plan(conn, fleet_uid=ctx.fleet_uid)
    assert plan.tasks == ()
    assert plan.left == ((admitted.task_id, f"its own workstream {own} already exists"),)


@pytest.mark.parametrize("change", ["withdrawn", "moved"])
def test_a_task_changed_between_the_read_and_the_write_refuses_the_whole_batch(estate, monkeypatch,
                                                                               change):
    ctx, conn = estate
    first, second = sorted((_legacy(ctx, "First"), _legacy(ctx, "Second")))
    target = _open(ctx, "Elsewhere")
    real = ws._backfill_read
    reads = []

    def changed_after_the_read(op_ctx):
        plan = real(op_ctx)
        reads.append([item.task_id for item in plan.tasks])
        if change == "withdrawn":
            tasks.withdraw(ctx, str(uuid4()), second, reason="Not needed")
        else:
            tasks.move(ctx, str(uuid4()), second, workstream_id=target, reason="Belongs there")
        return plan

    monkeypatch.setattr(ws, "_backfill_read", changed_after_the_read)
    request = str(uuid4())
    with pytest.raises(ws.WorkstreamTasksChanged,
                       match=f"tasks changed after the backfill read them: {second};"):
        _backfill(ctx, request)
    assert reads == [[first, second]]
    assert _rows_of(conn, request) == 0 and _task(ctx, conn, first).workstream_id is None
    assert _receipt(ctx, request).stages[0].status == "unrecorded"  # definitely absent
    # Retried once nothing is left to file, the same request ID is refused, not read as a run that
    # found nothing: its unrecorded intent still names the tasks of its first read.
    monkeypatch.setattr(ws, "_backfill_read", real)
    tasks.withdraw(ctx, str(uuid4()), first, reason="Not needed either")
    with pytest.raises(ws.WorkstreamError, match="no longer matches current state") as refused:
        _backfill(ctx, request)
    assert refused.value.code == "conflict" and _rows_of(conn, request) == 0


def test_a_held_task_lock_refuses_the_backfill_and_nothing_is_recorded(estate):
    ctx, conn = estate
    task_id = _legacy(ctx, "Busy")
    request = str(uuid4())
    with locked_request(ctx.root, ctx.fleet_uid, str(uuid4())) as store, \
            tasks._locked_task(store, task_id):
        with pytest.raises(ws.WorkstreamError, match="cannot be filed now") as refused:
            _backfill(ctx, request)
    assert refused.value.code == "conflict"
    assert _rows_of(conn, request) == 0 and _task(ctx, conn, task_id).workstream_id is None
    # Once the lock is free, the same request files it.
    assert [task.task_id for task in _backfill(ctx, request).filed_tasks] == [task_id]


def test_a_task_lock_replaced_while_the_backfill_holds_it_refuses_the_write(estate, monkeypatch):
    ctx, conn = estate
    task_id = _legacy(ctx, "Replaced lock")
    replace_lock_after_taking(monkeypatch, task_id)
    request = str(uuid4())
    with pytest.raises(ws.WorkstreamError, match="task lock was replaced"):
        _backfill(ctx, request)
    assert _rows_of(conn, request) == 0 and _task(ctx, conn, task_id).workstream_id is None


def test_only_the_manager_or_a_local_person_runs_the_backfill(estate):
    ctx, conn = estate
    task_id = _legacy(ctx, "Not a worker's to file")
    request = str(uuid4())
    with pytest.raises(ws.WorkstreamError, match="requires the current manager or local human") as refused:
        _backfill(replace(ctx, caller=ctx.bots["worker"]), request)
    assert refused.value.code == "conflict"
    assert _rows_of(conn, request) == 0 and _task(ctx, conn, task_id).workstream_id is None
