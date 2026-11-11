"""Task rows and messages about one task record the task's workstream (#1053).

Every task verb's row names the task's workstream at the moment of the act, in the row's
`detail`: accept, each report (progress, block, return, complete, fail), withdraw, escalate,
reschedule, nudge and reassign. After a move, a row names the workstream the task moved to. A
close's withdraw rows name the workstream it closes. A task in no workstream (admitted before
#1053, until the backfill files it) names none. A communication about one task names it in its
own column: a report, a nudge's ask and a comment, each naming the new one after a move. The
field is metadata, registered as such, so a metadata capture keeps it, and the task kind's own
workstream column stays empty.
"""
from dataclasses import replace
import json
from uuid import uuid4

import pydantic
import pytest

from claudlobby import task_operations as tasks
from claudlobby import workstream_operations as ws
from claudlobby.message_payload import MessageBody
from claudlobby.plane.contracts import TaskEvent
from claudlobby.plane.registries import CONTENT_FIELDS, FIELD_POLICY
from claudlobby.report_payload import ReportPayload
from tests.test_task_operations import _feedback_human, _manager_route
from tests.test_task_operations import estate  # noqa: F401 — private real-Plane fixture
from tests.test_workstream_backfill import _legacy, _open


def _named(conn, result):
    """What one operation recorded: each task row's event and the workstream its detail names,
    then each communication's class and its workstream column. A report's two facts carry
    `report:<message>` as their source; every other operation's carry `request:<request>`."""
    refs = (f"request:{result.request_id}", f"report:{result.message_id}")
    rows = conn.execute("SELECT event, detail, workstream_id FROM events WHERE kind='task'"
                        " AND source_ref IN (?, ?) ORDER BY ingest_seq", refs).fetchall()
    assert all(column is None for *_, column in rows), "the task kind's workstream column stays empty"
    messages = conn.execute("SELECT message_class, workstream_id FROM communications WHERE msg_id=?",
                            (result.message_id,)).fetchall()
    return ([(event, json.loads(detail).get("workstream_id") if detail else None)
             for event, detail, _ in rows], [tuple(message) for message in messages])


def test_each_task_verb_row_names_the_task_workstream_and_the_new_one_after_a_move(estate):
    ctx, conn = estate
    worker, other = replace(ctx, caller=ctx.bots["worker"]), replace(ctx, caller=ctx.bots["other"])
    human = _feedback_human(ctx, conn)
    task = tasks.admit(ctx, str(uuid4()), title="Ship the release")
    first = task.task.workstream_id
    routed = tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="worker")
    aid = routed.assignment_id

    assert _named(conn, tasks.accept(worker, str(uuid4()), aid)) == ([("accepted", first)], [])
    assert _named(conn, tasks.progress(worker, str(uuid4()), aid, ReportPayload(
        "progress", summary="Half done"))) == ([("progress", first)], [("report", first)])
    assert _named(conn, tasks.escalate(ctx, str(uuid4()), task.task_id, question="Which branch?")
                  ) == ([("escalated", first)], [])
    assert _named(conn, tasks.reschedule(ctx, str(uuid4()), aid, expected_by="2026-12-01T00:00:00Z",
                                         reason="Waits on a review")) == ([("deadline_changed", first)], [])
    assert _named(conn, tasks.nudge(ctx, str(uuid4()), task.task_id, reason="Please look",
                                    route=_manager_route(ctx))) == ([("nudged", first)], [("task_request", first)])
    assert _named(conn, tasks.feedback(human, str(uuid4()), task.task_id, body=MessageBody("Looks right"),
                                       expected_assignment_id=aid, route=_manager_route(human))
                  ) == ([], [("chat", first)])

    second = _open(ctx, "Release")
    tasks.move(ctx, str(uuid4()), task.task_id, workstream_id=second, reason="Part of the release")
    reassigned = tasks.reassign(ctx, str(uuid4()), task.task_id, bot_id="other", reason="Specialist")
    assert _named(conn, reassigned) == ([("reassigned", second)], [])
    aid = reassigned.assignment_id
    assert _named(conn, tasks.accept(other, str(uuid4()), aid)) == ([("accepted", second)], [])
    assert _named(conn, tasks.block(other, str(uuid4()), aid, ReportPayload(
        "blocked", reason="Needs a key"))) == ([("blocked_waiting", second)], [("report", second)])
    assert _named(conn, tasks.return_assignment(other, str(uuid4()), aid, ReportPayload(
        "blocked", reason="Cannot hold it"))) == ([("returned_blocked", second)], [("report", second)])
    aid = tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="worker").assignment_id
    assert _named(conn, tasks.accept(worker, str(uuid4()), aid)) == ([("accepted", second)], [])
    assert _named(conn, tasks.complete(worker, str(uuid4()), aid, ReportPayload(
        "completed", summary="Shipped"))) == ([("completed", second)], [("report", second)])

    failing = tasks.admit(ctx, str(uuid4()), title="Try the migration")
    aid = tasks.assign(ctx, str(uuid4()), failing.task_id, bot_id="worker").assignment_id
    tasks.accept(worker, str(uuid4()), aid)
    assert _named(conn, tasks.fail(worker, str(uuid4()), aid, ReportPayload("failed", reason="It broke"))
                  ) == ([("failed", failing.task.workstream_id)], [("report", failing.task.workstream_id)])
    dropped = tasks.admit(ctx, str(uuid4()), title="Not needed")
    assert _named(conn, tasks.withdraw(ctx, str(uuid4()), dropped.task_id, reason="Not needed")
                  ) == ([("cancelled", dropped.task.workstream_id)], [])


def test_a_nudge_ask_names_the_workstream_the_task_moved_to(estate):
    ctx, conn = estate
    worker = replace(ctx, caller=ctx.bots["worker"])
    task = tasks.admit(ctx, str(uuid4()), title="Review the plan")
    aid = tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="worker").assignment_id
    tasks.accept(worker, str(uuid4()), aid)
    second = _open(ctx, "Planning")
    tasks.move(ctx, str(uuid4()), task.task_id, workstream_id=second, reason="Part of the planning")
    assert second != task.task.workstream_id
    assert _named(conn, tasks.nudge(ctx, str(uuid4()), task.task_id, reason="Please look",
                                    route=_manager_route(ctx))) == ([("nudged", second)], [("task_request", second)])


def test_a_comment_names_the_workstream_the_task_moved_to(estate):
    ctx, conn = estate
    worker = replace(ctx, caller=ctx.bots["worker"])
    human = _feedback_human(ctx, conn)
    task = tasks.admit(ctx, str(uuid4()), title="Draft the notes")
    aid = tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="worker").assignment_id
    tasks.accept(worker, str(uuid4()), aid)
    second = _open(ctx, "Notes")
    tasks.move(ctx, str(uuid4()), task.task_id, workstream_id=second, reason="Belongs with the notes")
    assert second != task.task.workstream_id
    assert _named(conn, tasks.feedback(human, str(uuid4()), task.task_id, body=MessageBody("Reads well"),
                                       expected_assignment_id=aid, route=_manager_route(human))
                  ) == ([], [("chat", second)])


def test_a_task_in_no_workstream_names_none(estate):
    """A task admitted before every task had a workstream, until the backfill files it."""
    ctx, conn = estate
    worker = replace(ctx, caller=ctx.bots["worker"])
    task_id = _legacy(ctx)
    aid = tasks.assign(ctx, str(uuid4()), task_id, bot_id="worker").assignment_id
    assert _named(conn, tasks.accept(worker, str(uuid4()), aid)) == ([("accepted", None)], [])
    assert _named(conn, tasks.progress(worker, str(uuid4()), aid, ReportPayload(
        "progress", summary="Still going"))) == ([("progress", None)], [("report", None)])
    detail = conn.execute("SELECT detail FROM events WHERE kind='task' AND event='progress'").fetchone()[0]
    assert "workstream_id" not in json.loads(detail)


def test_a_close_withdraw_row_names_the_workstream_it_closes(estate):
    ctx, conn = estate
    wid = _open(ctx, "Spring cleaning")
    queued = tasks.admit(ctx, str(uuid4()), title="Never started", workstream_id=wid)
    request = str(uuid4())
    ws.apply(ctx, "close", {"id": wid, "status": "abandoned"}, request_id=request)
    rows = conn.execute("SELECT event, work_item_id, detail, workstream_id FROM events"
                        " WHERE kind='task' AND source_ref=?", (f"request:{request}",)).fetchall()
    assert [(event, task_id, json.loads(detail)["workstream_id"], column)
            for event, task_id, detail, column in rows] == [("cancelled", queued.task_id, wid, None)]


def test_a_metadata_capture_keeps_the_workstream_and_withholds_the_prose(estate):
    ctx, conn = estate
    capture = ctx.root / "state" / "plane" / "capture.json"
    capture.parent.mkdir(parents=True, exist_ok=True)
    capture.write_text('{"*": "metadata"}')
    worker = replace(ctx, caller=ctx.bots["worker"])
    task = tasks.admit(ctx, str(uuid4()), title="Quiet work")
    aid = tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="worker").assignment_id
    tasks.accept(worker, str(uuid4()), aid)
    reported = tasks.progress(worker, str(uuid4()), aid, ReportPayload("progress", summary="Private prose"))
    assert _named(conn, reported) == ([("progress", task.task.workstream_id)],
                                      [("report", task.task.workstream_id)])
    detail = json.loads(conn.execute("SELECT detail FROM events WHERE kind='task' AND source_ref=?",
                                      (f"report:{reported.message_id}",)).fetchone()[0])
    assert "summary" not in detail, "positive control: the capture withholds the report's prose"


def test_the_field_is_registered_metadata_and_rides_detail():
    assert FIELD_POLICY[("task", "workstream_id")] == {"class": "METADATA"}
    assert "workstream_id" not in CONTENT_FIELDS["task"]
    wid = "ws_" + "a" * 32
    event = TaskEvent(work_item_id="wi_" + "b" * 32, event="accepted", workstream_id=wid)
    assert event.workstream_id == wid
    with pytest.raises(pydantic.ValidationError):
        TaskEvent(work_item_id="wi_" + "b" * 32, event="accepted", workstream_id="")
