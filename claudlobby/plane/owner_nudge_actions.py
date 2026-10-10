"""Version-2 nudge HTTP semantics; authority and effects stay with OwnerNudges."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from pathlib import Path
import re
from uuid import UUID

from ..activation_identity import read_selected_identity_bindings
from ..active_config import resolve_active_context
from ..message_context import resolve_message_route
from ..operation_context import bind_task_context, OperationContextError
from ..request_receipts import ReceiptConflict
from ..task_operations import nudge_semantic_digest
from ..task_queries import show_task, TaskQueryError
from ..task_state import TaskStateError
from .db import connect_ro, db_file
from .ids import ID_PATTERNS
from .owner_access import AccessDenied, AccessUnavailable, OwnerAccess
from .owner_actions import ActionNotStarted, _exact, _opaque, _text
from .owner_nudges import OwnerNudges
from .owner_source import admit_source, inspect_source, source_host_uid

_FIELDS = {"version", "request_id", "kind", "scope", "target", "submitted_at", "semantic_sha256"}
_TARGET = {"recipient", "task_id", "assignment_id", "release_id"}


def _id(value, kind):
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERNS[kind], value):
        raise AccessDenied("invalid_action_body")


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AccessDenied("invalid_action_body")


def _reason(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise AccessDenied("invalid_action_body")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise AccessDenied("invalid_action_body") from exc


class OwnerNudgeActions:
    def __init__(self, root: Path, *, package):
        self.root, self.package = root, package

    def _context(self, reader, room):
        _text(room)
        host = inspect_source(self.root)
        access = OwnerAccess(self.root)
        access.authorize_read(reader.token, reader.principal, host_uid=host)
        bindings = read_selected_identity_bindings(self.root, room, package=self.package)
        grant = access.authorize_nudge(reader.token, reader.principal,
                                      host_uid=host, fleet_uid=bindings["fleet_uid"])
        selected = resolve_active_context(root=self.root, fleet=room, package=self.package)
        ctx = bind_task_context(selected, operator_alias=grant.actor_alias)
        if (ctx.host_uid != host or ctx.fleet_uid != grant.fleet_uid
                or bindings["host_uid"] != host or bindings["fleet_uid"] != grant.fleet_uid
                or ctx.caller.uid != grant.actor_uid or ctx.caller.alias != grant.actor_alias
                or ctx.caller_fleet_uid is not None
                or {name: actor.uid for name, actor in ctx.bots.items()} != bindings["bots"]):
            raise AccessDenied("nudge_binding_changed")
        manager_name = selected.fleet.manager
        manager = ctx.bots[manager_name]
        route = resolve_message_route(manager_name, root=self.root, fleet=room,
                                      package=self.package, caller_context=ctx)
        if (route.host_uid != host or route.selected_fleet_uid != ctx.fleet_uid
                or route.peer_fleet_uid != ctx.fleet_uid or route.caller != ctx.caller
                or route.peer != manager or route.manager != manager):
            raise AccessDenied("nudge_binding_changed")
        scope = {"workspace": _opaque([str(self.root), host]), "host": host, "fleet": room,
            "viewer": _opaque(["task.nudge", grant.fleet_uid, reader.principal.namespace,
                reader.principal.subject, grant.owner.revision, grant.generation,
                grant.actor_uid, grant.actor_alias])}
        return {"version": 2, "room": room, "scope": scope, "simulation": False,
            "recipients": [{"id": manager.uid, "label": manager_name, "lead": True}],
            "actions": ["nudge"], "release_id": route.release_id}, grant

    def _recheck(self, reader, context, grant, *, preview=False):
        fresh, current = self._context(reader, context["room"])
        if current != grant or fresh["scope"] != context["scope"] or fresh["recipients"] != context["recipients"]:
            raise AccessDenied("nudge_binding_changed")
        if preview and fresh != context:
            raise AccessDenied("nudge_binding_changed")

    def context(self, reader, payload):
        _exact(payload, {"room", "kind"})
        if payload["kind"] != "nudge":
            raise AccessDenied("unsupported_owner_action")
        context, grant = self._context(reader, payload["room"])
        self._recheck(reader, context, grant, preview=True)
        return context

    def _metadata(self, action, payload):
        fields = _FIELDS - ({"semantic_sha256"} if action == "prepare" else set())
        _exact(payload, fields | ({"body"} if action in {"prepare", "send"} else set()))
        if type(payload["version"]) is not int or payload["version"] != 2 or payload["kind"] != "nudge":
            raise AccessDenied("unsupported_owner_action")
        _exact(payload["scope"], {"workspace", "host", "fleet", "viewer"})
        _exact(payload["target"], _TARGET)
        for value in payload["scope"].values():
            _text(value)
        _id(payload["scope"]["host"], "host")
        target = payload["target"]
        _id(target["recipient"], "actor")
        _id(target["task_id"], "work_item")
        if target["assignment_id"] is not None:
            _id(target["assignment_id"], "assignment")
        if not isinstance(target["release_id"], str) or not re.fullmatch(r"r-[0-9a-f]{64}", target["release_id"]):
            raise AccessDenied("invalid_action_body")
        try:
            if not isinstance(payload["request_id"], str) or str(UUID(payload["request_id"])) != payload["request_id"]:
                raise ValueError
            _text(payload["submitted_at"])
            stamp = datetime.fromisoformat(payload["submitted_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise AccessDenied("invalid_action_body") from exc
        if action != "prepare":
            _digest(payload["semantic_sha256"])
        if action in {"prepare", "send"}:
            _reason(payload["body"])
        return fields

    def _bind(self, reader, payload, *, preview=False):
        context, grant = self._context(reader, payload["scope"]["fleet"])
        if payload["scope"] != context["scope"] or payload["target"]["recipient"] != context["recipients"][0]["id"]:
            raise AccessDenied("action_scope_mismatch")
        if preview and payload["target"]["release_id"] != context["release_id"]:
            raise AccessDenied("nudge_selection_changed")
        return context, grant

    def _selected_task(self, payload, grant):
        host = source_host_uid(self.root)
        with closing(connect_ro(db_file(self.root))) as conn:
            conn.execute("BEGIN")
            admit_source(conn, host)
            if host != grant.owner.host_uid:
                raise AccessDenied("nudge_binding_changed")
            task = show_task(conn, payload["target"]["task_id"], fleet_uid=grant.fleet_uid).require_resolved()
            assignment = task.current_assignment.assignment_id if task.current_assignment else None
            if not task.open or assignment != payload["target"]["assignment_id"]:
                raise AccessDenied("nudge_selection_changed")
        if source_host_uid(self.root) != host:
            raise AccessDenied("nudge_binding_changed")

    def prepare(self, reader, payload):
        fields = self._metadata("prepare", payload)
        context, grant = self._bind(reader, payload, preview=True)
        try:
            self._selected_task(payload, grant)
        except (TaskQueryError, TaskStateError) as exc:
            raise AccessDenied("nudge_selection_changed") from exc
        digest = nudge_semantic_digest(payload["target"]["task_id"], reason=payload["body"],
            by=grant.actor_alias, expected_assignment_id=payload["target"]["assignment_id"])
        self._recheck(reader, context, grant, preview=True)
        return {key: payload[key] for key in fields} | {"semantic_sha256": digest}

    def admit_response(self, action, reader, result):
        room = result["room"] if action == "context" else result["scope"]["fleet"]
        context, grant = self._context(reader, room)
        if action == "context":
            if result != context:
                raise AccessDenied("nudge_binding_changed")
            return
        if result["scope"] != context["scope"] or result["target"]["recipient"] != context["recipients"][0]["id"]:
            raise AccessDenied("nudge_binding_changed")
        if action == "prepare":
            if result["target"]["release_id"] != context["release_id"]:
                raise AccessDenied("nudge_selection_changed")
            try:
                self._selected_task(result, grant)
            except (TaskQueryError, TaskStateError) as exc:
                raise AccessDenied("nudge_selection_changed") from exc

    def operation(self, action, reader, payload):
        try:
            fields = self._metadata(action, payload)
            context, grant = self._bind(reader, payload, preview=action == "send")
            if action == "send":
                expected = nudge_semantic_digest(payload["target"]["task_id"], reason=payload["body"],
                    by=grant.actor_alias, expected_assignment_id=payload["target"]["assignment_id"])
                if expected != payload["semantic_sha256"]:
                    raise AccessDenied("nudge_intent_changed")
            self._recheck(reader, context, grant, preview=action == "send")
            adapter = OwnerNudges(self.root, package=self.package)
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
                adapter.nudge(reader, **options, reason=payload["body"],
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
                if (retained.operation != "task.nudge" or retained.host_uid != grant.owner.host_uid
                        or retained.fleet_uid != grant.fleet_uid or retained.caller_uid != grant.actor_uid
                        or retained.task_id != payload["target"]["task_id"]
                        or retained.recipient_uid != payload["target"]["recipient"]
                        or retained.assignment_id != payload["target"]["assignment_id"]
                        or retained.route is None or retained.route.release_id != payload["target"]["release_id"]
                        or retained.semantic_sha256 != payload["semantic_sha256"]):
                    raise ReceiptConflict("retained nudge differs from submitted intent")
                committed = any(stage.kind == "recording" and len(stage.expected_facts) == 2
                    and {fact.family for fact in stage.expected_facts} == {"task", "communication"}
                    and stage.proof is not None and stage.proof.status == "committed"
                    and stage.proof.matched == 2 for stage in retained.stages)
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
        except Exception:
            pass
        self._recheck(reader, context, grant)
        return {key: payload[key] for key in fields} | {"status": status}
