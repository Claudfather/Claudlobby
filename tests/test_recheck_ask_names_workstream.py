"""The recheck's ask about an overdue task names the task's workstream (#1053, #2871).

Each of the recheck's asks is a message about one task, as a nudge's ask is, so `_prepare`
names the task's workstream in the message's own column. It reads the workstream through the
task reducer when the recheck runs, so the ask about a moved task names the workstream the
task moved to. A task in no workstream (admitted before every task had one, until the
backfill files it) names none. The recheck takes no task lock, so each ask names the
workstream as of that read: `tests/test_stamp_under_task_lock.py` lists it as the one such
exception.
"""
from dataclasses import replace
from uuid import uuid4

from claudlobby import task_operations as tasks
from claudlobby.plane.identity import resolve_party
from claudlobby.task_recheck import recheck
from tests.test_task_operations import _manager_route
from tests.test_task_operations import estate  # noqa: F401 — private real-Plane fixture
from tests.test_workstream_backfill import _legacy, _open


def _timer(ctx, conn):
    """The recheck's caller as the fleet's timer runs it: the local operator."""
    alias = "human:operator"
    actor = tasks.TaskActor(resolve_party(conn, alias, now="2026-09-28T00:00:00Z"), alias)
    return replace(ctx, caller=actor, caller_fleet_uid=None)


def test_the_recheck_ask_names_each_task_workstream_and_a_task_in_none_names_none(estate):
    ctx, conn = estate
    own = tasks.admit(ctx, str(uuid4()), title="Ship the release")
    moved = tasks.admit(ctx, str(uuid4()), title="Review the plan")
    planning = _open(ctx, "Planning")
    tasks.move(ctx, str(uuid4()), moved.task_id, workstream_id=planning, reason="Part of the planning")
    legacy = _legacy(ctx)
    assert own.task.workstream_id.startswith("ws_") and planning.startswith("ws-")

    timer = _timer(ctx, conn)
    request_id = str(uuid4())
    result = recheck(timer, request_id, route=_manager_route(timer), max_age_h=0, repeat_h=0)

    assert sorted(result.task_ids) == sorted([own.task_id, moved.task_id, legacy])
    asks = conn.execute("SELECT work_item_id, message_class, workstream_id FROM communications"
                        " WHERE source_ref LIKE ?", (f"task-recheck:%:{request_id}",)).fetchall()
    assert len(asks) == 3
    assert {task_id: (kind, workstream) for task_id, kind, workstream in asks} == {
        own.task_id: ("task_request", own.task.workstream_id),
        moved.task_id: ("task_request", planning),
        legacy: ("task_request", None),
    }
