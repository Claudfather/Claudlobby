"""Ordinary message effects use a private Plane and injected native carrier."""

from dataclasses import replace
import json
import sqlite3
from uuid import uuid4

import pytest

from claudlobby import message_operations as messages
from claudlobby import assignment_delivery as deliveries
from claudlobby.config import BotConfig, FleetConfig
from claudlobby.context import Context
from claudlobby.message_context import MessageRoute
from claudlobby.message_payload import MessageBody
from claudlobby.message_transport import TransportDestination, TransportOutcome
from claudlobby.paths import Paths
from claudlobby.plane.db import connect, db_file
from claudlobby.plane.identity import resolve, resolve_party
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.ids import mint_msg_id
from claudlobby.request_facts import FactProof
from claudlobby.request_receipts import ReceiptConflict, RequestStore, locked_request
from claudlobby.report_payload import ReportPayload
from claudlobby.task_operations import TaskActor, TaskOperationContext
from claudlobby import task_operations as tasks
from tests.package_fixtures import source_package
from tests.plane_setup import initialize_plane


@pytest.fixture
def estate(tmp_path):
    tmp_path = tmp_path.resolve()
    initialize_plane(tmp_path)
    host_uid = ensure_host_uid(tmp_path / "state")
    conn = connect(db_file(tmp_path), synchronous="FULL")
    fleet_uid = resolve(conn, "fleet", "example", now="2026-09-28T00:00:00Z")
    actors = {name: TaskActor(resolve_party(conn, "bot:example/" + name,
                                now="2026-09-28T00:00:00Z", fleet_uid=fleet_uid),
                              "bot:example/" + name)
              for name in ("caller", "worker", "manager")}
    fleet = FleetConfig("example", "example", "manager",
                        bots={name: BotConfig(name, name, []) for name in actors}, projects={})
    package = source_package()
    context = Context(Paths(root=tmp_path, package=package), fleet, {}, bot_id="caller")
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir()
    peer = TransportDestination(tmp_path, "example", "socket-worker", "worker", tmux_dir)
    manager = TransportDestination(tmp_path, "example", "socket-manager", "manager", tmux_dir)
    route = MessageRoute(context, context, context, "activation-1", "plan-1", "release-1",
                         host_uid, fleet_uid, fleet_uid, fleet_uid, actors["caller"],
                         actors["worker"], actors["manager"], peer, manager)
    yield route, package, conn
    conn.close()


def _call(route, package, request_id, *, body="Private SECRET body", kind="chat",
          transport=None, **options):
    return messages.send_message(route, package, MessageBody(body), request_id=request_id,
                                 kind=kind, trusted_tiers={},
                                 transport=transport or (lambda *a, **k: TransportOutcome("submitted",
                                                   "sha256:" + "a" * 64, 99, 0)),
                                 notify=lambda *a, **k: "alerted",
                                 clear=lambda *a, **k: None, **options)


def _counts(conn):
    return (conn.execute("SELECT count(*) FROM communications").fetchone()[0],
            conn.execute("SELECT count(*) FROM events WHERE kind='transmission'").fetchone()[0])


def _receipt(route, request_id):
    with locked_request(route.selected.paths.root, route.selected_fleet_uid, request_id) as store:
        return store.load()


def test_submitted_send_replay_has_one_native_effect_and_exact_facts(estate):
    route, package, conn = estate
    request_id = str(uuid4())
    calls = []
    def transport(*args, **kwargs):
        calls.append(kwargs["body"])
        return TransportOutcome("submitted", "sha256:" + "a" * 64, 99, 0)
    first = _call(route, package, request_id, transport=transport)
    assert (first.delivery, first.recording, first.request_persisted, first.exit_code) == (
        "submitted", "committed", True, 0)
    assert _counts(conn) == (1, 1)
    second = _call(route, package, request_id, transport=transport)
    assert second.replayed and second.message_id == first.message_id
    assert _counts(conn) == (1, 1) and len(calls) == 1
    assert b"SECRET" not in _receipt(route, request_id).intent.semantic_sha256.encode()
    path = route.selected.paths.root / "state/requests" / route.selected_fleet_uid / (request_id + ".json")
    assert b"Private SECRET body" not in path.read_bytes()


def test_recorder_outage_continues_one_disclosed_native_send(estate, monkeypatch):
    route, package, conn = estate
    original = messages.emit_batch
    def outage(root, raw, **kwargs):
        if raw[0]["event_type"] == "communication":
            raise sqlite3.OperationalError("private outage")
        return original(root, raw, **kwargs)
    monkeypatch.setattr(messages, "emit_batch", outage)
    calls = []
    result = _call(route, package, str(uuid4()), transport=lambda *a, **k: (
        calls.append(k["body"]) or TransportOutcome("submitted", native_returncode=0)))
    assert result.exit_code == 11 and result.code == "recording_degraded"
    assert not result.ok and not result.retryable and result.request_persisted
    assert result.delivery == "submitted" and result.recording == "unrecorded"
    assert result.alert == "alerted" and len(calls) == 1
    assert "Recording degraded" in calls[0]


def test_receipt_write_outage_reports_plane_truth_without_second_send(estate, monkeypatch):
    route, package, conn = estate
    request_id = str(uuid4())
    import claudlobby.request_receipts as receipts
    original = receipts.RequestStore._save
    def fail_observation(self, receipt):
        if receipt.message_attempts and receipt.message_attempts[-1].observation is not None:
            raise OSError("private receipt outage")
        return original(self, receipt)
    monkeypatch.setattr(receipts.RequestStore, "_save", fail_observation)
    calls = []
    result = _call(route, package, request_id, transport=lambda *a, **k: (
        calls.append(k["body"]) or TransportOutcome("submitted", native_returncode=0)))
    assert len(calls) == 1 and _counts(conn) == (1, 1)
    assert result.recording == "committed" and not result.request_persisted
    assert result.exit_code == 11 and result.alert == "alerted"
    assert _receipt(route, request_id).message_attempts[0].observation is None
    with pytest.raises(ReceiptConflict, match="recorded submission"):
        _call(route, package, request_id, retry_uncertain=True,
              transport=lambda *a, **k: pytest.fail("unobserved recorded attempt must not resend"))
    assert len(calls) == 1


def test_existing_submitted_send_reconciles_prepared_recording_without_resend(estate, monkeypatch):
    route, package, _ = estate
    request_id = str(uuid4())
    import claudlobby.request_receipts as receipts
    original = receipts.RequestStore.outcome
    def fail_once(self, index, status):
        if index == 0:
            monkeypatch.setattr(receipts.RequestStore, "outcome", original)
            raise OSError("private receipt write outage")
        return original(self, index, status)
    monkeypatch.setattr(receipts.RequestStore, "outcome", fail_once)
    calls = []
    def transport(*a, **k):
        calls.append(1)
        return TransportOutcome("submitted", native_returncode=0)
    first = _call(route, package, request_id, transport=transport)
    assert first.recording == "committed" and not first.request_persisted
    assert _receipt(route, request_id).stages[0].status == "unknown"
    replay = _call(route, package, request_id, transport=transport)
    assert replay.recording == "committed" and replay.request_persisted
    assert _receipt(route, request_id).stages[0].status == "committed"
    assert len(calls) == 1


def test_unavailable_new_receipt_path_degrades_but_existing_path_refuses(estate, monkeypatch):
    route, package, _ = estate
    real_lock = messages.locked_request
    def unavailable(*a, **k):
        raise OSError("private request store outage")
    monkeypatch.setattr(messages, "locked_request", unavailable)
    calls = []
    result = _call(route, package, str(uuid4()), transport=lambda *a, **k: (
        calls.append(k["body"]) or TransportOutcome("submitted", native_returncode=0)))
    assert result.exit_code == 11 and not result.request_persisted
    assert result.recording == "committed" and len(calls) == 1
    assert "Recording degraded" in calls[0]
    monkeypatch.setattr(messages, "locked_request", real_lock)
    existing_id = str(uuid4())
    _call(route, package, existing_id)
    monkeypatch.setattr(messages, "locked_request", unavailable)
    with pytest.raises(ReceiptConflict):
        _call(route, package, existing_id, transport=lambda *a, **k: pytest.fail("resend"))


def test_unknown_result_needs_explicit_retry_and_preserves_first_attempt(estate):
    route, package, _ = estate
    request_id = str(uuid4())
    calls = []
    def transport(*args, **kwargs):
        calls.append(1)
        return TransportOutcome("unknown") if len(calls) == 1 else TransportOutcome("submitted", native_returncode=0)
    first = _call(route, package, request_id, transport=transport)
    assert first.delivery == "unknown" and len(calls) == 1
    replay = _call(route, package, request_id, transport=transport)
    assert replay.delivery == "unknown" and len(calls) == 1
    second = _call(route, package, request_id, transport=transport, retry_uncertain=True)
    assert second.delivery == "submitted" and len(calls) == 2
    saved = _receipt(route, request_id)
    assert len(saved.message_attempts) == 2
    assert saved.message_attempts[0].observation.status == "unknown"
    assert saved.message_attempts[1].observation.status == "submitted"


def test_same_uuid_refuses_changed_route_and_semantics_before_native_effect(estate):
    route, package, _ = estate
    request_id = str(uuid4())
    _call(route, package, request_id)
    def forbidden(*args, **kwargs):
        raise AssertionError("must not send")
    with pytest.raises(ReceiptConflict):
        _call(route, package, request_id, body="Changed private body", transport=forbidden)
    for changed in (replace(route, manager=route.peer),
                    replace(route, peer_destination=replace(route.peer_destination, socket="other-socket"))):
        with pytest.raises(ReceiptConflict):
            _call(changed, package, request_id, transport=forbidden)
    with pytest.raises(messages.MessageConflict):
        _call(replace(route, peer_destination=replace(route.peer_destination, fleet="other")),
              package, request_id, transport=forbidden)


def test_selected_release_change_preserves_receipt_and_requires_explicit_retry(estate):
    route, package, conn = estate
    request_id = str(uuid4())
    calls = []
    def transport(*args, **kwargs):
        calls.append(kwargs["body"])
        return TransportOutcome("unknown") if len(calls) == 1 else TransportOutcome(
            "submitted", native_returncode=0)
    first = _call(route, package, request_id, transport=transport)
    original = _receipt(route, request_id)
    assert first.delivery == "unknown" and len(calls) == 1
    changed = replace(route, activation_id="activation-2", plan_id="plan-2", release_id="release-2")
    replay = _call(changed, package, request_id, transport=transport)
    assert replay.replayed and replay.delivery == "unknown" and len(calls) == 1
    assert _receipt(route, request_id) == original
    retried = _call(changed, package, request_id, transport=transport, retry_uncertain=True)
    saved = _receipt(route, request_id)
    assert retried.delivery == "submitted" and retried.message_id == first.message_id
    assert len(calls) == 2 and len(saved.message_attempts) == 2
    assert saved.intent == original.intent
    assert saved.message_attempts[0].observation.status == "unknown"
    assert saved.message_attempts[1].observation.status == "submitted"
    assert _counts(conn) == (1, 2)


def test_reply_freezes_parent_answer_and_replays_without_resend(estate):
    route, package, conn = estate
    request_id = str(uuid4())
    parent_id = mint_msg_id()
    calls = []
    def transport(*args, **kwargs):
        calls.append(kwargs["body"])
        return TransportOutcome("submitted", native_returncode=0)
    first = _call(route, package, request_id, kind="answer",
                  parent_message_id=parent_id, transport=transport)
    row = conn.execute("SELECT message_class, reply_to_msg_id FROM communications WHERE msg_id=?",
                       (first.message_id,)).fetchone()
    assert tuple(row) == ("answer", parent_id)
    assert f"Reply to: {parent_id}" in calls[0]
    replay = _call(route, package, request_id, kind="answer",
                   parent_message_id=parent_id, transport=transport)
    assert replay.replayed and replay.message_id == first.message_id and len(calls) == 1
    with pytest.raises(ReceiptConflict):
        _call(route, package, request_id, kind="answer",
              parent_message_id=mint_msg_id(), transport=lambda *a, **k: pytest.fail("retargeted"))


def test_unlinked_report_records_atomic_marker_without_task_effect(estate):
    route, package, conn = estate
    route = replace(route, peer=route.manager, peer_destination=route.manager_destination)
    report = ReportPayload("progress", summary="Private report evidence", percent=25)
    request_id = str(uuid4())
    calls = []
    def transport(*args, **kwargs):
        calls.append(kwargs["body"])
        return TransportOutcome("submitted", native_returncode=0)
    first = messages.send_unlinked_report(route, package, report, request_id=request_id,
                                          trusted_tiers={}, transport=transport,
                                          notify=lambda *a, **k: "alerted",
                                          clear=lambda *a, **k: None)
    assert first.recording == "committed" and first.delivery == "submitted"
    comm = conn.execute("SELECT message_class, body FROM communications WHERE msg_id=?",
                        (first.message_id,)).fetchone()
    marker = conn.execute("SELECT detail FROM events WHERE event='report_status'").fetchone()
    assert comm[0] == "report" and json.loads(comm[1])["summary"] == report.summary
    assert json.loads(marker[0])["msg_id"] == first.message_id
    assert conn.execute("SELECT count(*) FROM work_items").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM assignments").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE kind='task'").fetchone()[0] == 0
    assert len(calls) == 1 and "Private report evidence" in calls[0]
    saved = _receipt(route, request_id)
    assert saved.intent.operation == "fleet.reports.submit"
    assert len(saved.intent.stages[0].facts) == 2
    assert b"Private report evidence" not in (
        route.selected.paths.root / "state/requests" / route.selected_fleet_uid / (request_id + ".json")
    ).read_bytes()
    replay = messages.send_unlinked_report(route, package, report, request_id=request_id,
                                           trusted_tiers={}, transport=transport,
                                           notify=lambda *a, **k: "alerted",
                                           clear=lambda *a, **k: None)
    assert replay.replayed and replay.message_id == first.message_id and len(calls) == 1


def test_unlinked_report_o1_replays_missing_batch_without_second_send(estate, monkeypatch):
    route, package, conn = estate
    route = replace(route, peer=route.manager, peer_destination=route.manager_destination)
    report = ReportPayload("blocked", reason="Private blocker")
    request_id = str(uuid4())
    original = messages.emit_batch
    def outage(root, raws, **kwargs):
        if raws[0]["event_type"] == "communication":
            raise sqlite3.OperationalError("private report recorder outage")
        return original(root, raws, **kwargs)
    monkeypatch.setattr(messages, "emit_batch", outage)
    calls = []
    def transport(*args, **kwargs):
        calls.append(kwargs["body"])
        return TransportOutcome("submitted", native_returncode=0)
    first = messages.send_unlinked_report(route, package, report, request_id=request_id,
                                          trusted_tiers={}, transport=transport,
                                          notify=lambda *a, **k: "alerted",
                                          clear=lambda *a, **k: None)
    assert first.exit_code == 11 and first.code == "recording_degraded"
    assert len(calls) == 1 and "Recording degraded" in calls[0]
    assert conn.execute("SELECT count(*) FROM communications").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE event='report_status'").fetchone()[0] == 0
    monkeypatch.setattr(messages, "emit_batch", original)
    replay = messages.send_unlinked_report(route, package, report, request_id=request_id,
                                           trusted_tiers={}, transport=transport,
                                           notify=lambda *a, **k: "alerted",
                                           clear=lambda *a, **k: None)
    assert replay.replayed and replay.recording == "committed" and len(calls) == 1
    assert conn.execute("SELECT count(*) FROM communications").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM events WHERE event='report_status'").fetchone()[0] == 1


def test_linked_report_notifies_after_exact_commit_with_operation_attempt_two(estate, monkeypatch):
    route, package, conn = estate
    actors = {"caller": route.caller, "worker": route.peer, "manager": route.manager}
    manager_ctx = TaskOperationContext(route.selected, route.host_uid,
                                       route.selected_fleet_uid, route.manager, actors)
    task = tasks.admit(manager_ctx, str(uuid4()), title="Native report notification")
    assignment = tasks.assign(manager_ctx, str(uuid4()), task.task_id, bot_id="worker")
    worker_ctx = replace(manager_ctx, caller=route.peer)
    native_route = replace(route, caller=route.peer, peer=route.manager,
                           peer_destination=route.manager_destination)
    request_id = str(uuid4())
    reported = tasks.progress(worker_ctx, request_id, assignment.assignment_id,
                              ReportPayload("progress", summary="Started"),
                              route=native_route.receipt_binding())
    assert reported.recording == "committed" and reported.notification == "pending"
    calls = []
    def transport(*args, **kwargs):
        calls.append(kwargs["body"])
        return TransportOutcome("submitted", "sha256:" + "a" * 64, 99, 0)
    envelope = messages.RenderedNativeEnvelope(reported.message_id, "Private report prompt")
    with locked_request(route.selected.paths.root, route.selected_fleet_uid, request_id) as store:
        with monkeypatch.context() as patch:
            patch.setattr(messages, "reconcile_facts", lambda *_: FactProof("unknown", "private outage"))
            with pytest.raises(messages.MessageConflict, match="strict recording fact is absent"):
                messages.send_committed_native_attempt(
                    native_route, package, store, store.load(), envelope,
                    request_id=request_id, transport=transport)
        assert store.load().attempt == 1 and calls == []
        first = messages.send_committed_native_attempt(
            native_route, package, store, store.load(), envelope,
            request_id=request_id, transport=transport)
        assert first.attempt_no == 2 and first.delivery == "submitted"
        assert first.transmission_recording == "committed" and first.request_persisted
        replay = messages.send_committed_native_attempt(
            native_route, package, store, store.load(), envelope,
            request_id=request_id, transport=transport)
        assert replay.replayed and replay.attempt_no == 2 and replay.delivery == "submitted"
        saved = store.load()
        assert saved.stages[0].status == "committed" and saved.stages[0].attempt == 1
        assert saved.stages[1].status == "submitted" and saved.stages[1].attempt == 2
        assert len(saved.message_attempts) == 1 and saved.message_attempts[0].attempt_no == 2
        assert saved.message_attempts[0].recording_status == "committed"
    assert len(calls) == 1 and _counts(conn)[1] == 1
    path = route.selected.paths.root / "state/requests" / route.selected_fleet_uid / (request_id + ".json")
    assert b"Private report prompt" not in path.read_bytes()


def _manager_delivery(estate):
    route, package, conn = estate
    origin = replace(route.origin, bot_id="manager")
    context = replace(route.selected, bot_id=None)  # bind_task_context selects the fleet, not a bot.
    route = replace(route, origin=origin, selected=context, peer_context=context,
                    caller=route.manager)
    actors = {"caller": estate[0].caller, "worker": route.peer, "manager": route.manager}
    ctx = TaskOperationContext(context, route.host_uid, route.selected_fleet_uid,
                               route.manager, actors)
    task = tasks.admit(ctx, str(uuid4()), title="Deliver private assignment")
    assignment = tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="worker")
    return ctx, route, package, conn, task, assignment


def test_assignment_delivery_records_intent_then_sends_once_even_after_task_closes(estate, monkeypatch):
    ctx, route, package, conn, task, assignment = _manager_delivery(estate)
    request_id = str(uuid4())
    calls = []
    def transport(*args, **kwargs):
        calls.append(kwargs["body"])
        return TransportOutcome("submitted", "sha256:" + "a" * 64, 99, 0)
    body = MessageBody("Private assignment body")
    first = deliveries.deliver(ctx, route, package, request_id, assignment.assignment_id,
                               body, transport=transport)
    assert first.recording == first.transmission_recording == "committed"
    assert first.delivery == "submitted" and first.request_persisted and not first.replayed
    assert first.task_id == task.task_id and first.message_id in calls[0]
    assert assignment.assignment_id in calls[0] and len(calls) == 1
    assert _counts(conn) == (1, 1)
    original_outcome = RequestStore.outcome
    def lost_receipt_outcome(self, index, status):
        if index == 0 and status == "committed":
            raise OSError("private receipt outage after Plane commit")
        return original_outcome(self, index, status)
    with monkeypatch.context() as patch:
        patch.setattr(RequestStore, "outcome", lost_receipt_outcome)
        partial = deliveries.deliver(ctx, route, package, str(uuid4()), assignment.assignment_id,
                                     MessageBody("Second private assignment"), transport=transport)
    assert partial.recording == "committed" and partial.delivery == "not_requested"
    assert not partial.request_persisted and partial.message_id and len(calls) == 1
    assert _counts(conn) == (2, 1)
    tasks.withdraw(ctx, str(uuid4()), task.task_id, reason="Finished elsewhere")
    replay = deliveries.deliver(ctx, route, package, request_id, assignment.assignment_id,
                                body, transport=transport)
    assert replay.replayed and replay.message_id == first.message_id
    assert replay.delivery == "submitted" and len(calls) == 1
    receipt = _receipt(route, request_id)
    assert receipt.intent.task_id == task.task_id and receipt.intent.assignment_id == assignment.assignment_id
    assert receipt.stages[0].status == "committed" and receipt.message_attempts[0].attempt_no == 2
    path = route.selected.paths.root / "state/requests" / route.selected_fleet_uid / (request_id + ".json")
    assert b"Private assignment body" not in path.read_bytes()


def test_assignment_delivery_refuses_recorder_outage_and_stale_first_send(estate, monkeypatch):
    ctx, route, package, conn, task, assignment = _manager_delivery(estate)
    original = messages.emit_batch
    def outage(root, raws, **kwargs):
        if raws[0]["event_type"] == "communication":
            raise sqlite3.OperationalError("private recorder outage")
        return original(root, raws, **kwargs)
    monkeypatch.setattr(messages, "emit_batch", outage)
    calls = []
    request_id = str(uuid4())
    with pytest.raises(tasks.TaskRecordingError):
        deliveries.deliver(ctx, route, package, request_id, assignment.assignment_id,
                           MessageBody("Private delivery"),
                           transport=lambda *a, **k: calls.append(k["body"]))
    assert calls == [] and _counts(conn) == (0, 0)
    assert _receipt(route, request_id).message_attempts == ()
    tasks.withdraw(ctx, str(uuid4()), task.task_id, reason="Cancelled before delivery")
    with pytest.raises(tasks.TaskConflictError, match="stale|closed"):
        deliveries.deliver(ctx, route, package, request_id, assignment.assignment_id,
                           MessageBody("Private delivery"),
                           transport=lambda *a, **k: pytest.fail("stale assignment must not send"))
