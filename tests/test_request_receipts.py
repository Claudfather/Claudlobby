"""Operational receipts retain uncertainty without replaying a transport."""

from dataclasses import replace
import json
import os
import stat
from uuid import uuid4

import pytest

from claudlobby import request_receipts as rr


def _message_route(root):
    root = root.resolve()
    return rr.MessageRouteBinding(
        "activation-1", "plan-1", "release-1", "fleet_" + "2" * 32,
        "fleet_" + "2" * 32, "bot:fleet-a/caller", "bot:fleet-a/worker",
        "actor_" + "8" * 32, "bot:fleet-a/manager",
        rr.NativeDestination(str(root), "fleet-a", "sock-worker", "worker", str(root / "tmux")),
        rr.NativeDestination(str(root), "fleet-a", "sock-manager", "manager", str(root / "tmux")),
    )


def _message_intent(root, intent):
    return replace(intent, operation="message.send", route=_message_route(root))


@pytest.fixture
def receipt_case(tmp_path):
    intent = rr.RequestIntent(
        "task.admit", 1, "host_" + "1" * 32, "fleet_" + "2" * 32,
        "actor_" + "3" * 32, "actor_" + "4" * 32,
        rr.semantic_digest({"body": b"SECRET-private-body", "token": "SECRET-token"}),
        (rr.StagePlan("recording", (rr.ExpectedFact("ev_" + "5" * 32, "communication", "6" * 64),)),
         rr.StagePlan("delivery")), message_id="msg_" + "7" * 32)
    return tmp_path, str(uuid4()), intent


def test_interrupted_recording_and_send_reload_unknown_without_automatic_retry(receipt_case, monkeypatch):
    root, ident, intent = receipt_case
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        with monkeypatch.context() as patch:
            patch.setattr(rr.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("receipt unavailable")))
            with pytest.raises(OSError):
                store.prepare(intent)
        assert store.load() is None and not store.path.exists()
        store.prepare(intent)
        store.begin_attempt()
        store.stage(0)
        store.outcome(0, "committed")
        before = store.path.read_bytes()
        with monkeypatch.context() as patch:
            patch.setattr(rr.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("interrupted before rename")))
            with pytest.raises(OSError):
                store.stage(1)
        assert store.path.read_bytes() == before
        assert not list(store.path.parent.glob("*.tmp"))
        with monkeypatch.context() as patch:
            patch.setattr(rr, "_sync", lambda *_: (_ for _ in ()).throw(OSError("interrupted after rename")))
            with pytest.raises(OSError):
                store.stage(1)
    with pytest.raises(rr.ReceiptError, match="lock"):
        store.load()
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        saved = store.prepare(intent)
        assert [s.status for s in saved.stages] == ["committed", "unknown"]
        assert saved.attempt == 1
        store.begin_attempt()
        with pytest.raises(rr.ReceiptConflict):
            store.stage(0)
        with pytest.raises(rr.ReceiptConflict):
            store.stage(1)
        store.stage(1, retry_uncertain=True)
        store.outcome(1, "submitted")
        store.outcome(1, "received")
        assert store.load().stages[1] == rr.StageOutcome("received", 2)
        with pytest.raises(rr.ReceiptConflict):
            store.outcome(0, "unrecorded")


@pytest.mark.parametrize(("operation", "families"), [
    ("message.send", ("communication",)),
    ("task.assign", ("work_item", "assignment", "task")),
])
def test_uuid_conflicts_and_private_digest_only_storage(receipt_case, operation, families):
    root, ident, intent = receipt_case
    plans = (rr.StagePlan("recording", tuple(
        rr.ExpectedFact(f"ev_{index:032x}", family, "6" * 64)
        for index, family in enumerate(families, 1))),)
    if operation == "message.send":
        plans += (rr.StagePlan("delivery"),)
    intent = replace(intent, operation=operation,
                     route=_message_route(root) if operation == "message.send" else None,
                     task_id="wi_" + "b" * 32 if operation == "task.assign" else None,
                     assignment_id="asg_" + "c" * 32 if operation == "task.assign" else None,
                     stages=plans)
    assert rr.semantic_digest({"body": b"SECRET-private-body", "token": "SECRET-token"},
                              presentation={"json": True, "retry_uncertain": True}) == intent.semantic_sha256
    assert rr.semantic_digest({"body": b"changed"}) != intent.semantic_sha256
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        assert store.load() is None  # no inference about a prior best-effort send
        saved = store.prepare(intent)
        assert store.prepare(intent) == saved
        assert tuple(f.family for f in store.load().intent.stages[0].facts) == families
        for table in ("events", "communications", "work_items", "assignments", "metric_samples", "registry_snapshots"):
            invalid = replace(intent, stages=(rr.StagePlan("recording", (
                replace(intent.stages[0].facts[0], family=table),)),) + intent.stages[1:])
            with pytest.raises(rr.ReceiptError, match="expected fact"):
                store.prepare(invalid)
        changes = [replace(intent, semantic_sha256="8" * 64),
                   replace(intent, recipient_uid="actor_" + "9" * 32),
                   replace(intent, message_id="msg_" + "a" * 32)]
        if operation == "message.send":
            changes.append(replace(intent, route=replace(intent.route, release_id="release-2")))
        for changed in changes:
            with pytest.raises(rr.ReceiptConflict):
                store.prepare(changed)
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert b"SECRET" not in store.path.read_bytes()
        raw = json.loads(store.path.read_bytes())
        assert raw["intent"].pop("expected_by") is None  # earlier records omit the field
        store.path.write_text(json.dumps(raw))
        assert store.load().intent.expected_by is None
        with pytest.raises(rr.ReceiptError, match="deadline"):
            store.prepare(replace(intent, expected_by="2026-10-01T00:00:00"))
        raw["format_version"] = 2
        store.path.write_text(json.dumps(raw))
        with pytest.raises(rr.ReceiptError):
            store.load()
        assert json.loads(store.path.read_bytes())["format_version"] == 2


def test_flock_excludes_independent_process_and_rejects_inherited_store(receipt_case):
    root, ident, intent = receipt_case
    # fork runs only Python against this owned temp tree, with no native service.
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        store.prepare(intent)
        pid = os.fork()
        if pid == 0:
            try:
                with pytest.raises(rr.ReceiptError, match="lock"):
                    store.load()
                with pytest.raises(rr.ReceiptBusy):
                    with rr.locked_request(root, intent.fleet_uid, ident):
                        pass
            except BaseException:
                os._exit(1)
            os._exit(0)
        assert os.waitpid(pid, 0)[1] == 0
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        assert store.load().intent == intent


def _transmission_fact(event_id):
    return rr.ExpectedFact(event_id, "transmission", "a" * 64,
                           ("emitter", "event_id", "fleet_uid", "host_uid"))


def test_linked_report_notification_reserves_operation_attempt_after_recording(receipt_case):
    root, ident, base = receipt_case
    intent = replace(base, operation="assignment.progress", route=_message_route(root),
                     task_id="wi_" + "b" * 32, assignment_id="asg_" + "c" * 32,
                     stages=(base.stages[0], rr.StagePlan("notification")))
    event_id = "ev_" + "a" * 32
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        store.prepare(intent)
        store.begin_attempt()  # The linked report's own recording operation.
        store.stage(0)
        store.outcome(0, "committed")
        reserved = store.begin_native_attempt(event_id)
        assert reserved.attempt == 2 and len(reserved.message_attempts) == 1
        assert reserved.message_attempts[0].attempt_no == 2
        assert reserved.stages[1] == rr.StageOutcome("unknown", 2)
        store.observe_message_transport(2, rr.TransportObservation("submitted", native_returncode=0))
        store.prepare_message_transmission(2, _transmission_fact(event_id))
        store.stage_message_transmission(2)
        store.message_transmission_outcome(2, "committed")
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        saved = store.load()
        assert saved.stages[0] == rr.StageOutcome("committed", 1)
        assert saved.stages[1] == rr.StageOutcome("submitted", 2)
        assert saved.message_attempts[0].recording_status == "committed"
        with pytest.raises(rr.ReceiptConflict):
            store.begin_native_attempt("ev_" + "d" * 32, retry_uncertain=True)


def test_message_reservation_survives_missing_post_send_observation(receipt_case, monkeypatch):
    root, ident, intent = receipt_case
    intent = _message_intent(root, intent)
    event_id = "ev_" + "a" * 32
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        store.prepare(intent)
        with pytest.raises(rr.ReceiptError, match="atomically"):
            store.begin_attempt()
        reserved = store.begin_native_attempt(event_id)
        assert reserved.stages[1] == rr.StageOutcome("unknown", 1)
        assert reserved.message_attempts[0].observation is None
        store.stage(0)  # Pre-send intent recording can itself be interrupted.
        assert [s.status for s in store.load().stages] == ["unknown", "unknown"]
        with monkeypatch.context() as patch:
            patch.setattr(rr.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("post-send write unavailable")))
            with pytest.raises(OSError):
                store.observe_message_transport(1, rr.TransportObservation("submitted", native_returncode=0))
        assert store.load().message_attempts[0].observation is None
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        assert store.prepare(intent).message_attempts[0].transmission_event_id == event_id
        with pytest.raises(rr.ReceiptError, match="observation"):
            store.prepare_message_transmission(1, _transmission_fact(event_id))
        with pytest.raises(rr.ReceiptConflict, match="explicit"):
            store.begin_native_attempt("ev_" + "b" * 32)
        # Inspection/reopen is not a transport replay or a claim of no send,
        # even if the process died before intent recording finished.
        assert [s.status for s in store.load().stages] == ["unknown", "unknown"]


def test_retained_native_observation_recovers_exact_transmission_fact(receipt_case, monkeypatch):
    root, ident, intent = receipt_case
    intent = _message_intent(root, intent)
    event_id = "ev_" + "a" * 32
    observation = rr.TransportObservation("submitted", "sha256:" + "b" * 64, 42, 0)
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        store.prepare(intent)
        store.begin_native_attempt(event_id)
        store.stage(0)
        store.outcome(0, "committed")  # Caller-proven exact communication reconciliation.
        assert [s.status for s in store.load().stages] == ["committed", "unknown"]
        with monkeypatch.context() as patch:
            patch.setattr(rr, "_sync", lambda *_: (_ for _ in ()).throw(OSError("after rename")))
            with pytest.raises(OSError):
                store.observe_message_transport(1, observation)
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        saved = store.load()
        assert saved.stages[0] == rr.StageOutcome("committed", 1)
        assert saved.message_attempts[0].observation == observation
        assert saved.stages[1] == rr.StageOutcome("submitted", 1)
        fact = _transmission_fact(event_id)
        with pytest.raises(rr.ReceiptConflict, match="reserved"):
            store.prepare_message_transmission(1, _transmission_fact("ev_" + "c" * 32))
        store.prepare_message_transmission(1, fact)
        assert store.prepare_message_transmission(1, fact).message_attempts[0].transmission_fact == fact
        with pytest.raises(rr.ReceiptConflict, match="immutable"):
            store.observe_message_transport(1, rr.TransportObservation("unknown"))
        store.stage_message_transmission(1)
        assert store.load().message_attempts[0].recording_status == "unknown"
        store.message_transmission_outcome(1, "committed")
        assert store.load().message_attempts[0].recording_status == "committed"
        with pytest.raises(rr.ReceiptConflict):
            store.begin_native_attempt("ev_" + "d" * 32, retry_uncertain=True)
        assert b"SECRET" not in store.path.read_bytes()


def test_submitted_message_can_restage_absent_communication_without_new_send(receipt_case):
    root, ident, intent = receipt_case
    intent = _message_intent(root, intent)
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        store.prepare(intent)
        store.begin_native_attempt("ev_" + "a" * 32)
        store.stage(0)
        store.outcome(0, "unrecorded")  # Exact communication fact was proved absent.
        store.observe_message_transport(1, rr.TransportObservation("submitted", native_returncode=0))
        before = store.load()
        assert before.stages[1] == rr.StageOutcome("submitted", 1)
        assert store.stage(0).stages[0] == rr.StageOutcome("unknown", 1)
        assert store.outcome(0, "committed").stages[0] == rr.StageOutcome("committed", 1)
        after = store.load()
        assert after.attempt == 1
        assert after.message_attempts == before.message_attempts
        assert after.stages[1] == before.stages[1]
        with pytest.raises(rr.ReceiptConflict):
            store.begin_native_attempt("ev_" + "b" * 32, retry_uncertain=True)


def test_explicit_second_attempt_retains_first_event_and_fact(receipt_case):
    root, ident, intent = receipt_case
    intent = _message_intent(root, intent)
    first, second = "ev_" + "a" * 32, "ev_" + "b" * 32
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        store.prepare(intent)
        store.begin_native_attempt(first)
        store.observe_message_transport(1, rr.TransportObservation("unknown", native_returncode=-9))
        store.prepare_message_transmission(1, _transmission_fact(first))
        store.stage_message_transmission(1)
        store.message_transmission_outcome(1, "unrecorded")
        with pytest.raises(rr.ReceiptConflict, match="explicit"):
            store.begin_native_attempt(second)
        with pytest.raises(rr.ReceiptConflict, match="already reserved"):
            store.begin_native_attempt(first, retry_uncertain=True)
        store.begin_native_attempt(second, retry_uncertain=True)
        store.observe_message_transport(2, rr.TransportObservation("failed"))
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        saved = store.load()
        assert saved.attempt == 2
        assert [item.transmission_event_id for item in saved.message_attempts] == [first, second]
        assert saved.message_attempts[0].transmission_fact == _transmission_fact(first)
        assert saved.message_attempts[0].recording_status == "unrecorded"
        assert saved.message_attempts[1].observation == rr.TransportObservation("failed")
        assert saved.stages[1] == rr.StageOutcome("failed", 2)


def _recorded_reply(intent, recipient="human:operator"):
    return replace(intent, operation="message.reply", stages=intent.stages[:1],
                   route=rr.RecordedReplyBinding("activation-1", "plan-1", "release-1",
                                                 "fleet_" + "2" * 32, "bot:fleet-a/worker",
                                                 recipient, "msg_" + "9" * 32))


def test_a_reply_recorded_for_a_human_is_keyed_on_positive_facts(receipt_case):
    """#2068: the one message shape with no delivery stage is a reply from a bot
    to a recorded human. A bot recipient, an added delivery stage, another
    operation, or a native route without its delivery stage never takes it."""
    root, ident, intent = receipt_case
    recorded = _recorded_reply(intent)
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        store.prepare(recorded)
        store.begin_attempt()
        store.stage(0)
        store.outcome(0, "committed")
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        assert store.load().intent == recorded  # the binding survives the codec
        with pytest.raises(rr.ReceiptError):
            store.begin_native_attempt("ev_" + "a" * 32)  # nothing is ever carried
    mutants = (_recorded_reply(intent, recipient="bot:fleet-a/caller"),
               replace(_recorded_reply(intent), stages=intent.stages),
               replace(_recorded_reply(intent), operation="message.send"),
               replace(_message_intent(root, intent), operation="message.reply", stages=intent.stages[:1]))
    for mutant in mutants:
        with rr.locked_request(root, intent.fleet_uid, str(uuid4())) as store:
            with pytest.raises(rr.ReceiptError):
                store.prepare(mutant)


@pytest.mark.parametrize('damage', [None, 'bot', 'no_task', 'other_recipient', 'task_event', 'delivery', 'extra_fact'])
def test_feedback_codec_requires_one_human_task_comment(receipt_case, damage):
    root, ident, intent = receipt_case
    route = _message_route(root)
    route = replace(route, caller_alias='human:reviewer', caller_fleet_uid=None,
                    recipient_alias=route.manager_alias, peer_destination=route.manager_destination)
    intent = replace(intent, operation='task.feedback', task_id='wi_' + 'a' * 32,
        recipient_uid=route.manager_uid, route=route,
        stages=(intent.stages[0], rr.StagePlan('notification')))
    if damage == 'bot':
        intent = replace(intent, route=replace(route, caller_alias='bot:fleet-a/caller', caller_fleet_uid=intent.fleet_uid))
    elif damage == 'no_task':
        intent = replace(intent, task_id=None)
    elif damage == 'other_recipient':
        intent = replace(intent, recipient_uid='actor_' + 'b' * 32)
    elif damage == 'task_event':
        intent = replace(intent, stages=(rr.StagePlan('recording', (replace(intent.stages[0].facts[0], family='task'),)), intent.stages[1]))
    elif damage == 'delivery':
        intent = replace(intent, stages=(intent.stages[0], rr.StagePlan('delivery')))
    elif damage == 'extra_fact':
        intent = replace(intent, stages=(rr.StagePlan('recording', (intent.stages[0].facts[0],
            replace(intent.stages[0].facts[0], event_id='ev_' + 'c' * 32))), intent.stages[1]))
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        if damage:
            with pytest.raises(rr.ReceiptError):
                store.prepare(intent)
        else:
            store.prepare(intent)
            assert store.load().intent == intent
            with pytest.raises(rr.ReceiptConflict):
                store.begin_native_attempt('ev_' + 'd' * 32)
