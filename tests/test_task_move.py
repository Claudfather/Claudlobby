"""A manager moves open work into another workstream (#1053).

`task move` records one system row, `task_moved`, whose subject is the task's fleet. It names the
task, the workstream it left, the one it joined, who moved it and why. Every read takes a task's
workstream from its newest move, else from its admission: the task reducer, the workstream
registry and its lookup door, admit's join check, and a manager's close. A move refuses a target
that is closed (by the manager, or because its tasks ended or moved out), archived, or in another
fleet or project; a task that has ended; and a caller who is neither the fleet's manager nor a
local person. Its batch rechecks the target under the plane's write lock. A workstream whose last
open task moves out reads closed, dated by the move, and a task's own workstream emptied by a
move reads `abandoned`.
"""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from claudlobby import task_operations as tasks
from claudlobby import task_state
from claudlobby import workstream_operations as ws
from claudlobby.application_tasks import task_outcome
from claudlobby.config import ProjectConfig
from claudlobby.plane.emit_api import emit_batch
from claudlobby.report_payload import ReportPayload
from claudlobby.task_queries import show_task
from claudlobby.task_state import TASK_EMITTER, read_tasks
from tests.conftest import load_lib_module
from tests.test_task_operations import _receipt, estate  # noqa: F401 — private real-Plane fixture

LIB = Path(__file__).resolve().parent.parent / "claudlobby/_runtime_scripts"
READERS = load_lib_module("plane-readers")


def _open(ctx, title="Release review", project=None):
    return ws.apply(ctx, "open", {"title": title, "id": None, "project": project, "owner": None,
                                  "next": None}, request_id=str(uuid4())).workstream_ids[0]


def _move(ctx, task_id, workstream_id, request_id=None, reason="Belongs there"):
    return tasks.move(ctx, request_id or str(uuid4()), task_id, workstream_id=workstream_id,
                      reason=reason)


def _registry(ctx):
    return ws._reader(ctx, 14, or_empty=False)["workstreams"]


def _task(ctx, conn, task_id):
    task, = read_tasks(conn, fleet_uid=ctx.fleet_uid, task_ids=[task_id]).tasks
    return task


def _moves(conn):
    return conn.execute("SELECT event_id FROM events WHERE kind='system' AND event='task_moved'"
                        ).fetchall()


def _moved_at(conn, request_id):
    at, = conn.execute("SELECT occurred_at FROM events WHERE source_ref=? AND event='task_moved'",
                       (f"request:{request_id}",)).fetchone()
    return READERS.legacy_ts(at)


def _accepted(ctx, task_id):
    routed = tasks.assign(ctx, str(uuid4()), task_id, bot_id="worker")
    worker = replace(ctx, caller=ctx.bots["worker"])
    tasks.accept(worker, str(uuid4()), routed.assignment_id)
    return worker, routed.assignment_id


def test_a_move_records_one_row_and_every_read_shows_the_new_workstream(estate):
    ctx, conn = estate
    task = tasks.admit(ctx, str(uuid4()), title="Belongs elsewhere")
    own, target = task.task.workstream_id, _open(ctx, "Release")
    request = str(uuid4())
    moved = _move(ctx, task.task_id, target, request, reason="Part of the release")
    assert moved.task.workstream_id == target and not moved.replayed

    rows = conn.execute("SELECT kind, event, emitter, severity, subject_kind, subject_uid,"
                        " subject_alias, detail_truncated, detail FROM events WHERE source_ref=?",
                        (f"request:{request}",)).fetchall()
    assert len(rows) == 1
    assert rows[0][:8] == ("system", "task_moved", TASK_EMITTER, "notice", "fleet", ctx.fleet_uid,
                           "example", 0)
    assert json.loads(rows[0][8]) == {"task_id": task.task_id, "from_workstream_id": own,
                                      "to_workstream_id": target, "actor": ctx.caller.alias,
                                      "reason": "Part of the release"}

    # The task reducer: a read of the task, of the fleet, and the verbs' printout.
    assert _task(ctx, conn, task.task_id).workstream_id == target
    assert show_task(conn, task.task_id, fleet_uid=ctx.fleet_uid).workstream_id == target
    assert read_tasks(conn, fleet_uid=ctx.fleet_uid).get(task.task_id).workstream_id == target
    assert task_outcome(moved, fleet_name="example", release_id=None).data["workstream_id"] == target
    # The registry: the target holds the task, and the task's own workstream reads abandoned.
    registry, at = _registry(ctx), _moved_at(conn, request)
    assert registry[target]["task_ids"] == registry[target]["open_task_ids"] == [task.task_id]
    assert registry[target]["status"] == "active"
    assert (registry[own]["task_ids"], registry[own]["status"], registry[own]["closed_by"],
            registry[own]["closed_ts"]) == ([], "abandoned", f"task:{task.task_id}", at)
    # Admit's join check, and a close's read of the workstream's open tasks.
    joined = tasks.admit(ctx, str(uuid4()), title="Joins the target", workstream_id=target)
    with pytest.raises(tasks.WorkstreamClosedError, match="a move took its last task out of it"):
        tasks.admit(ctx, str(uuid4()), title="Too late", workstream_id=own)
    assert {t.task_id for t in ws._open_tasks(ctx, conn, target)} == {task.task_id, joined.task_id}
    assert ws._open_tasks(ctx, conn, own) == ()

    again = _move(ctx, task.task_id, target, request, reason="Part of the release")
    assert again.replayed and len(_moves(conn)) == 1


def test_moving_the_last_open_task_out_closes_the_workstream_it_left(estate):
    ctx, conn = estate
    wid, target = _open(ctx, "Release"), _open(ctx, "Next release")
    done = tasks.admit(ctx, str(uuid4()), title="Shipped", workstream_id=wid)
    rest = tasks.admit(ctx, str(uuid4()), title="Carried over", workstream_id=wid)
    worker, assignment = _accepted(ctx, done.task_id)
    tasks.complete(worker, str(uuid4()), assignment, ReportPayload("completed", summary="Shipped"))
    assert _registry(ctx)[wid]["status"] == "active"  # the carried-over task is still open

    request = str(uuid4())
    _move(ctx, rest.task_id, target, request, reason="Carried to the next release")
    entry, at = _registry(ctx)[wid], _moved_at(conn, request)
    assert (entry["task_ids"], entry["open_task_ids"], entry["status"], entry["closed_by"],
            entry["closed_ts"], entry["last_progress_ts"]) == (
        [done.task_id], [], "done", f"task:{rest.task_id}", at, at)
    with pytest.raises(tasks.WorkstreamClosedError, match="every task in it has ended or moved out"):
        tasks.admit(ctx, str(uuid4()), title="Too late", workstream_id=wid)
    with pytest.raises(tasks.WorkstreamClosedError,
                       match="ended or moved out; move the task into an open workstream"):
        _move(ctx, rest.task_id, wid, reason="Back again")


def test_a_move_refuses_a_closed_or_archived_target(estate):
    ctx, conn = estate
    task = tasks.admit(ctx, str(uuid4()), title="Looking for a home")
    closed = _open(ctx, "Closed by the manager")
    ws.apply(ctx, "close", {"id": closed, "status": "done"}, request_id=str(uuid4()))
    with pytest.raises(tasks.WorkstreamClosedError,
                       match="workstream is closed; move the task into an open workstream"):
        _move(ctx, task.task_id, closed)
    ended = tasks.admit(ctx, str(uuid4()), title="Ended")
    tasks.withdraw(ctx, str(uuid4()), ended.task_id, reason="Not needed")
    with pytest.raises(tasks.WorkstreamClosedError, match="every task in it has ended; move"):
        _move(ctx, task.task_id, ended.task.workstream_id)
    archived = _open(ctx, "Archived")
    ws.apply(ctx, "close", {"id": archived, "status": "abandoned"}, request_id=str(uuid4()))
    ws.apply(ctx, "prune", {}, request_id=str(uuid4()))
    with pytest.raises(tasks.WorkstreamClosedError, match="workstream is archived"):
        _move(ctx, task.task_id, archived)
    assert _moves(conn) == []
    assert _task(ctx, conn, task.task_id).workstream_id == task.task.workstream_id


def test_a_move_refuses_another_fleet_or_project_an_ended_task_and_a_worker(estate):
    ctx, conn = estate
    emit_batch(ctx.root, [{"event_type": "workstream", "emitter": "t", "fleet": "elsewhere",
                           "payload": {"workstream_id": "ws-elsewhere", "title": "Theirs",
                                       "opened_by": "bot:elsewhere/manager"}}], require_commit=True)
    task = tasks.admit(ctx, str(uuid4()), title="Stays home")
    with pytest.raises(tasks.TaskQueryError, match="does not belong to the selected fleet"):
        _move(ctx, task.task_id, "ws-elsewhere")
    fleet = replace(ctx.context.fleet, projects={
        **ctx.context.fleet.projects, "blog": ProjectConfig("blog", "Blog", repos=["owner/blog"])})
    ctx = replace(ctx, context=replace(ctx.context, fleet=fleet))
    shop = _open(ctx, "Shop work", project="shop")
    blog = tasks.admit(ctx, str(uuid4()), title="Blog post", project_key="blog")
    with pytest.raises(tasks.TaskQueryError, match="workstream project differs"):
        _move(ctx, blog.task_id, shop)
    target = _open(ctx, "Open target")
    with pytest.raises(tasks.TaskConflictError,
                       match="requires the current manager or a local person"):
        _move(replace(ctx, caller=ctx.bots["worker"]), task.task_id, target)
    with pytest.raises(tasks.TaskConflictError, match="already in that workstream"):
        _move(ctx, task.task_id, task.task.workstream_id)
    tasks.withdraw(ctx, str(uuid4()), task.task_id, reason="Not needed")
    with pytest.raises(tasks.TaskConflictError, match="terminal task cannot be moved"):
        _move(ctx, task.task_id, target)
    assert _moves(conn) == []


def test_a_close_of_the_target_between_the_read_and_the_write_refuses_the_move(estate, monkeypatch):
    ctx, conn = estate
    task = tasks.admit(ctx, str(uuid4()), title="Raced")
    target = _open(ctx, "Closing soon")
    real = tasks._require_open_workstream
    calls = []

    def close_after_the_read(op_ctx, check_conn, workstream_id, **kwargs):
        calls.append(workstream_id)
        real(op_ctx, check_conn, workstream_id, **kwargs)
        if len(calls) == 1:  # the read passed; the close lands before the batch is written
            ws.apply(ctx, "close", {"id": target, "status": "done"}, request_id=str(uuid4()))

    monkeypatch.setattr(tasks, "_require_open_workstream", close_after_the_read)
    request = str(uuid4())
    with pytest.raises(tasks.WorkstreamClosedError, match="workstream is closed"):
        _move(ctx, task.task_id, target, request)
    assert calls == [target, target]  # the precondition ran, and it is what refused
    assert _moves(conn) == []
    assert _receipt(ctx, request).stages[0].status == "unrecorded"
    assert _task(ctx, conn, task.task_id).workstream_id == task.task.workstream_id


def test_a_close_reads_a_task_moved_in_and_leaves_one_moved_out(estate):
    ctx, conn = estate
    source, target = _open(ctx, "Source"), _open(ctx, "Target")
    moving = tasks.admit(ctx, str(uuid4()), title="Moves on", workstream_id=source)
    staying = tasks.admit(ctx, str(uuid4()), title="Stays", workstream_id=source)
    _move(ctx, moving.task_id, target)
    assert [t.task_id for t in ws._open_tasks(ctx, conn, source)] == [staying.task_id]
    closed = ws.apply(ctx, "close", {"id": source, "status": "done"}, request_id=str(uuid4()))
    assert closed.withdrawn_task_ids == (staying.task_id,)
    assert _task(ctx, conn, moving.task_id).terminal_event is None  # moved out, untouched
    closed = ws.apply(ctx, "close", {"id": target, "status": "done"}, request_id=str(uuid4()))
    assert closed.withdrawn_task_ids == (moving.task_id,)
    # A task with a live assignment, moved in, refuses its new workstream's close.
    live_source, live_target = _open(ctx, "Live source"), _open(ctx, "Live target")
    live = tasks.admit(ctx, str(uuid4()), title="In flight", workstream_id=live_source)
    _accepted(ctx, live.task_id)
    _move(ctx, live.task_id, live_target)
    with pytest.raises(ws.WorkstreamError, match=live.task_id):
        ws.apply(ctx, "close", {"id": live_target, "status": "done"}, request_id=str(uuid4()))


def test_the_reader_finds_a_move_by_its_fleet_subject_and_no_other_fleets(estate):
    ctx, conn = estate
    task = tasks.admit(ctx, str(uuid4()), title="Ours")
    target = _open(ctx, "Ours too")
    _move(ctx, task.task_id, target)
    data = {"task_id": task.task_id, "from_workstream_id": target, "actor": "bot:elsewhere/manager",
            "reason": "Not ours"}
    emit_batch(ctx.root, [
        {"event_type": "system", "emitter": emitter, "fleet": fleet,
         "payload": {"event": "task_moved", "subject_kind": "fleet", "subject": subject,
                     "data": {**data, "to_workstream_id": joined}}}
        for emitter, fleet, subject, joined in (
            (TASK_EMITTER, "elsewhere", "elsewhere", "ws-theirs"),  # another fleet's own move
            (TASK_EMITTER, "example", "elsewhere", "ws-about-them"),  # about another fleet
            (TASK_EMITTER, "elsewhere", "example", "ws-from-them"),  # recorded by another fleet
            ("another.writer", "example", "example", "ws-forged"))  # not the task door's row
    ], require_commit=True)
    found = task_state.read_moves(conn, fleet_uid=ctx.fleet_uid)
    assert [(m.task_id, m.left, m.joined) for m in found] == [
        (task.task_id, task.task.workstream_id, target)]
    assert _task(ctx, conn, task.task_id).workstream_id == target
    plan = conn.execute("EXPLAIN QUERY PLAN " + task_state._MOVES_SQL,
                        (ctx.fleet_uid, task_state.TASK_MOVED, ctx.fleet_uid, TASK_EMITTER)).fetchall()
    assert any("idx_events_subject" in row["detail"] for row in plan), [tuple(r) for r in plan]


def test_an_unreadable_move_refuses_the_read_rather_than_passing_it_over(estate):
    ctx, conn = estate
    task = tasks.admit(ctx, str(uuid4()), title="Moved, but where to?")
    emit_batch(ctx.root, [{"event_type": "system", "emitter": TASK_EMITTER, "fleet": "example",
                           "payload": {"event": "task_moved", "subject_kind": "fleet",
                                       "subject": "example",
                                       "data": {"task_id": task.task_id,
                                                "from_workstream_id": task.task.workstream_id}}}],
               require_commit=True)
    with pytest.raises(task_state.TaskStateError, match="is unreadable"):
        read_tasks(conn, fleet_uid=ctx.fleet_uid)


def test_newest_activity_comes_from_the_tasks_and_their_moves_not_an_old_opening(estate):
    ctx, conn = estate
    emit_batch(ctx.root, [{"event_type": "workstream", "emitter": "t", "fleet": "example",
                           "occurred_at": "2026-01-01T00:00:00+00:00",
                           "payload": {"workstream_id": wid, "title": title,
                                       "owner": "bot:example/manager",
                                       "opened_by": "bot:example/manager"}}
                          for wid, title in (("ws-old", "Opened long ago"),
                                             ("ws-older", "Also opened long ago"))],
               require_commit=True)
    task = tasks.admit(ctx, str(uuid4()), title="Late work", workstream_id="ws-old")
    entry = _registry(ctx)["ws-old"]
    assert entry["opened_ts"] == "2026-01-01T00:00:00Z"
    assert entry["last_progress_ts"] == READERS.legacy_ts(_task(ctx, conn, task.task_id).occurred_at)
    request = str(uuid4())
    _move(ctx, task.task_id, "ws-older", request)
    registry, at = _registry(ctx), _moved_at(conn, request)
    assert registry["ws-older"]["last_progress_ts"] == at  # the move is the newest row of both
    assert (registry["ws-old"]["last_progress_ts"], registry["ws-old"]["closed_ts"],
            registry["ws-old"]["status"]) == (at, at, "abandoned")


def test_the_lookup_door_reads_the_tasks_and_their_moves(estate):
    ctx, conn = estate
    task = tasks.admit(ctx, str(uuid4()), title="Seen through the door")
    own, target = task.task.workstream_id, _open(ctx, "Door target")
    request = str(uuid4())
    _move(ctx, task.task_id, target, request)
    door = subprocess.run([sys.executable, str(LIB / "plane-lookup.py"), "--root", str(ctx.root),
                           "--workstreams", "--fleet", "example"],
                          capture_output=True, text=True, timeout=120)
    assert door.returncode == 0, door.stderr
    registry = json.loads(door.stdout)["workstreams"]
    assert registry[target]["task_ids"] == registry[target]["open_task_ids"] == [task.task_id]
    assert (registry[own]["task_ids"], registry[own]["status"], registry[own]["closed_by"],
            registry[own]["closed_ts"]) == ([], "abandoned", f"task:{task.task_id}",
                                            _moved_at(conn, request))


def test_a_task_stays_in_its_new_workstream_after_the_one_it_left_is_archived(estate):
    """The registry reads a task a move took into a live workstream even when the workstream it
    was admitted into is no longer live: the case of every task a move files out of a workstream
    that a prune has since archived."""
    ctx, _conn = estate
    task = tasks.admit(ctx, str(uuid4()), title="Outlives its first workstream")
    own, target = task.task.workstream_id, _open(ctx, "Its new home")
    _move(ctx, task.task_id, target)
    ws.apply(ctx, "prune", {}, request_id=str(uuid4()))  # the emptied workstream reads abandoned
    registry = _registry(ctx)
    assert own not in registry
    assert registry[target]["task_ids"] == registry[target]["open_task_ids"] == [task.task_id]


def test_a_reason_within_its_cap_whose_row_ingest_would_cut_is_refused(estate):
    """JSON writes most control characters as six bytes (`\\u00XX`), so a reason within its 4,096
    bytes can make a row past the 16,384 bytes at which ingest cuts a system row's `data`, and one
    cut row would refuse every task read of the fleet. The door measures the row as ingest encodes
    it: a reason dense in control characters, and a row one byte over the cut, are refused with
    nothing written, and a row of exactly the cut is recorded whole and read."""
    ctx, conn = estate
    task = tasks.admit(ctx, str(uuid4()), title="Dense reason")
    target = _open(ctx, "Release")
    shape = {"task_id": task.task_id, "from_workstream_id": task.task.workstream_id,
             "to_workstream_id": target, "actor": ctx.caller.alias, "reason": ""}
    room = 16_384 - len(json.dumps(shape, ensure_ascii=False).encode("utf-8"))
    exact = "\x01" * (room // 6) + "x" * (room % 6)
    for reason in ("\x1b" * 4_000, exact + "x"):
        assert len(reason.encode("utf-8")) <= 4_096
        with pytest.raises(tasks.TaskQueryError, match="over the plane's 16384"):
            _move(ctx, task.task_id, target, reason=reason)
    assert _moves(conn) == []

    request = str(uuid4())
    assert _move(ctx, task.task_id, target, request, reason=exact).task.workstream_id == target
    assert tuple(conn.execute("SELECT detail_truncated, length(CAST(detail AS BLOB)) FROM events"
                              " WHERE source_ref=?", (f"request:{request}",)).fetchone()) == (0, 16_384)
    assert _task(ctx, conn, task.task_id).workstream_id == target
