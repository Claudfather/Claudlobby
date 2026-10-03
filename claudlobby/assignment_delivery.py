"""Commit a linked assignment delivery, then submit one native message attempt.

The public caller holds runtime mutation admission. Request lock precedes the
task owner's lock; the task lock is released before native transport. This
operation never changes task state or proves receiver acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
from typing import Callable

from .message_context import MessageRoute
from .message_operations import (MessageConflict, RenderedNativeEnvelope, _record,
                                 send_committed_native_attempt)
from .message_payload import MessageBody
from .message_transport import send as native_send
from .plane import PLANE_SCHEMA_VERSION
from .plane.emit_api import _load_capture_config, validate_item
from .plane.ids import mint_event_id, mint_msg_id
from .request_facts import expected_fact, reconcile_facts
from .request_receipts import (ReceiptConflict, RequestIntent, StagePlan,
                               locked_request, same_native_route, semantic_digest)
from .resources import PackageResources
from .task_operations import (TaskConflictError, TaskOperationContext,
                              TaskRecordingError, _identities, _locked_task, _reader)
from .task_queries import show_assignment
from .task_state import TASK_EMITTER


@dataclass(frozen=True)
class AssignmentDeliveryResult:
    request_id: str
    task_id: str
    assignment_id: str
    message_id: str
    recording: str
    delivery: str                 # submitted, failed, unknown, not_requested; never receiver-verified
    transmission_recording: str
    request_persisted: bool
    replayed: bool
    attempt_no: int | None
    reason: str | None = None
    native_returncode: int | None = None  # the transport's, when it observed one


def _scope(ctx: TaskOperationContext, route: MessageRoute, package: PackageResources,
           body: MessageBody) -> None:
    if (not isinstance(ctx, TaskOperationContext) or not isinstance(route, MessageRoute)
            or not isinstance(body, MessageBody) or package != route.selected.paths.package
            or package != ctx.context.paths.package):
        raise TaskConflictError("frozen task context, route, package and body are required")
    manager_id = ctx.context.fleet.manager
    # Human provenance does not authorize a send. The route owner currently
    # resolves generated bots only; a future operator route needs its own proof.
    if (ctx.caller != ctx.bots.get(manager_id)
            or route.origin.bot_id != manager_id or route.caller != ctx.caller
            or ctx.caller.alias != f"bot:{ctx.context.fleet.name}/{manager_id}"):
        raise TaskConflictError("assignment delivery requires the generated fleet manager")
    peer_name = route.peer.alias.rsplit("/", 1)[-1]
    if (route.selected.paths.root != ctx.root or route.origin.paths.root != ctx.root
            or route.peer_context.paths.root != ctx.root
            or route.origin.fleet.name != ctx.context.fleet.name
            or route.selected.fleet.name != ctx.context.fleet.name
            or route.peer_context.fleet.name != ctx.context.fleet.name
            or route.host_uid != ctx.host_uid or route.selected_fleet_uid != ctx.fleet_uid
            or route.caller_fleet_uid != ctx.fleet_uid or route.peer_fleet_uid != ctx.fleet_uid
            or route.manager != ctx.caller or ctx.bots.get(peer_name) != route.peer
            or route.peer.alias != f"bot:{ctx.context.fleet.name}/{peer_name}"
            or route.peer_destination.root != ctx.root
            or route.peer_destination.fleet != ctx.context.fleet.name
            or route.peer_destination.session != peer_name
            or route.manager_destination.root != ctx.root
            or route.manager_destination.fleet != ctx.context.fleet.name
            or route.manager_destination.session != manager_id):
        raise TaskConflictError("assignment recipient or fleet differs from the frozen route")


def _current_assignment(ctx: TaskOperationContext, route: MessageRoute, view) -> None:
    task = view.task.require_resolved()
    if (not task.open or task.current_assignment != view.assignment
            or view.assignment.assignee_uid != route.peer.uid):
        raise TaskConflictError("assignment is stale, closed or addressed to a different worker")


def _communication(route: MessageRoute, body: MessageBody, request_id: str,
                   task_id: str, assignment_id: str, message_id: str,
                   event_id: str, at: str) -> dict:
    return {"event_type": "communication", "schema_version": PLANE_SCHEMA_VERSION,
            "emitter": TASK_EMITTER, "fleet": route.selected.fleet.name,
            "source_ref": f"request:{request_id}", "event_id": event_id,
            "occurred_at": at,
            "payload": {"msg_id": message_id, "sender": route.caller.alias,
                        "recipient": route.peer.alias,
                        "recipient_raw": route.peer_destination.session,
                        "message_class": "task_request", "command_type": "task",
                        "work_item_id": task_id, "assignment_id": assignment_id,
                        "body": body.text}}


def _native_envelope(route: MessageRoute, body: MessageBody, task_id: str,
                     assignment_id: str, message_id: str) -> RenderedNativeEnvelope:
    text = (f"[Claudlobby assignment delivery]\nTask: {task_id}\n"
            f"Assignment: {assignment_id}\nMessage: {message_id}\n"
            f"From: {route.caller.alias}\nTo: {route.peer.alias}\n\n{body.text}")
    return RenderedNativeEnvelope(message_id, text)


def deliver(ctx: TaskOperationContext, route: MessageRoute, package: PackageResources,
            request_id: str, assignment_id: str, body: MessageBody, *,
            retry_uncertain: bool = False,
            transport: Callable = native_send) -> AssignmentDeliveryResult:
    """Record intent under the task lock; send only after exact committed proof."""
    _scope(ctx, route, package, body)
    modes = _load_capture_config(ctx.root)
    semantic = semantic_digest({"assignment_id": assignment_id,
                                "body": body.text.encode("utf-8")})
    at = datetime.now(timezone.utc).isoformat()
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = store.load()
        if previous is not None:
            old = previous.intent
            if (old.operation != "assignment.deliver" or old.operation_version != 1
                    or old.host_uid != ctx.host_uid or old.fleet_uid != ctx.fleet_uid
                    or old.caller_uid != ctx.caller.uid or old.recipient_uid != route.peer.uid
                    or old.assignment_id != assignment_id or old.semantic_sha256 != semantic
                    or not same_native_route(old.route, route.receipt_binding())):
                raise ReceiptConflict("request UUID already has different assignment delivery semantics")
        with _reader(ctx) as conn:
            first = show_assignment(conn, assignment_id, fleet_uid=ctx.fleet_uid)
            task_id = first.task.task_id
        if previous is not None and previous.intent.task_id != task_id:
            raise ReceiptConflict("assignment task differs from frozen delivery")
        with _locked_task(store, task_id) as check:
            with _reader(ctx) as conn:
                view = show_assignment(conn, assignment_id, fleet_uid=ctx.fleet_uid)
                _identities(ctx, conn, (route.peer,))
                message_id = previous.intent.message_id if previous else mint_msg_id()
                event_id = (previous.intent.stages[0].facts[0].event_id
                            if previous else mint_event_id())
                raw = _communication(route, body, request_id, task_id, assignment_id,
                                     message_id, event_id, at)
                item, _ = validate_item(raw, modes)
                fact = expected_fact(item, host_uid=ctx.host_uid, fleet_uid=ctx.fleet_uid,
                                     parties={ctx.caller.alias: ctx.caller.uid,
                                              route.peer.alias: route.peer.uid})
                intent = RequestIntent("assignment.deliver", 1, ctx.host_uid, ctx.fleet_uid,
                                       ctx.caller.uid, route.peer.uid, semantic,
                                       (StagePlan("recording", (fact,)), StagePlan("delivery")),
                                       task_id=task_id, assignment_id=assignment_id,
                                       message_id=message_id,
                                       route=previous.intent.route if previous else route.receipt_binding())
                if previous is not None and previous.intent != intent:
                    raise ReceiptConflict("delivery fact or capture policy differs from frozen request")
                proof = (reconcile_facts(conn, (fact,)).status if previous else "unrecorded")
                if proof == "conflict":
                    raise ReceiptConflict("delivery communication differs from frozen request fact")
                if proof == "unknown":
                    raise TaskRecordingError(request_id, "delivery communication proof is unavailable")
                if previous is not None and previous.stages[0].status == "committed" and proof != "committed":
                    raise ReceiptConflict("committed delivery fact is absent")
                prior_native = previous.message_attempts[-1] if previous and previous.message_attempts else None
                if proof != "committed" or prior_native is None or retry_uncertain:
                    _current_assignment(ctx, route, view)
                if previous is None:
                    receipt = store.prepare(intent)
                else:
                    receipt = previous
                if proof == "unrecorded":
                    if receipt.stages[0].status == "unknown":
                        receipt = store.outcome(0, "unrecorded")
                    if receipt.attempt == 0 or receipt.stages[0].status == "unrecorded":
                        receipt = store.begin_attempt()
                    check()
                    persisted = [True]
                    proof = _record(ctx.root, route, raw, fact, store=store, stage=0,
                                    attempt_no=None, persistence=persisted)
                    if proof != "committed":
                        raise TaskRecordingError(request_id, "delivery intent did not commit durably")
                    if not persisted[0]:
                        return AssignmentDeliveryResult(
                            request_id, task_id, assignment_id, message_id,
                            "committed", "not_requested", "unknown", False,
                            bool(previous), None, "request_history_unavailable")
                elif receipt.stages[0].status == "unknown":
                    receipt = store.outcome(0, "committed")
                elif receipt.stages[0].status != "committed":
                    raise ReceiptConflict("recorded delivery has no compatible request attempt")
        # No task lock is held during native transport or any later receipt wait.
        try:
            receipt = store.load()
        except OSError:
            return AssignmentDeliveryResult(request_id, task_id, assignment_id, message_id,
                                            "committed", "not_requested", "unknown", False,
                                            bool(previous), None, "request_history_unavailable")
        envelope = _native_envelope(route, body, task_id, assignment_id, message_id)
        try:
            native = send_committed_native_attempt(route, package, store, receipt, envelope,
                                                   request_id=request_id,
                                                   retry_uncertain=retry_uncertain,
                                                   transport=transport)
        except (OSError, sqlite3.Error, MessageConflict) as exc:
            return AssignmentDeliveryResult(request_id, task_id, assignment_id, message_id,
                                            "committed", "not_requested", "unknown", True,
                                            bool(previous), None, type(exc).__name__)
        return AssignmentDeliveryResult(request_id, task_id, assignment_id, message_id,
                                        "committed", native.delivery,
                                        native.transmission_recording, native.request_persisted,
                                        native.replayed, native.attempt_no,
                                        native_returncode=(native.observation.native_returncode
                                                           if native.observation else None))
