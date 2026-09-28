"""Pure ordinary-message encoding; no lookup, recording or transport.

Authored bytes stay in memory until the operation hands the raw communication
to emit_api.validate_item, whose Plane contract owns ANSI handling and capture.
The separate native envelope is sent only by the operation's selected transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Literal
from uuid import UUID

from .plane import PLANE_SCHEMA_VERSION
from .plane.contracts import cap_body
from .plane.ids import ID_PATTERNS
from .plane.registries import cap_for
from .request_receipts import MessageRouteBinding, RequestIntent, TransportObservation


MESSAGE_EMITTER = "claudlobby.message.v1"
SendKind = Literal["question", "notice", "chat"]
_SEND_KINDS = frozenset({"question", "notice", "chat"})


class MessagePayloadError(ValueError):
    """An ordinary message cannot be encoded without frozen, valid inputs."""


@dataclass(frozen=True)
class MessageBody:
    """One input rule for --text and UTF-8 --file, preserving authored bytes."""

    text: str
    byte_count: int = field(init=False)
    authored_sha256: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.text, str) or not self.text.strip() or "\0" in self.text:
            raise MessagePayloadError("message body must be nonempty text without null bytes")
        try:
            raw = self.text.encode("utf-8")
        except UnicodeError as exc:
            raise MessagePayloadError("message body must be valid UTF-8") from exc
        cap = cap_for("communication", "body")
        if len(raw) > cap:
            raise MessagePayloadError(f"communication.body exceeds {cap} bytes")
        if not cap_body(self.text).body.strip():
            raise MessagePayloadError("message body has no capturable text")
        object.__setattr__(self, "byte_count", len(raw))
        object.__setattr__(self, "authored_sha256", "sha256:" + hashlib.sha256(raw).hexdigest())

    @classmethod
    def from_input(cls, value: str | bytes) -> MessageBody:
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except UnicodeError as exc:
                raise MessagePayloadError("message file must be UTF-8") from exc
        if not isinstance(value, str):
            raise MessagePayloadError("message input must be text or UTF-8 bytes")
        return cls(value)


def _id(value: str, kind: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERNS[kind], value):
        raise MessagePayloadError(f"canonical {kind} ID required")


def _request_id(value: str) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("noncanonical UUID")
    except (ValueError, AttributeError) as exc:
        raise MessagePayloadError("canonical request UUID required") from exc


def _ordinary_intent(intent: RequestIntent) -> MessageRouteBinding:
    if not isinstance(intent, RequestIntent) or not isinstance(intent.route, MessageRouteBinding):
        raise MessagePayloadError("frozen message intent and route required")
    if intent.task_id is not None or intent.assignment_id is not None:
        raise MessagePayloadError("ordinary messages cannot carry work or assignment links")
    _id(intent.message_id, "msg")
    _id(intent.host_uid, "host")
    _id(intent.fleet_uid, "fleet")
    _id(intent.caller_uid, "actor")
    _id(intent.recipient_uid, "actor")
    if intent.operation not in {"message.send", "message.reply"}:
        raise MessagePayloadError("only ordinary message operations use this encoder")
    return intent.route


def _message_class(intent: RequestIntent, *, kind: SendKind | None,
                   parent_message_id: str | None) -> str:
    _ordinary_intent(intent)
    if intent.operation == "message.send":
        if kind is not None and (not isinstance(kind, str) or kind not in _SEND_KINDS):
            raise MessagePayloadError("ordinary send kind must be question, notice or chat")
        if parent_message_id is not None:
            raise MessagePayloadError("ordinary send cannot claim a reply parent")
        return kind or "chat"
    if intent.operation == "message.reply":
        if kind is not None:
            raise MessagePayloadError("reply kind is fixed to answer")
        _id(parent_message_id, "msg")
        if parent_message_id == intent.message_id:
            raise MessagePayloadError("reply cannot name itself as parent")
        return "answer"
    raise AssertionError("unreachable message operation")


def _envelope(intent: RequestIntent, *, request_id: str, event_id: str,
              occurred_at: str, family: str) -> dict:
    _request_id(request_id)
    _id(event_id, "event")
    route = intent.route
    return {"event_type": family, "emitter": MESSAGE_EMITTER,
            "fleet": route.manager_destination.fleet,
            "source_ref": f"request:{request_id}", "event_id": event_id,
            "occurred_at": occurred_at, "schema_version": PLANE_SCHEMA_VERSION}


def encode_communication(intent: RequestIntent, body: MessageBody, *, request_id: str,
                         event_id: str, occurred_at: str, kind: SendKind | None = None,
                         parent_message_id: str | None = None) -> dict:
    """Encode only ordinary communication; the operation validates/captures it.

    Reply participation and parent existence are admission checks elsewhere.
    Neither task links nor command_type is exposed through this encoder.
    """
    message_class = _message_class(intent, kind=kind, parent_message_id=parent_message_id)
    if not isinstance(body, MessageBody):
        raise MessagePayloadError("validated message body required")
    route = intent.route
    payload = {"msg_id": intent.message_id, "sender": route.caller_alias,
               "recipient": route.recipient_alias,
               "recipient_raw": route.peer_destination.session,
               "message_class": message_class, "body": body.text}
    if parent_message_id is not None:
        payload["reply_to_msg_id"] = parent_message_id
    return {**_envelope(intent, request_id=request_id, event_id=event_id,
                        occurred_at=occurred_at, family="communication"), "payload": payload}


def native_message_envelope(intent: RequestIntent, body: MessageBody, *, request_id: str,
                            kind: SendKind | None = None, parent_message_id: str | None = None,
                            recording_degraded: bool = False) -> str:
    """Frame a native prompt as ordinary communication, including O1 disclosure.

    This text is never stored in a receipt. The transport appends its own
    tracking trailer and computes wire proof after sanitizing the whole prompt.
    """
    message_class = _message_class(intent, kind=kind, parent_message_id=parent_message_id)
    _request_id(request_id)
    if not isinstance(body, MessageBody) or type(recording_degraded) is not bool:
        raise MessagePayloadError("validated message body and degradation flag required")
    route = intent.route
    lines = ["[Claudlobby ordinary message]", f"Message: {intent.message_id}",
             f"From: {route.caller_alias}", f"To: {route.recipient_alias}",
             f"Kind: {message_class}"]
    if parent_message_id is not None:
        lines.append(f"Reply to: {parent_message_id}")
    if recording_degraded:
        lines.append(f"Recording degraded for request {request_id}; message history or request receipt may be incomplete.")
    return "\n".join((*lines, "", body.text))


def native_unlinked_report_envelope(intent: RequestIntent, body: MessageBody, *, request_id: str,
                                    recording_degraded: bool = False) -> str:
    """Render the same native carrier for a typed, unlinked report."""
    if (not isinstance(intent, RequestIntent) or intent.operation != "fleet.reports.submit"
            or not isinstance(intent.route, MessageRouteBinding)
            or not isinstance(body, MessageBody) or type(recording_degraded) is not bool):
        raise MessagePayloadError("frozen unlinked report and body required")
    _id(intent.message_id, "msg")
    _request_id(request_id)
    route = intent.route
    if route.recipient_alias != route.manager_alias:
        raise MessagePayloadError("unlinked report must address the frozen manager")
    lines = ["[Claudlobby unlinked report]", f"Message: {intent.message_id}",
             f"From: {route.caller_alias}", f"To: {route.recipient_alias}"]
    if recording_degraded:
        lines.append(f"Recording degraded for request {request_id}; report history or request receipt may be incomplete.")
    return "\n".join((*lines, "", body.text))


def encode_transmission(intent: RequestIntent, observation: TransportObservation, *,
                        request_id: str, attempt_no: int, event_id: str,
                        occurred_at: str) -> dict:
    """Record one reserved native result without promoting uncertain effects."""
    if not isinstance(intent, RequestIntent) or intent.operation not in {
            "message.send", "message.reply", "fleet.reports.submit", "task.nudge",
            "task.recheck", "assignment.deliver",
            "assignment.progress", "assignment.block", "assignment.return",
            "assignment.complete", "assignment.fail"}:
        raise MessagePayloadError("unsupported native transmission operation")
    if not isinstance(intent.route, MessageRouteBinding):
        raise MessagePayloadError("frozen native route required")
    for value, kind in ((intent.message_id, "msg"), (intent.host_uid, "host"),
                        (intent.fleet_uid, "fleet"), (intent.caller_uid, "actor"),
                        (intent.recipient_uid, "actor")):
        _id(value, kind)
    # Only the native result is encoded here; domain links and any reply parent
    # were frozen and validated by the separate communication/task owner.
    if not isinstance(observation, TransportObservation):
        raise MessagePayloadError("typed transport observation required")
    if type(attempt_no) is not int or attempt_no < 1:
        raise MessagePayloadError("positive fixed attempt number required")
    if observation.status == "submitted":
        if observation.native_returncode != 0:
            raise MessagePayloadError("submitted transmission requires native success")
        state = "pane_submitted"
    elif observation.status == "failed":
        if observation.native_returncode is not None or observation.wire_sha256 is not None:
            raise MessagePayloadError("failed transmission must be a pre-launch failure")
        state = "failed"
    elif observation.status == "unknown":
        state = "unknown"
    else:
        raise MessagePayloadError("unknown transport observation status")
    if (observation.wire_sha256 is None) != (observation.wire_bytes is None):
        raise MessagePayloadError("native wire proof must include hash and byte count")
    if observation.wire_sha256 is not None and (
            not isinstance(observation.wire_sha256, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", observation.wire_sha256)
            or type(observation.wire_bytes) is not int or observation.wire_bytes < 0):
        raise MessagePayloadError("invalid native wire proof")
    payload = {"msg_id": intent.message_id, "attempt_no": attempt_no, "carrier": "tmux",
               "destination": intent.route.peer_destination.session, "state": state}
    if state == "pane_submitted" and observation.wire_sha256 is not None:
        payload.update(wire_sha256=observation.wire_sha256, wire_bytes=observation.wire_bytes)
    return {**_envelope(intent, request_id=request_id, event_id=event_id,
                        occurred_at=occurred_at, family="transmission"), "payload": payload}
