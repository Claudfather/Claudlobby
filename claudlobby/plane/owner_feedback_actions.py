"""Version-2 feedback HTTP semantics; authority and effects stay with OwnerFeedback."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

from ..activation_identity import read_selected_identity_bindings
from ..active_config import resolve_active_context
from ..message_context import resolve_message_route
from ..message_payload import MessageBody, MessagePayloadError
from ..message_queries import MessageQueryError
from ..operation_context import bind_task_context, OperationContextError
from ..request_receipts import ReceiptConflict
from ..task_operations import feedback_semantic_digest
from ..task_queries import show_task, TaskQueryError
from ..task_state import TaskStateError
from .db import connect_ro, db_file
from .owner_access import AccessDenied, AccessUnavailable, OwnerAccess
from .owner_actions import ActionNotStarted, _exact, _opaque, _text
from .owner_feedback import OwnerFeedback
from .owner_task_action_protocol import metadata
from .owner_source import admit_source, inspect_source, source_host_uid


class OwnerFeedbackActions:
    def __init__(self, root: Path, *, package):
        self.root, self.package = root, package

    def _context(self, reader, room):
        _text(room)
        host = inspect_source(self.root)
        access = OwnerAccess(self.root)
        access.authorize_read(reader.token, reader.principal, host_uid=host)
        bindings = read_selected_identity_bindings(self.root, room, package=self.package)
        grant = access.authorize_feedback(reader.token, reader.principal,
                                          host_uid=host, fleet_uid=bindings["fleet_uid"])
        selected = resolve_active_context(root=self.root, fleet=room, package=self.package)
        ctx = bind_task_context(selected, operator_alias=grant.actor_alias)
        if (ctx.context.fleet.name != room or ctx.host_uid != host or ctx.fleet_uid != grant.fleet_uid
                or bindings["host_uid"] != host or bindings["fleet_uid"] != grant.fleet_uid
                or ctx.caller.uid != grant.actor_uid or ctx.caller.alias != grant.actor_alias
                or ctx.caller_fleet_uid is not None
                or {name: actor.uid for name, actor in ctx.bots.items()} != bindings["bots"]):
            raise AccessDenied("feedback_binding_changed")
        manager_name = selected.fleet.manager
        manager = ctx.bots[manager_name]
        route = resolve_message_route(manager_name, root=self.root, fleet=room,
                                      package=self.package, caller_context=ctx)
        if (route.host_uid != host or route.selected_fleet_uid != ctx.fleet_uid
                or route.peer_fleet_uid != ctx.fleet_uid or route.caller != ctx.caller
                or route.peer != manager or route.manager != manager):
            raise AccessDenied("feedback_binding_changed")
        scope = {"workspace": _opaque([str(self.root), host]), "host": host, "fleet": room,
            "viewer": _opaque(["task.feedback", grant.fleet_uid, reader.principal.namespace,
                reader.principal.subject, grant.owner.revision, grant.generation,
                grant.actor_uid, grant.actor_alias])}
        return {"version": 2, "room": room, "scope": scope, "simulation": False,
            "recipients": [{"id": manager.uid, "label": manager_name, "lead": True}],
            "actions": ["feedback"], "release_id": route.release_id}, grant

    def _recheck(self, reader, context, grant, *, preview=False):
        fresh, current = self._context(reader, context["room"])
        if current != grant or fresh["scope"] != context["scope"] or fresh["recipients"] != context["recipients"]:
            raise AccessDenied("feedback_binding_changed")
        if preview and fresh != context:
            raise AccessDenied("feedback_binding_changed")

    def context(self, reader, payload):
        _exact(payload, {"room", "kind"})
        if payload["kind"] != "feedback":
            raise AccessDenied("unsupported_owner_action")
        context, grant = self._context(reader, payload["room"])
        self._recheck(reader, context, grant, preview=True)
        return context

    def _bind(self, reader, payload, *, preview=False):
        context, grant = self._context(reader, payload["scope"]["fleet"])
        if payload["scope"] != context["scope"] or payload["target"]["recipient"] != context["recipients"][0]["id"]:
            raise AccessDenied("action_scope_mismatch")
        if preview and payload["target"]["release_id"] != context["release_id"]:
            raise AccessDenied("feedback_selection_changed")
        return context, grant

    def _selected_task(self, payload, grant):
        try:
            host = source_host_uid(self.root)
            with closing(connect_ro(db_file(self.root))) as conn:
                conn.execute("BEGIN")
                admit_source(conn, host)
                if host != grant.owner.host_uid:
                    raise AccessDenied("feedback_binding_changed")
                task = show_task(conn, payload["target"]["task_id"], fleet_uid=grant.fleet_uid).require_resolved()
                assignment = task.current_assignment.assignment_id if task.current_assignment else None
                if assignment != payload["target"]["assignment_id"]:
                    raise AccessDenied("feedback_selection_changed")
            if source_host_uid(self.root) != host:
                raise AccessDenied("feedback_binding_changed")
        except (TaskQueryError, TaskStateError) as exc:
            raise AccessDenied("feedback_selection_changed") from exc

    @staticmethod
    def _intent_digest(payload):
        try:
            body = MessageBody.from_input(payload["body"])
        except MessagePayloadError as exc:
            raise AccessDenied("invalid_action_body") from exc
        return feedback_semantic_digest(payload["target"]["task_id"], body=body,
            expected_assignment_id=payload["target"]["assignment_id"])

    def prepare(self, reader, payload):
        fields = metadata("prepare", payload, kind="feedback")
        context, grant = self._bind(reader, payload, preview=True)
        self._selected_task(payload, grant)
        digest = self._intent_digest(payload)
        self._recheck(reader, context, grant, preview=True)
        return {key: payload[key] for key in fields} | {"semantic_sha256": digest}

    def admit_response(self, action, reader, result):
        room = result["room"] if action == "context" else result["scope"]["fleet"]
        context, grant = self._context(reader, room)
        if action == "context":
            if result != context:
                raise AccessDenied("feedback_binding_changed")
            return
        if result["scope"] != context["scope"] or result["target"]["recipient"] != context["recipients"][0]["id"]:
            raise AccessDenied("feedback_binding_changed")
        if action == "prepare":
            if result["target"]["release_id"] != context["release_id"]:
                raise AccessDenied("feedback_selection_changed")
            self._selected_task(result, grant)

    def operation(self, action, reader, payload):
        try:
            fields = metadata(action, payload, kind="feedback")
            context, grant = self._bind(reader, payload, preview=action == "send")
            if action == "send":
                if self._intent_digest(payload) != payload["semantic_sha256"]:
                    raise AccessDenied("feedback_intent_changed")
            self._recheck(reader, context, grant, preview=action == "send")
            adapter = OwnerFeedback(self.root, package=self.package)
            options = dict(fleet=context["room"], fleet_uid=grant.fleet_uid,
                task_id=payload["target"]["task_id"], manager_uid=payload["target"]["recipient"],
                request_id=payload["request_id"], expected_grant=grant)
        except (AccessDenied, AccessUnavailable, OperationContextError) as exc:
            if action == "send":
                raise ActionNotStarted(exc) from exc
            raise
        conflict = False
        if action == "send":
            try:
                adapter.submit(reader, **options, text=payload["body"],
                    expected_assignment_id=payload["target"]["assignment_id"],
                    expected_release_id=payload["target"]["release_id"])
            except (AccessDenied, AccessUnavailable):
                raise
            except ReceiptConflict:
                conflict = True
            except Exception:
                pass  # Raw canonical failure can follow a committed/native effect.
        status = "unknown"
        try:
            if not conflict:
                observed = adapter.inspect(reader, **options)
                retained, receiver = observed.request, observed.receiver
                if (retained.request_id != payload["request_id"] or retained.operation != "task.feedback"
                        or retained.host_uid != grant.owner.host_uid
                        or retained.fleet_uid != grant.fleet_uid or retained.caller_uid != grant.actor_uid
                        or retained.task_id != payload["target"]["task_id"]
                        or retained.recipient_uid != payload["target"]["recipient"]
                        or retained.assignment_id != payload["target"]["assignment_id"]
                        or retained.route is None or retained.route.release_id != payload["target"]["release_id"]
                        or retained.semantic_sha256 != payload["semantic_sha256"]):
                    raise ReceiptConflict("retained feedback differs from submitted intent")
                # Feedback records exactly one linked chat, with no task event.
                committed = any(stage.kind == "recording" and len(stage.expected_facts) == 1
                    and stage.expected_facts[0].family == "communication"
                    and stage.proof is not None and stage.proof.status == "committed"
                    and stage.proof.matched == 1 for stage in retained.stages)
                if committed:
                    status = "recorded"
                    if (receiver.message_id == retained.message_id
                            and receiver.receipt_observation == "received" and receiver.integrity_verdict == "delivered"
                            and receiver.sender is not None and receiver.sender.uid == grant.actor_uid
                            and receiver.sender.alias == grant.actor_alias and receiver.destination is not None
                            and receiver.destination.uid == options["manager_uid"]
                            and receiver.destination.alias == retained.route.recipient_alias):
                        status = "delivered"
        except (AccessDenied, AccessUnavailable):
            raise
        except MessageQueryError as exc:
            # A canonical receipt can refuse its recorded destination before
            # producing an observation. That refusal is not recovery evidence.
            self._recheck(reader, context, grant)
            raise AccessDenied("feedback_receipt_binding_changed") from exc
        except Exception:
            pass
        self._recheck(reader, context, grant)
        return {key: payload[key] for key in fields} | {"status": status}
