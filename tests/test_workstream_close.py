"""A manager's `workstream close` checks the workstream's tasks (#1053).

The close refuses while any task in the workstream holds a live assignment, or is one the task
reducer cannot resolve, and names each: the manager ends it first, and nothing is recorded.
Otherwise it withdraws the workstream's queued tasks in its own batch, one `cancelled` row each,
the row `task withdraw` writes, carrying the close's request and naming the workstream. The
batch's precondition rechecks the workstream's open tasks under the plane's write lock, so a task
assigned between the close's read and its write refuses the close, and nothing is written. The
close holds each queued task's lock through its write, so no task verb acts on a task it is
withdrawing, and checks each lock just before the write, as a task verb checks its own.
"""
from contextlib import contextmanager
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from claudlobby import task_operations as tasks
from claudlobby import workstream_operations as ws
from claudlobby.commands import workstream as workstream_cli
from claudlobby.config import WorkstreamsConfig
from claudlobby.plane import emit_api
from claudlobby.plane.ids import mint_assignment_id
from claudlobby.report_payload import ReportPayload
from claudlobby.request_receipts import locked_request
from claudlobby.task_state import TASK_EMITTER, read_tasks
from tests.test_task_operations import _receipt, estate  # noqa: F401 — private real-Plane fixture


def _open(ctx, title="Release review"):
    return ws.apply(ctx, "open", {"title": title, "id": None, "project": None, "owner": None,
                                  "next": None}, request_id=str(uuid4())).workstream_ids[0]


def _close(ctx, wid, request_id=None, status="done"):
    return ws.apply(ctx, "close", {"id": wid, "status": status},
                    request_id=request_id or str(uuid4()))


def _registry(ctx):
    return ws._reader(ctx, 14, or_empty=False)["workstreams"]


def _task(ctx, conn, task_id):
    task, = read_tasks(conn, fleet_uid=ctx.fleet_uid, task_ids=[task_id]).tasks
    return task


def _closes(conn, wid):
    return conn.execute("SELECT count(*) FROM events WHERE kind='workstream' AND workstream_id=?"
                        " AND event='closed'", (wid,)).fetchone()[0]


def _rows_of(conn, request_id):
    return conn.execute("SELECT count(*) FROM events WHERE source_ref=?",
                        (f"request:{request_id}",)).fetchone()[0]


def _accepted(ctx, task_id):
    routed = tasks.assign(ctx, str(uuid4()), task_id, bot_id="worker")
    worker = replace(ctx, caller=ctx.bots["worker"])
    tasks.accept(worker, str(uuid4()), routed.assignment_id)
    return worker, routed.assignment_id


def _batches(monkeypatch):
    """Record every batch the close hands the plane, then write it."""
    seen, real = [], emit_api.emit_batch

    def recorded(root, raws, **kwargs):
        seen.append([dict(raw) for raw in raws])
        return real(root, raws, **kwargs)

    monkeypatch.setattr(emit_api, "emit_batch", recorded)
    return seen


def test_the_close_is_refused_naming_each_task_with_a_live_assignment(estate):
    ctx, conn = estate
    wid = _open(ctx)
    queued = tasks.admit(ctx, str(uuid4()), title="Queued", workstream_id=wid)
    assigned = tasks.admit(ctx, str(uuid4()), title="Assigned", workstream_id=wid)
    tasks.assign(ctx, str(uuid4()), assigned.task_id, bot_id="worker")
    active = tasks.admit(ctx, str(uuid4()), title="Active", workstream_id=wid)
    _accepted(ctx, active.task_id)
    blocked = tasks.admit(ctx, str(uuid4()), title="Blocked", workstream_id=wid)
    worker, assignment_id = _accepted(ctx, blocked.task_id)
    tasks.block(worker, str(uuid4()), assignment_id, ReportPayload("blocked", reason="Waiting"))
    request = str(uuid4())
    with pytest.raises(ws.WorkstreamError, match="holds tasks the close cannot withdraw") as refused:
        _close(ctx, wid, request)
    assert refused.value.code == "conflict"
    for task, state in ((assigned, "assigned"), (active, "active"), (blocked, "blocked")):
        assert f"{task.task_id} ({state}, a live assignment)" in str(refused.value)
    assert queued.task_id not in str(refused.value)
    # Nothing is recorded: no close, no withdrawal, no request.
    assert _closes(conn, wid) == 0 and _rows_of(conn, request) == 0
    assert _task(ctx, conn, queued.task_id).state == "queued"
    assert _registry(ctx)[wid]["status"] == "active"
    assert _receipt(ctx, request) is None


def test_the_close_is_refused_naming_a_task_the_reducer_cannot_resolve(estate):
    ctx, conn = estate
    wid = _open(ctx)
    task = tasks.admit(ctx, str(uuid4()), title="Unreadable", workstream_id=wid)
    # An assignment from a producer the reducer does not know leaves the task unresolved: it
    # cannot be told queued from assigned.
    emit_api.emit_batch(ctx.root, [{
        "event_type": "assignment", "emitter": "claudlobby.tasks.v2", "fleet": "example",
        "payload": {"assignment_id": mint_assignment_id(), "work_item_id": task.task_id,
                    "assignee": "bot:example/worker", "assigned_by": "bot:example/manager"}}],
        require_commit=True)
    assert _task(ctx, conn, task.task_id).state is None
    with pytest.raises(ws.WorkstreamError, match="cannot resolve") as refused:
        _close(ctx, wid)
    assert f"{task.task_id} (the task reducer cannot resolve it: " in str(refused.value)
    assert "unknown_task_producer" in str(refused.value)
    assert _closes(conn, wid) == 0


def test_the_close_withdraws_each_queued_task_in_its_own_batch_one_row_each(estate, monkeypatch):
    ctx, conn = estate
    wid = _open(ctx)
    first = tasks.admit(ctx, str(uuid4()), title="First", workstream_id=wid)
    second = tasks.admit(ctx, str(uuid4()), title="Second", workstream_id=wid)
    finished = tasks.admit(ctx, str(uuid4()), title="Finished", workstream_id=wid)
    worker, assignment_id = _accepted(ctx, finished.task_id)
    tasks.complete(worker, str(uuid4()), assignment_id, ReportPayload("completed", summary="Done"))
    batches = _batches(monkeypatch)
    request = str(uuid4())
    result = _close(ctx, wid, request, status="abandoned")
    queued = sorted((first.task_id, second.task_id))
    assert result.workstream_ids == (wid,) and result.withdrawn_task_ids == tuple(queued)
    # One batch: the close, then one withdraw row per queued task; the finished task is untouched.
    assert len(batches) == 1
    assert [(raw["event_type"], raw["payload"].get("work_item_id")) for raw in batches[0]] == [
        ("workstream_event", None), *(("task", task_id) for task_id in queued)]
    assert _rows_of(conn, request) == 3
    for task_id in queued:
        task = _task(ctx, conn, task_id)
        end = task.terminal_event
        assert task.state == "cancelled"
        assert (end.event, end.assignment_id, end.emitter) == ("cancelled", None, TASK_EMITTER)
        assert (end.source_ref, end.actor_uid) == (f"request:{request}", ctx.caller.uid)
        assert json.loads(end.detail)["reason"] == (
            f"withdrawn by the close of workstream {wid} (abandoned)")
    assert _task(ctx, conn, finished.task_id).state == "completed"
    entry = _registry(ctx)[wid]
    assert (entry["status"], entry["closed_by"]) == ("abandoned", "bot:example/manager")
    assert entry["open_task_ids"] == []
    # The text view names the withdrawn tasks on one line.
    assert workstream_cli.write_lines("close", result) == (
        f"close: {wid}; recording=committed; replayed=False",
        f"withdrawn by the close: {', '.join(queued)}")
    # A replay of the same request writes nothing more and names the same tasks.
    again = _close(ctx, wid, request, status="abandoned")
    assert again.replayed and again.withdrawn_task_ids == tuple(queued)
    assert len(batches) == 1 and _rows_of(conn, request) == 3


@pytest.mark.parametrize("change", ["assigned", "joined", "withdrawn"])
def test_a_task_changed_between_the_read_and_the_write_refuses_the_close(estate, monkeypatch, change):
    """The precondition rereads the workstream's open tasks under the plane's write lock: a task
    assigned, a task that joins, or a task withdrawn after the close's read refuses the close."""
    ctx, conn = estate
    wid = _open(ctx)
    queued = tasks.admit(ctx, str(uuid4()), title="Raced", workstream_id=wid)
    real = ws._open_tasks
    reads, changed = [], []

    def changed_after_the_read(op_ctx, read_conn, workstream_id):
        found = real(op_ctx, read_conn, workstream_id)
        reads.append(sorted((task.task_id, task.state) for task in found))
        if len(reads) == 1:  # the close read one queued task; it changes before the close's write
            if change == "assigned":
                tasks.assign(ctx, str(uuid4()), queued.task_id, bot_id="worker")
                changed.append(queued.task_id)
            elif change == "joined":
                changed.append(tasks.admit(ctx, str(uuid4()), title="Joined late",
                                           workstream_id=wid).task_id)
            else:
                tasks.withdraw(ctx, str(uuid4()), queued.task_id, reason="Withdrawn first")
                changed.append(queued.task_id)
        return found

    monkeypatch.setattr(ws, "_open_tasks", changed_after_the_read)
    request = str(uuid4())
    with pytest.raises(ws.WorkstreamTasksChanged) as refused:
        _close(ctx, wid, request)
    assert f"tasks changed after the close read them: {changed[0]};" in str(refused.value)
    assert len(reads) == 2 and reads[0] == [(queued.task_id, "queued")]  # the precondition reread
    assert _closes(conn, wid) == 0 and _rows_of(conn, request) == 0
    assert _task(ctx, conn, queued.task_id).state == {"assigned": "assigned", "joined": "queued",
                                                      "withdrawn": "cancelled"}[change]
    assert _receipt(ctx, request).stages[0].status == "unrecorded"  # definitely absent


def test_the_close_holds_each_queued_tasks_lock_through_its_write(estate, monkeypatch):
    ctx, conn = estate
    wid = _open(ctx)
    queued = tasks.admit(ctx, str(uuid4()), title="Locked", workstream_id=wid)
    # A task verb in flight holds the task's lock: the close refuses, and records nothing.
    with locked_request(ctx.root, ctx.fleet_uid, str(uuid4())) as store, \
            tasks._locked_task(store, queued.task_id):
        with pytest.raises(ws.WorkstreamError, match="cannot be withdrawn now"):
            _close(ctx, wid)
    assert _closes(conn, wid) == 0
    # While the close writes, a task verb on that task is refused.
    real = ws._recheck_close
    refused = []

    def assign_during_the_write(op_ctx, workstream_id, withdrawn, write):
        with pytest.raises(tasks.TaskConflictError, match="already being changed"):
            tasks.assign(ctx, str(uuid4()), queued.task_id, bot_id="worker")
        refused.append(withdrawn)
        return real(op_ctx, workstream_id, withdrawn, write)

    monkeypatch.setattr(ws, "_recheck_close", assign_during_the_write)
    result = _close(ctx, wid)
    assert refused == [(queued.task_id,)] and result.withdrawn_task_ids == (queued.task_id,)
    assert _task(ctx, conn, queued.task_id).state == "cancelled"


def replace_lock_after_taking(monkeypatch, task_id):
    """Once a caller holds *task_id*'s lock, put a new lock file at its path, as a cleanup that
    removed the file would. The held lock no longer keeps a task verb out: a verb that opens the
    path now locks the new file."""
    real = tasks._locked_task

    @contextmanager
    def replaced(store, locked_id):
        with real(store, locked_id) as check:
            if locked_id == task_id:
                path = (store.path.parent.parent.parent / "task-locks" / store.fleet_uid
                        / f"{task_id}.lock")
                path.unlink()
                path.touch(mode=0o600)
            yield check

    monkeypatch.setattr(tasks, "_locked_task", replaced)


def test_a_task_lock_replaced_while_the_close_holds_it_refuses_the_close(estate, monkeypatch):
    """The close runs each held lock's check just before its write, as `task_operations._commit`
    runs its one: a lock file replaced while the close held it refuses the close, and nothing is
    recorded."""
    ctx, conn = estate
    wid = _open(ctx)
    queued = tasks.admit(ctx, str(uuid4()), title="Replaced lock", workstream_id=wid)
    replace_lock_after_taking(monkeypatch, queued.task_id)
    request = str(uuid4())
    with pytest.raises(ws.WorkstreamError, match="task lock was replaced") as refused:
        _close(ctx, wid, request)
    assert refused.value.code == "conflict"
    assert _closes(conn, wid) == 0 and _rows_of(conn, request) == 0
    assert _task(ctx, conn, queued.task_id).state == "queued"
    assert _registry(ctx)[wid]["status"] == "active"


def test_a_close_of_a_workstream_with_no_open_task_writes_its_one_row(estate, monkeypatch):
    ctx, _conn = estate
    wid = _open(ctx)
    batches = _batches(monkeypatch)
    result = _close(ctx, wid)
    assert result.workstream_ids == (wid,) and result.withdrawn_task_ids == ()
    raw, = batches[0]
    assert (raw["event_type"], raw["emitter"]) == ("workstream_event", "workstream-operation")
    assert raw["payload"] == {"workstream_id": wid, "actor": "bot:example/manager",
                              "event": "closed", "disposition": "done"}
    assert workstream_cli.write_lines("close", result) == (
        f"close: {wid}; recording=committed; replayed=False",)
    assert (_registry(ctx)[wid]["status"], _registry(ctx)[wid]["closed_by"]) == (
        "done", "bot:example/manager")
    # A workstream whose tasks have all ended reads closed already, and refuses as before.
    ended = tasks.admit(ctx, str(uuid4()), title="Ended")
    tasks.withdraw(ctx, str(uuid4()), ended.task_id, reason="Not needed")
    with pytest.raises(ws.WorkstreamError, match="terminal workstream"):
        _close(ctx, ended.task.workstream_id)


def test_unblock_reopens_a_blocked_workstream_while_task_workstreams_alone_exceed_the_cap(estate):
    """A gap the review of #2850 named: `_active` serves `open` and `unblock`, and only `open`
    was tested with task workstreams. They do not count toward the cap on `unblock` either."""
    ctx, _conn = estate
    fleet = replace(ctx.context.fleet, workstreams=WorkstreamsConfig(max_active=1, lease_days=14))
    ctx = replace(ctx, context=replace(ctx.context, fleet=fleet))
    wid = _open(ctx, "One focus")
    ws.apply(ctx, "block", {"id": wid, "on": "human:reviewer", "note": "Waiting"},
             request_id=str(uuid4()))
    own = [tasks.admit(ctx, str(uuid4()), title=f"Task {n}").task.workstream_id for n in range(2)]
    assert [_registry(ctx)[w]["status"] for w in own] == ["active", "active"]  # 2, over the cap of 1
    unblocked = ws.apply(ctx, "unblock", {"id": wid, "note": "Signed off"}, request_id=str(uuid4()))
    assert unblocked.workstream_ids == (wid,) and _registry(ctx)[wid]["status"] == "active"
    # The opened ones still count: with another one open, unblock is refused at the cap.
    ws.apply(ctx, "block", {"id": wid, "on": "human:reviewer", "note": "Waiting again"},
             request_id=str(uuid4()))
    _open(ctx, "Another focus")
    with pytest.raises(ws.WorkstreamError, match=r"at the cap \(1\)"):
        ws.apply(ctx, "unblock", {"id": wid, "note": "Too many"}, request_id=str(uuid4()))
    assert _registry(ctx)[wid]["status"] == "blocked"
