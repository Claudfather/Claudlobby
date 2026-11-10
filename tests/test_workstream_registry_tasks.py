"""The workstream registry reads its tasks (#1053).

A workstream lists the tasks that name it, in admission order, with the open ones apart. One that
holds a task and no open one reads closed, by the task reducer's own terminal facts, the ones
admit's join check reads: `done` if any of its tasks completed, else `abandoned`, dated by the row
that ended its last open task and closed by `task:<ID>`. A recorded close wins. A workstream that
holds an open task is never stalled, and its lease never expires; one that holds no task keeps its
lease. The brief keeps the oldest WORKSTREAM_ROW_LIMIT rows of each workstream list and labels a
cut.
"""
from dataclasses import replace
from datetime import datetime, timezone
import time
from uuid import uuid4

import pytest

from claudlobby import brief as brief_mod
from claudlobby import task_operations as tasks
from claudlobby.brief import _workstream_section, build_brief, format_brief, plane_session
from claudlobby.report_payload import ReportPayload
from claudlobby.task_state import read_tasks
from claudlobby.workstream_operations import WorkstreamError, _reader, apply as workstream_apply
from claudlobby.workstreams import format_show
from tests.test_task_operations import estate  # noqa: F401 — private real-Plane fixture


def _open(ctx, title="Release review"):
    return workstream_apply(ctx, "open", {"title": title, "id": None, "project": None,
                                          "owner": None, "next": None},
                            request_id=str(uuid4())).workstream_ids[0]


def _registry(ctx):
    return _reader(ctx, 14, or_empty=False)["workstreams"]


def _assigned(ctx, task_id):
    assigned = tasks.assign(ctx, str(uuid4()), task_id, bot_id="worker")
    worker = replace(ctx, caller=ctx.bots["worker"])
    tasks.accept(worker, str(uuid4()), assigned.assignment_id)
    return worker, assigned.assignment_id


def _complete(ctx, task_id):
    worker, assignment_id = _assigned(ctx, task_id)
    tasks.complete(worker, str(uuid4()), assignment_id, ReportPayload("completed", summary="Done"))


def _ended_at(ctx, conn, task_id):
    """The task's terminal row's instant, as the registry writes instants."""
    task, = read_tasks(conn, fleet_uid=ctx.fleet_uid, task_ids=[task_id]).tasks
    at = datetime.fromisoformat(task.terminal_event.occurred_at.replace("Z", "+00:00"))
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _section(ctx, now):
    plane, note = plane_session(ctx.context.paths, ctx.context.fleet.name)
    assert note is None
    degraded = []
    with plane:
        return _workstream_section(ctx.context.fleet, ctx.context.paths, now, degraded,
                                   plane=plane), degraded


def test_a_workstream_lists_its_tasks_in_admission_order_with_the_open_ones(estate):
    ctx, _conn = estate
    wid = _open(ctx)
    first = tasks.admit(ctx, str(uuid4()), title="First", workstream_id=wid)
    second = tasks.admit(ctx, str(uuid4()), title="Second", workstream_id=wid)
    own = tasks.admit(ctx, str(uuid4()), title="On its own")
    registry = _registry(ctx)
    assert registry[wid]["task_ids"] == registry[wid]["open_task_ids"] == [first.task_id, second.task_id]
    assert registry[wid]["status"] == "active"
    mine = registry[own.task.workstream_id]
    assert mine["task_ids"] == mine["open_task_ids"] == [own.task_id]
    tasks.withdraw(ctx, str(uuid4()), first.task_id, reason="Not needed")
    entry = _registry(ctx)[wid]
    assert entry["task_ids"] == [first.task_id, second.task_id]
    assert entry["open_task_ids"] == [second.task_id] and entry["status"] == "active"
    # A workstream that holds no task lists none.
    empty = _open(ctx, "Holds none")
    assert _registry(ctx)[empty]["task_ids"] == _registry(ctx)[empty]["open_task_ids"] == []


def test_one_ended_task_and_one_open_one_keep_the_workstream_open_to_admit_and_the_registry(estate):
    """Note A of #2850's review: every derived-close test had one task, so `all` -> `any` in
    admit's join check still passed them. With one task ended and one open, admit still joins,
    and the registry reads the workstream open."""
    ctx, _conn = estate
    wid = _open(ctx)
    ended = tasks.admit(ctx, str(uuid4()), title="Ended", workstream_id=wid)
    still = tasks.admit(ctx, str(uuid4()), title="Still open", workstream_id=wid)
    _complete(ctx, ended.task_id)
    assert _registry(ctx)[wid]["status"] == "active"
    joined = tasks.admit(ctx, str(uuid4()), title="Follow-up", workstream_id=wid)
    assert joined.task.workstream_id == wid
    entry = _registry(ctx)[wid]
    assert entry["status"] == "active" and "closed_ts" not in entry and "closed_by" not in entry
    assert entry["open_task_ids"] == [still.task_id, joined.task_id]
    # A task's own workstream, the same way: its first task withdrawn, its follow-up open.
    own = tasks.admit(ctx, str(uuid4()), title="Own")
    follow = tasks.admit(ctx, str(uuid4()), title="Its follow-up", workstream_id=own.task.workstream_id)
    tasks.withdraw(ctx, str(uuid4()), own.task_id, reason="Superseded by its follow-up")
    assert _registry(ctx)[own.task.workstream_id]["open_task_ids"] == [follow.task_id]
    assert tasks.admit(ctx, str(uuid4()), title="Another", workstream_id=own.task.workstream_id).task_id


def test_it_reads_done_from_the_row_that_ended_its_last_open_task(estate):
    ctx, conn = estate
    wid = _open(ctx)
    done = tasks.admit(ctx, str(uuid4()), title="Done", workstream_id=wid)
    last = tasks.admit(ctx, str(uuid4()), title="Withdrawn last", workstream_id=wid)
    _complete(ctx, done.task_id)
    tasks.withdraw(ctx, str(uuid4()), last.task_id, reason="Not needed")
    entry = _registry(ctx)[wid]
    assert entry["status"] == "done"  # one of its tasks completed
    assert entry["closed_by"] == f"task:{last.task_id}"
    assert entry["closed_ts"] == _ended_at(ctx, conn, last.task_id)
    assert entry["open_task_ids"] == [] and entry["task_ids"] == [done.task_id, last.task_id]
    # Its newest row is its progress.
    assert entry["last_progress_ts"] >= entry["closed_ts"]
    # Closed: admit refuses a join, and the manager's verbs refuse it as terminal.
    with pytest.raises(tasks.WorkstreamClosedError, match="every task in it has ended"):
        tasks.admit(ctx, str(uuid4()), title="Too late", workstream_id=wid)
    with pytest.raises(WorkstreamError, match="terminal workstream"):
        workstream_apply(ctx, "progress", {"id": wid, "next": None}, request_id=str(uuid4()))


def test_one_whose_tasks_all_ended_uncompleted_reads_abandoned(estate):
    ctx, conn = estate
    withdrawn = tasks.admit(ctx, str(uuid4()), title="Never needed")
    tasks.withdraw(ctx, str(uuid4()), withdrawn.task_id, reason="Not needed")
    entry = _registry(ctx)[withdrawn.task.workstream_id]
    assert (entry["status"], entry["closed_by"]) == ("abandoned", f"task:{withdrawn.task_id}")
    failed = tasks.admit(ctx, str(uuid4()), title="Could not be done")
    worker, assignment_id = _assigned(ctx, failed.task_id)
    tasks.fail(worker, str(uuid4()), assignment_id, ReportPayload("failed", reason="Blocked for good"))
    entry = _registry(ctx)[failed.task.workstream_id]
    assert (entry["status"], entry["closed_by"]) == ("abandoned", f"task:{failed.task_id}")
    assert entry["closed_ts"] == _ended_at(ctx, conn, failed.task_id)


def test_a_recorded_close_wins_over_the_derived_one(estate):
    ctx, _conn = estate
    wid = _open(ctx)
    task = tasks.admit(ctx, str(uuid4()), title="Finished after the close", workstream_id=wid)
    workstream_apply(ctx, "close", {"id": wid, "status": "abandoned"}, request_id=str(uuid4()))
    closed = _registry(ctx)[wid]
    assert (closed["status"], closed["closed_by"]) == ("abandoned", "bot:example/manager")
    _complete(ctx, task.task_id)
    entry = _registry(ctx)[wid]
    assert entry["status"] == "abandoned"  # not done: the manager's close stands
    assert (entry["closed_ts"], entry["closed_by"]) == (closed["closed_ts"], closed["closed_by"])
    assert entry["task_ids"] == [task.task_id] and entry["open_task_ids"] == []


def test_an_open_task_keeps_its_workstream_from_stalling_or_expiring(estate):
    """A quiet open task past the lease leaves its workstream neither stalled nor expired, and
    the brief names the task; a workstream that holds no task keeps its lease, and expires."""
    ctx, _conn = estate
    own = tasks.admit(ctx, str(uuid4()), title="Quiet for a month")
    empty = _open(ctx, "Holds no task")
    section, degraded = _section(ctx, int(time.time()) + 30 * 86400)  # past both leases
    flags = {e["id"]: (e["stalled"], e["lease_expired"], e["open_task_ids"]) for e in section["active"]}
    assert flags == {own.task.workstream_id: (False, False, [own.task_id]), empty: (True, True, [])}
    assert [e["id"] for e in section["stalled"]] == [empty] and degraded == []


def test_the_brief_task_snapshot_and_the_registry_own_read_agree(estate):
    """The brief hands the registry the fleet's tasks it already read; any other caller lets the
    registry read its own. Both give the same registry."""
    ctx, _conn = estate
    wid = _open(ctx)
    first = tasks.admit(ctx, str(uuid4()), title="A", workstream_id=wid)
    tasks.admit(ctx, str(uuid4()), title="B", workstream_id=wid)
    own = tasks.admit(ctx, str(uuid4()), title="Own")
    tasks.withdraw(ctx, str(uuid4()), own.task_id, reason="Not needed")
    _complete(ctx, first.task_id)
    plane, note = plane_session(ctx.context.paths, ctx.context.fleet.name)
    assert note is None
    with plane:
        plane.conn.execute("BEGIN")
        snapshot = read_tasks(plane.conn, fleet_uid=ctx.fleet_uid)
        shared = plane.pr.workstream_registry(plane.conn, plane.fleet, tasks=snapshot.tasks)
        alone = plane.pr.workstream_registry(plane.conn, plane.fleet)
    assert shared == alone
    assert alone["workstreams"][own.task.workstream_id]["status"] == "abandoned"
    assert alone["workstreams"][wid]["open_task_ids"] != []


def test_the_brief_keeps_the_oldest_active_workstreams_and_labels_the_cut(estate):
    """More tasks than the cap, each with its own workstream: the JSON keeps the oldest rows, the
    cut is labeled with the whole count, and the text view counts against it."""
    ctx, _conn = estate
    limit = brief_mod.WORKSTREAM_ROW_LIMIT
    admitted = [tasks.admit(ctx, str(uuid4()), title=f"Task {n}") for n in range(limit + 1)]
    brief = build_brief(ctx.context.fleet, ctx.context.paths, "manager", int(time.time()))
    assert [e["id"] for e in brief["workstreams"]["active"]] == [
        task.task.workstream_id for task in admitted[:limit]]
    cut, = [d for d in brief["degraded"] if d["field"] == "workstreams.active"]
    assert (cut["mode"], cut["count"], cut["issue"]) == ("labeled", limit + 1, "#1053")
    assert brief_mod.WORKSTREAMS_LIST_COMMAND in cut["reason"]
    text = format_brief(brief)
    assert f"showing the oldest 10 of {limit + 1}" in text
    assert brief_mod.WORKSTREAMS_LIST_COMMAND in text


def test_each_workstream_list_is_cut_and_labeled_on_its_own(estate, monkeypatch):
    ctx, _conn = estate
    monkeypatch.setattr(brief_mod, "WORKSTREAM_ROW_LIMIT", 2)
    opened = [_open(ctx, f"Plan {n}") for n in range(6)]
    for wid in opened[3:]:
        workstream_apply(ctx, "block", {"id": wid, "on": "human:reviewer", "note": "Waiting"},
                         request_id=str(uuid4()))
    section, degraded = _section(ctx, int(time.time()) + 30 * 86400)  # the active three are stalled
    assert [e["id"] for e in section["active"]] == [e["id"] for e in section["stalled"]] == opened[:2]
    assert [e["id"] for e in section["blocked"]] == opened[3:5]
    assert sorted((d.field, d.mode, d.count) for d in degraded) == [
        ("workstreams.active", "labeled", 3), ("workstreams.blocked", "labeled", 3),
        ("workstreams.stalled", "labeled", 3)]


def test_workstream_show_prints_its_open_tasks_and_its_close(estate):
    ctx, _conn = estate
    own = tasks.admit(ctx, str(uuid4()), title="Shown")
    wid = own.task.workstream_id
    text = format_show(_registry(ctx)[wid])
    assert f"tasks:    {own.task_id}" in text and f"open:     {own.task_id}" in text
    assert "closed:" not in text
    tasks.withdraw(ctx, str(uuid4()), own.task_id, reason="Not needed")
    entry = _registry(ctx)[wid]
    text = format_show(entry)
    assert "open:     none" in text
    assert f"closed:   {entry['closed_ts']} by task:{own.task_id}" in text


def test_prune_archives_a_workstream_its_tasks_closed(estate):
    ctx, _conn = estate
    own = tasks.admit(ctx, str(uuid4()), title="Done and pruned")
    kept = tasks.admit(ctx, str(uuid4()), title="Still open")
    tasks.withdraw(ctx, str(uuid4()), own.task_id, reason="Not needed")
    pruned = workstream_apply(ctx, "prune", {}, request_id=str(uuid4()))
    assert pruned.workstream_ids == (own.task.workstream_id,)
    registry = _registry(ctx)
    assert own.task.workstream_id not in registry and kept.task.workstream_id in registry
