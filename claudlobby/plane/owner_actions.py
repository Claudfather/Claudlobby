"""Grant-bound ordinary-message HTTP payloads; no identity creation or retries."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from uuid import UUID

from ..activation_identity import read_selected_identity_bindings
from ..active_config import resolve_active_context
from ..context import resolve_paths
from ..operation_context import bind_task_context
from ..request_receipts import ReceiptConflict
from .owner_access import AccessDenied, AccessUnavailable, VerifiedReader
from .owner_messages import OwnerMessages
from .owner_source import inspect_source


def _exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise AccessDenied("invalid_action_body")


def _text(value, limit=240):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise AccessDenied("invalid_action_body")


def _opaque(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()


class OwnerActions:
    def __init__(self, root: Path, *, package=None):
        paths = resolve_paths(root=root, package=package)
        self.root, self.package = paths.root, paths.package

    def _context(self, reader: VerifiedReader, room: str):
        _text(room)
        host = inspect_source(self.root)
        # Session admission precedes configuration/registry access.
        from .owner_access import OwnerAccess
        access = OwnerAccess(self.root)
        access.authorize_read(reader.token, reader.principal, host_uid=host)
        bindings = read_selected_identity_bindings(self.root, room, package=self.package)
        grant = access.authorize_message(reader.token, reader.principal,
            host_uid=host, fleet_uid=bindings["fleet_uid"])
        selected = resolve_active_context(root=self.root, fleet=room, package=self.package)
        ctx = bind_task_context(selected, operator_alias=grant.actor_alias)
        if (ctx.context.fleet.name != room or ctx.host_uid != host
                or bindings["host_uid"] != host or ctx.fleet_uid != grant.fleet_uid
                or ctx.caller.uid != grant.actor_uid or ctx.caller.alias != grant.actor_alias
                or ctx.caller_fleet_uid is not None
                or {name: actor.uid for name, actor in ctx.bots.items()} != bindings["bots"]):
            raise AccessDenied("message_binding_changed")
        recipients = [{"id": actor.uid, "label": name} for name, actor in sorted(ctx.bots.items())]
        if not 1 <= len(recipients) <= 100:
            raise AccessUnavailable("owner actions unavailable")
        scope = {"workspace": _opaque([str(self.root), host]), "host": host,
            "fleet": room, "viewer": _opaque([grant.fleet_uid, reader.principal.namespace,
                reader.principal.subject, grant.owner.revision, grant.actor_uid, grant.actor_alias])}
        return {"version": 1, "room": room, "scope": scope, "simulation": False,
                "recipients": recipients, "actions": ["message"]}, grant

    def context(self, reader, payload):
        _exact(payload, {"room"})
        context, grant = self._context(reader, payload["room"])
        self._recheck(reader, context, grant)
        return context

    def _recheck(self, reader, context, grant):
        fresh, current = self._context(reader, context["room"])
        if fresh != context or current != grant:
            raise AccessDenied("message_binding_changed")

    def admit_response(self, reader, result):
        room = result.get("room", result["scope"]["fleet"])
        current, _ = self._context(reader, room)
        if "room" in result:
            if result != current:
                raise AccessDenied("message_binding_changed")
        elif (result["scope"] != current["scope"] or result["target"]["recipient"]
              not in {item["id"] for item in current["recipients"]}):
            raise AccessDenied("message_binding_changed")

    def operation(self, action, reader, payload):
        fields = {"request_id", "kind", "scope", "target", "submitted_at"}
        _exact(payload, fields | ({"body"} if action == "send" else set()))
        _exact(payload["scope"], {"workspace", "host", "fleet", "viewer"})
        _exact(payload["target"], {"recipient", "task_id"})
        for value in payload["scope"].values():
            _text(value)
        _text(payload["target"]["recipient"])
        try:
            if not isinstance(payload["request_id"], str) or str(UUID(payload["request_id"])) != payload["request_id"]:
                raise ValueError
            _text(payload["submitted_at"])
            datetime.fromisoformat(payload["submitted_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise AccessDenied("invalid_action_body") from exc
        if payload["kind"] != "message" or payload["target"]["task_id"] is not None:
            raise AccessDenied("unsupported_owner_action")
        if action == "send" and (not isinstance(payload["body"], str)
                or not payload["body"].strip() or len(payload["body"]) > 2000):
            raise AccessDenied("invalid_action_body")
        room = payload["scope"]["fleet"]
        if action == "send":
            try:
                payload["body"].encode("utf-8")
            except UnicodeError as exc:
                raise AccessDenied("invalid_action_body") from exc
        context, grant = self._context(reader, room)
        if (payload["scope"] != context["scope"] or payload["target"]["recipient"]
                not in {item["id"] for item in context["recipients"]}):
            raise AccessDenied("action_scope_mismatch")
        adapter = OwnerMessages(self.root, package=self.package)
        options = dict(fleet=context["room"], fleet_uid=grant.fleet_uid,
            recipient_uid=payload["target"]["recipient"], request_id=payload["request_id"],
            expected_grant=grant)
        conflicting_send = False
        if action == "send":
            self._recheck(reader, context, grant)
            try:
                adapter.send(reader, **options, text=payload["body"])
            except (AccessDenied, AccessUnavailable):
                raise
            except ReceiptConflict:
                # Existing proof may be for a different body under this UUID.
                # Never present it as delivery of the newly submitted text.
                conflicting_send = True
            except Exception:
                # An exception can follow a native effect. Inspect the ORIGINAL
                # UUID only; neither failure nor missing history licenses resend.
                pass
        status = "unknown"
        try:
            if conflicting_send:
                self._recheck(reader, context, grant)
                return {"version": 1, **{key: payload[key] for key in fields},
                        "status": "unknown"}
            observed = adapter.inspect(reader, **options)
            proof = observed.receiver
            if (proof.integrity_verdict == "delivered" and proof.receipt_observation == "received"
                    and proof.sender is not None and proof.sender.uid == grant.actor_uid
                    and proof.destination is not None and proof.destination.uid == options["recipient_uid"]):
                status = "delivered"
            elif any(stage.kind == "recording" and stage.expected_facts
                     and all(fact.family == "communication" for fact in stage.expected_facts) and stage.proof is not None
                     and stage.proof.status == "committed" for stage in observed.request.stages):
                status = "recorded"
        except (AccessDenied, AccessUnavailable):
            raise
        except Exception:
            pass
        self._recheck(reader, context, grant)
        return {"version": 1, **{key: payload[key] for key in fields},
                "status": status}
