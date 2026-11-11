"""An assignment delivery names its task's workstream, and an unlinked report names none (#1053).

A delivery's communication names the task's workstream in its own column. A retry rebuilds that
fact and must match the one first recorded, which named the workstream at that time. So a retry
matches after the task moved, and matches a record an earlier release made with no workstream.
The workstream is the only field the retry's check relaxes: a retry that differs in any other
field still refuses, and so does one whose first record names a workstream the task was never
in. Neither refusal sends anything.
"""
from dataclasses import replace
from uuid import uuid4

import pytest

from claudlobby import assignment_delivery as deliveries
from claudlobby import message_operations as messages
from claudlobby import task_operations as tasks
from claudlobby.message_payload import MessageBody
from claudlobby.message_transport import TransportOutcome
from claudlobby.report_payload import ReportPayload
from claudlobby.request_receipts import ReceiptConflict
from tests.test_message_operations import _manager_delivery, _receipt
from tests.test_message_operations import estate  # noqa: F401 — private real-Plane fixture
from tests.test_workstream_backfill import _open


def _column(conn, message_id):
    return conn.execute("SELECT workstream_id FROM communications WHERE msg_id=?",
                        (message_id,)).fetchone()[0]


def _uncertain_then_sent(calls):
    """The first native send ends unknown; a retry's send is submitted."""
    def transport(*args, **kwargs):
        calls.append(kwargs["body"])
        return (TransportOutcome("unknown") if len(calls) == 1
                else TransportOutcome("submitted", native_returncode=0))
    return transport


def _never_sends(*args, **kwargs):
    pytest.fail("a refused retry must not send")


def _first_record_names(monkeypatch, workstream_id):
    """Record the first delivery as naming *workstream_id*: as an earlier release did (None), or
    a workstream the task was never in."""
    monkeypatch.setattr(deliveries, "_recorded_workstreams", lambda *args: iter([workstream_id]))


def test_a_delivery_names_the_task_workstream_and_a_retry_after_a_move_matches_it(estate):
    ctx, route, package, conn, task, assignment = _manager_delivery(estate)
    first_ws = task.task.workstream_id
    request_id, body, calls = str(uuid4()), MessageBody("Private delivery"), []
    first = deliveries.deliver(ctx, route, package, request_id, assignment.assignment_id, body,
                               transport=_uncertain_then_sent(calls))
    assert first.delivery == "unknown" and _column(conn, first.message_id) == first_ws
    original = _receipt(route, request_id)

    target = _open(ctx, "Elsewhere")
    tasks.move(ctx, str(uuid4()), task.task_id, workstream_id=target, reason="Belongs there")
    retried = deliveries.deliver(ctx, route, package, request_id, assignment.assignment_id, body,
                                 retry_uncertain=True, transport=_uncertain_then_sent(calls))
    assert retried.replayed and retried.delivery == "submitted" and len(calls) == 2
    assert _receipt(route, request_id).intent == original.intent
    assert _column(conn, first.message_id) == first_ws, "the record keeps the workstream it was delivered in"

    later = deliveries.deliver(ctx, route, package, str(uuid4()), assignment.assignment_id,
                               MessageBody("Second private delivery"),
                               transport=lambda *a, **k: TransportOutcome("submitted", native_returncode=0))
    assert _column(conn, later.message_id) == target, "a new delivery names the task's workstream now"


def test_a_delivery_retry_of_a_record_with_no_workstream_matches(estate, monkeypatch):
    ctx, route, package, conn, task, assignment = _manager_delivery(estate)
    request_id, body, calls = str(uuid4()), MessageBody("Private delivery"), []
    with monkeypatch.context() as patch:
        _first_record_names(patch, None)
        first = deliveries.deliver(ctx, route, package, request_id, assignment.assignment_id, body,
                                   transport=_uncertain_then_sent(calls))
    assert first.delivery == "unknown" and _column(conn, first.message_id) is None
    retried = deliveries.deliver(ctx, route, package, request_id, assignment.assignment_id, body,
                                 retry_uncertain=True, transport=_uncertain_then_sent(calls))
    assert retried.replayed and retried.delivery == "submitted" and len(calls) == 2


@pytest.mark.parametrize("field, value", [("command_type", "query"), ("recipient_raw", "another-pane")])
def test_a_delivery_retry_that_differs_in_another_field_still_refuses(estate, monkeypatch, field, value):
    ctx, route, package, conn, task, assignment = _manager_delivery(estate)
    request_id, body, calls = str(uuid4()), MessageBody("Private delivery"), []
    first = deliveries.deliver(ctx, route, package, request_id, assignment.assignment_id, body,
                               transport=_uncertain_then_sent(calls))
    assert first.delivery == "unknown"
    real = deliveries._communication

    def changed(*args, **kwargs):
        raw = real(*args, **kwargs)
        raw["payload"][field] = value
        return raw

    monkeypatch.setattr(deliveries, "_communication", changed)
    with pytest.raises(ReceiptConflict, match="differs from frozen request"):
        deliveries.deliver(ctx, route, package, request_id, assignment.assignment_id, body,
                           retry_uncertain=True, transport=_never_sends)
    assert len(calls) == 1


def test_a_delivery_retry_naming_a_workstream_the_task_was_never_in_refuses(estate, monkeypatch):
    ctx, route, package, conn, task, assignment = _manager_delivery(estate)
    never, target = _open(ctx, "Never this task's"), _open(ctx, "Elsewhere")
    request_id, body, calls = str(uuid4()), MessageBody("Private delivery"), []
    with monkeypatch.context() as patch:
        _first_record_names(patch, never)
        deliveries.deliver(ctx, route, package, request_id, assignment.assignment_id, body,
                           transport=_uncertain_then_sent(calls))
    tasks.move(ctx, str(uuid4()), task.task_id, workstream_id=target, reason="Belongs there")
    with pytest.raises(ReceiptConflict, match="differs from frozen request"):
        deliveries.deliver(ctx, route, package, request_id, assignment.assignment_id, body,
                           retry_uncertain=True, transport=_never_sends)
    assert len(calls) == 1


def test_an_unlinked_report_names_no_workstream(estate):
    route, package, conn = estate
    route = replace(route, peer=route.manager, peer_destination=route.manager_destination)
    sent = messages.send_unlinked_report(
        route, package, ReportPayload("progress", summary="Not tied to a task"),
        request_id=str(uuid4()), trusted_tiers={},
        transport=lambda *a, **k: TransportOutcome("submitted", native_returncode=0),
        notify=lambda *a, **k: "alerted", clear=lambda *a, **k: None)
    assert conn.execute("SELECT message_class, work_item_id, workstream_id FROM communications"
                        " WHERE msg_id=?", (sent.message_id,)).fetchone() == ("report", None, None)
