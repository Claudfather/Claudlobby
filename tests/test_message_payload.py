"""Pure ordinary message encoders leave validation, capture and effects to owners."""

from dataclasses import asdict, replace
import hashlib
import json

import pytest

from claudlobby.message_payload import (
    MessageBody, MessagePayloadError, encode_communication, encode_transmission,
    native_message_envelope,
)
from claudlobby.plane.emit_api import validate_item
from claudlobby.plane.registries import cap_for
from claudlobby.request_facts import expected_fact
from claudlobby.request_receipts import (
    ExpectedFact, MessageRouteBinding, NativeDestination, RequestIntent, StagePlan,
    TransportObservation, semantic_digest,
)


REQUEST = "11111111-1111-4111-8111-111111111111"
MESSAGE = "msg_" + "1" * 32
PARENT = "msg_" + "2" * 32
COMM_EVENT = "ev_" + "3" * 32
TX_EVENT = "ev_" + "4" * 32
INSTANT = "2026-09-28T12:00:00Z"
HOST = "host_" + "5" * 32
FLEET = "fleet_" + "6" * 32
CALLER = "actor_" + "7" * 32
RECIPIENT = "actor_" + "8" * 32


def _intent(tmp_path, *, operation="message.send"):
    root = str(tmp_path.resolve())
    route = MessageRouteBinding(
        "activation-1", "p-" + "a" * 64, "release-1", FLEET, FLEET,
        "bot:demo/sender", "bot:demo/receiver", "actor_" + "9" * 32,
        "bot:demo/manager",
        NativeDestination(root, "demo", "sock-peer", "receiver", root),
        NativeDestination(root, "demo", "sock-manager", "manager", root),
    )
    return RequestIntent(operation, 1, HOST, FLEET, CALLER, RECIPIENT,
                         semantic_digest({"body": b"original"}),
                         (StagePlan("delivery"),), message_id=MESSAGE, route=route)


def _communication(intent, body, **kwargs):
    return encode_communication(intent, body, request_id=REQUEST, event_id=COMM_EVENT,
                                occurred_at=INSTANT, **kwargs)


def _transmission(intent, observation):
    return encode_transmission(intent, observation, request_id=REQUEST, attempt_no=1,
                               event_id=TX_EVENT, occurred_at=INSTANT)


def test_text_and_file_inputs_share_one_raw_utf8_cap_and_digest():
    authored = "  résumé\nnext\tstep  "
    text = MessageBody.from_input(authored)
    file = MessageBody.from_input(authored.encode("utf-8"))
    assert text == file and text.text == authored
    assert text.byte_count == len(authored.encode("utf-8"))
    assert text.authored_sha256 == "sha256:" + hashlib.sha256(authored.encode("utf-8")).hexdigest()
    cap = cap_for("communication", "body")
    assert MessageBody.from_input("é" * (cap // 2)).byte_count == cap
    for invalid in ("", " \n\t ", "\0hidden", b"\xff", "\ud800", "x" * (cap + 1),
                    "é" * (cap // 2 + 1), None):
        with pytest.raises(MessagePayloadError):
            MessageBody.from_input(invalid)
    with pytest.raises(MessagePayloadError, match="capturable"):
        MessageBody.from_input("\x1b[31m\x1b[0m")


def test_send_kind_and_capture_keep_authored_hash_distinct_from_stored_projection(tmp_path):
    intent = _intent(tmp_path)
    body = MessageBody.from_input("SECRET \x1b[31mred\x1b[0m\nline")
    for kind in (None, "question", "notice", "chat"):
        raw = _communication(intent, body, kind=kind)
        payload = raw["payload"]
        assert payload["message_class"] == (kind or "chat")
        assert payload["sender"] == intent.route.caller_alias
        assert payload["recipient"] == intent.route.recipient_alias
        assert payload["recipient_raw"] == intent.route.peer_destination.session
        assert not {"command_type", "work_item_id", "assignment_id", "supersedes_msg_id"} & set(payload)
        full, _ = validate_item(raw, {"*": "full"})
        metadata, captured = validate_item(raw, {"*": "metadata"})
        assert full[1].body == "SECRET red\nline"
        assert body.authored_sha256 != full[1].body_sha256  # ANSI bytes remain in retry semantics.
        assert metadata[1].body is None and captured["payload"]["body"] is None
        assert metadata[1].body_sha256 == full[1].body_sha256
        fact = expected_fact(metadata, host_uid=HOST, fleet_uid=FLEET,
                             parties={intent.route.caller_alias: CALLER,
                                      intent.route.recipient_alias: RECIPIENT})
        assert fact.event_id == COMM_EVENT and fact.family == "communication"
        assert "SECRET" not in json.dumps(asdict(fact))
    for invalid in ("task_request", "raw_control", "report", "answer", "bogus"):
        with pytest.raises(MessagePayloadError, match="kind"):
            _communication(intent, body, kind=invalid)


def test_reply_requires_explicit_parent_and_cannot_spoof_work_or_control(tmp_path):
    send = _intent(tmp_path)
    reply = replace(send, operation="message.reply")
    body = MessageBody.from_input("Answer as written")
    raw = _communication(reply, body, parent_message_id=PARENT)
    assert raw["payload"]["message_class"] == "answer"
    assert raw["payload"]["reply_to_msg_id"] == PARENT
    validate_item(raw, {})
    for kwargs in ({}, {"parent_message_id": "not-a-msg"},
                   {"parent_message_id": MESSAGE},
                   {"parent_message_id": PARENT, "kind": "chat"}):
        with pytest.raises(MessagePayloadError):
            _communication(reply, body, **kwargs)
    with pytest.raises(MessagePayloadError, match="parent"):
        _communication(send, body, parent_message_id=PARENT)
    with pytest.raises(MessagePayloadError, match="work or assignment"):
        _communication(replace(send, task_id="wi_" + "a" * 32), body)


def test_native_envelope_discloses_only_degraded_recording_without_task_claim(tmp_path):
    intent = _intent(tmp_path)
    body = MessageBody.from_input("Authored\nbody")
    normal = native_message_envelope(intent, body, request_id=REQUEST)
    degraded = native_message_envelope(intent, body, request_id=REQUEST,
                                       recording_degraded=True)
    assert normal.endswith("\n\nAuthored\nbody")
    assert "Claudlobby ordinary message" in normal
    assert "Recording degraded" not in normal
    assert f"Recording degraded for request {REQUEST}" in degraded
    assert degraded.endswith("\n\nAuthored\nbody")
    assert "task dispatch" not in degraded.lower()
    with pytest.raises(MessagePayloadError):
        native_message_envelope(intent, body, request_id=REQUEST, recording_degraded="yes")


def test_transmission_states_do_not_promote_uncertain_native_effects(tmp_path):
    intent = _intent(tmp_path)
    proof = "sha256:" + "b" * 64
    submitted = _transmission(intent, TransportObservation("submitted", proof, 42, 0))
    assert submitted["payload"]["state"] == "pane_submitted"
    assert submitted["payload"]["wire_sha256"] == proof
    assert submitted["payload"]["wire_bytes"] == 42
    validate_item(submitted, {})
    unknown = _transmission(intent, TransportObservation("unknown", proof, 42, -9))
    assert unknown["payload"]["state"] == "unknown"
    assert "wire_sha256" not in unknown["payload"] and "wire_bytes" not in unknown["payload"]
    validate_item(unknown, {})
    failed = _transmission(intent, TransportObservation("failed"))
    assert failed["payload"]["state"] == "failed"
    validate_item(failed, {})
    for invalid in (TransportObservation("submitted"), TransportObservation("failed", proof, 42),
                    TransportObservation("unknown", proof), TransportObservation("submitted", "bad", 2, 0)):
        with pytest.raises(MessagePayloadError):
            _transmission(intent, invalid)
    linked = replace(intent, operation="assignment.progress",
                     task_id="wi_" + "a" * 32, assignment_id="asg_" + "c" * 32,
                     stages=(StagePlan("recording", (ExpectedFact(COMM_EVENT, "communication", "a" * 64),)),
                             StagePlan("notification")))
    assert _transmission(linked, TransportObservation("submitted", proof, 42, 0))["payload"] == submitted["payload"]
    with pytest.raises(MessagePayloadError, match="unsupported"):
        _transmission(replace(intent, operation="task.admit"), TransportObservation("submitted", proof, 42, 0))
