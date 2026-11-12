"""A task verb frozen but not recorded, retried after a move, refuses and writes nothing (#2871).

A task verb freezes its rows in its request (`_prepare`, which names the task's workstream on
each) before it writes them (`_commit`). If its first attempt ends after freezing and before any
row is written, and the task then moves, a retry with the same request ID rebuilds rows that name
the new workstream. The request store compares them with the frozen rows and refuses, "request
UUID already has different semantics or identities", before any write: the limit #2870's
description names. Nothing is lost by it: the frozen rows were proved absent first, so the act
can be recorded under a new request ID, and it then names the new workstream. Without the move,
the same retry records the frozen rows.

The first attempt ends in each way it can before a row exists: the process stops between
freezing and writing, or the write fails. The verbs are escalate (one task row) and a progress
report (a task row and a message about the task).
"""
from dataclasses import replace
from uuid import uuid4

import pytest

from claudlobby import task_operations as tasks
from claudlobby.plane import emit_api
from claudlobby.report_payload import ReportPayload
from claudlobby.request_receipts import ReceiptConflict, RequestStore
from tests.test_rows_name_workstream import _named
from tests.test_task_operations import _counts
from tests.test_task_operations import estate  # noqa: F401 — private real-Plane fixture
from tests.test_workstream_backfill import _open

REFUSAL = "request UUID already has different semantics or identities"


class _Stopped(BaseException):
    """The process stopping between freezing a request's rows and writing them."""


def _escalate(ctx, task):
    return lambda request_id: tasks.escalate(ctx, request_id, task.task_id, question="Which branch?")


def _progress(ctx, task):
    worker = replace(ctx, caller=ctx.bots["worker"])
    aid = tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="worker").assignment_id
    tasks.accept(worker, str(uuid4()), aid)
    return lambda request_id: tasks.progress(worker, request_id, aid,
                                             ReportPayload("progress", summary="Half done"))


#: verb: (set it up on a task and return the act, the task row it records, whether it reports)
VERBS = {"escalate": (_escalate, "escalated", False), "progress": (_progress, "progress", True)}


def _stop_before_the_write(patch):
    def stopped(*args, **kwargs):
        raise _Stopped()
    patch.setattr(tasks, "_commit", stopped)
    return _Stopped


def _fail_the_write(patch):
    def failed(*args, **kwargs):
        raise OSError("the write failed before any row")
    patch.setattr(emit_api, "emit_batch", failed)
    return tasks.TaskRecordingError


ENDINGS = {"stopped": _stop_before_the_write, "write_failed": _fail_the_write}


def _first_attempt(ctx, conn, monkeypatch, verb, ending):
    """A task, its act, and the request ID of a first attempt that froze its rows and wrote none."""
    task = tasks.admit(ctx, str(uuid4()), title="Ship the release")
    act = VERBS[verb][0](ctx, task)
    request_id = str(uuid4())
    before = _counts(conn)
    with monkeypatch.context() as patch:
        with pytest.raises(ENDINGS[ending](patch)):
            act(request_id)
    assert _counts(conn) == before, "the first attempt wrote no row"
    return task, act, request_id


def _recorded(verb, workstream_id):
    _setup, event, reports = VERBS[verb]
    return [(event, workstream_id)], [("report", workstream_id)] if reports else []


@pytest.mark.parametrize("ending", ENDINGS)
@pytest.mark.parametrize("verb", VERBS)
def test_a_retry_after_a_move_refuses_in_the_request_store_and_writes_nothing(estate, monkeypatch, verb, ending):
    ctx, conn = estate
    task, act, request_id = _first_attempt(ctx, conn, monkeypatch, verb, ending)
    release = _open(ctx, "Release")
    tasks.move(ctx, str(uuid4()), task.task_id, workstream_id=release, reason="Part of the release")
    assert release != task.task.workstream_id

    refused, prepare = [], RequestStore.prepare

    def watched(store, intent):
        try:
            return prepare(store, intent)
        except ReceiptConflict as exc:
            refused.append(str(exc))
            raise

    monkeypatch.setattr(RequestStore, "prepare", watched)
    before = _counts(conn)
    with pytest.raises(ReceiptConflict, match=REFUSAL):
        act(request_id)
    assert refused == [REFUSAL], "the request store refused the rebuilt rows"
    assert _counts(conn) == before, "the refused retry wrote nothing"

    again = act(str(uuid4()))
    assert _named(conn, again) == _recorded(verb, release)


@pytest.mark.parametrize("ending", ENDINGS)
@pytest.mark.parametrize("verb", VERBS)
def test_without_a_move_the_same_retry_records_the_frozen_rows(estate, monkeypatch, verb, ending):
    ctx, conn = estate
    task, act, request_id = _first_attempt(ctx, conn, monkeypatch, verb, ending)
    retried = act(request_id)
    assert retried.request_id == request_id and retried.replayed is False
    assert _named(conn, retried) == _recorded(verb, task.task.workstream_id)
