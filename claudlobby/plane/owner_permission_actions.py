"""Exact task permission decisions through the existing owner HTTP carriers.

This adapter authenticates the locally granted human and records through the
canonical task owner. It never executes an approved effect or retries a send.
"""
from contextlib import closing
import json
import re
from uuid import UUID

from ..activation_identity import read_selected_identity_bindings
from ..active_config import resolve_active_context
from ..operation_context import bind_task_context, OperationContextError
from ..request_queries import read_request
from ..request_receipts import ReceiptConflict
from ..runtime_admission import RuntimeIdentity, mutation_admission
from ..commands.task_permissions import decide_and_notify_permission, inspect_permission_notification
from ..task_permissions import (list_task_permissions,
                               permission_decision_digest, read_permission)
from ..task_queries import TaskQueryError
from .db import connect_ro, db_file
from .ids import ID_PATTERNS
from .owner_access import AccessDenied, AccessUnavailable, VerifiedReader
from .owner_action_authorization import action_authorization, scope_binding, scope_inputs
from .owner_actions import ActionNotStarted, _exact, _opaque, _text
from .owner_service import admitted_action, check_action_process
from .owner_source import admit_source, inspect_source

_FIELDS = {"version", "kind", "request_id", "scope", "target", "decision", "semantic_sha256"}


class OwnerPermissionActions:
    def __init__(self, root, *, package, authorization=None):
        self.root, self.package = root, package
        self.authorization = action_authorization(root, authorization)

    @admitted_action
    def _context(self, reader, room):
        if type(reader) is not VerifiedReader:
            raise AccessDenied("verified_reader_required")
        check_action_process(self.root, self.package)
        _text(room)
        host = inspect_source(self.root)
        access = self.authorization
        access.authorize_read(reader.token, reader.principal, host_uid=host)
        bindings = read_selected_identity_bindings(self.root, room, package=self.package)
        grant = access.authorize_permission(reader.token, reader.principal,
            host_uid=host, fleet_uid=bindings["fleet_uid"])
        selected = resolve_active_context(root=self.root, fleet=room, package=self.package)
        ctx = bind_task_context(selected, operator_alias=grant.actor_alias)
        if (ctx.host_uid != host or ctx.fleet_uid != grant.fleet_uid
                or bindings["host_uid"] != host or bindings["fleet_uid"] != grant.fleet_uid
                or ctx.caller.uid != grant.actor_uid or ctx.caller.alias != grant.actor_alias
                or ctx.caller_fleet_uid is not None
                or {name: actor.uid for name, actor in ctx.bots.items()} != bindings["bots"]):
            raise AccessDenied("permission_binding_changed")
        from ..activation_state import read_selection
        selection = read_selection(self.root)
        if selection is None:
            raise AccessUnavailable("active release unavailable")
        binding = scope_binding(access, reader, host)
        scope = {"workspace": _opaque(scope_inputs([str(self.root), host], binding)),
            "host": host, "fleet": room,
            "viewer": _opaque(scope_inputs(["task.permission", grant.fleet_uid,
                reader.principal.namespace, reader.principal.subject, grant.owner.revision,
                grant.generation, grant.actor_uid, grant.actor_alias], binding))}
        return {"version": 2, "room": room, "scope": scope, "simulation": False,
                "actions": ["permission"], "release_id": selection["release_id"]}, grant, ctx

    def _recheck(self, reader, context, grant, *, release=False):
        fresh, current, _ = self._context(reader, context["room"])
        if current != grant or fresh["scope"] != context["scope"] or release and fresh != context:
            raise AccessDenied("permission_binding_changed")

    def _read(self, context, grant, permission_id=None, task_id=None, decision_event_id=None):
        with closing(connect_ro(db_file(self.root))) as conn:
            conn.execute("BEGIN")
            admit_source(conn, context["scope"]["host"])
            options = dict(root=self.root, host_uid=context["scope"]["host"], fleet_uid=grant.fleet_uid)
            if permission_id is not None:
                permission = read_permission(conn, permission_id, **options)
                if decision_event_id is None:
                    return permission
                row = conn.execute("SELECT detail FROM events WHERE event_id=? AND host_uid=? "
                    "AND fleet_uid=? AND source_ref=?", (decision_event_id, options["host_uid"],
                    grant.fleet_uid, "permission:" + permission_id)).fetchone()
                if row is None:
                    raise ReceiptConflict("decision event does not belong to this permission")
                # The full history above proved this fact's canonical receipt.
                return permission, json.loads(row["detail"]).get("release_id")
            return list_task_permissions(conn, task_id, **options)

    def context(self, reader, payload):
        _exact(payload, {"kind", "room", "task_id"})
        if payload["kind"] != "permission":
            raise AccessDenied("unsupported_owner_action")
        context, grant, _ = self._context(reader, payload["room"])
        items, more = self._read(context, grant, task_id=payload["task_id"])
        self._recheck(reader, context, grant, release=True)
        return {**context, "task_id": payload["task_id"],
                "permissions": list(items), "has_more": more}

    @staticmethod
    def _metadata(action, payload):
        if action not in {"prepare", "send", "receipt"}:
            raise AccessDenied("unsupported_owner_action")
        fields = _FIELDS - ({"semantic_sha256"} if action == "prepare" else set())
        _exact(payload, fields | ({"body"} if action != "receipt" else set()))
        if type(payload["version"]) is not int or payload["version"] != 2 or payload["kind"] != "permission":
            raise AccessDenied("unsupported_owner_action")
        _exact(payload["scope"], {"workspace", "host", "fleet", "viewer"})
        for value in payload["scope"].values():
            _text(value)
        _exact(payload["target"], {"permission_id", "task_id", "scope_sha256", "release_id"})
        for key, pattern in {"permission_id": r"perm_[0-9a-f]{32}",
                "task_id": ID_PATTERNS["work_item"], "scope_sha256": r"[0-9a-f]{64}",
                "release_id": r"r-[0-9a-f]{64}"}.items():
            value = payload["target"][key]
            if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
                raise AccessDenied("invalid_action_body")
        try:
            if not isinstance(payload["request_id"], str) or str(UUID(payload["request_id"])) != payload["request_id"]:
                raise ValueError
        except ValueError as exc:
            raise AccessDenied("invalid_action_body") from exc
        if payload["decision"] not in ("approve", "decline", "revoke"):
            raise AccessDenied("invalid_action_body")
        if action != "prepare" and (not isinstance(payload["semantic_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload["semantic_sha256"])):
            raise AccessDenied("invalid_action_body")
        return fields

    @staticmethod
    def _digest(payload):
        try:
            return permission_decision_digest(payload["target"]["permission_id"],
                payload["target"]["scope_sha256"], payload["body"], release_id=payload["target"]["release_id"])
        except TaskQueryError as exc:
            raise AccessDenied("invalid_action_body") from exc

    def _bind(self, reader, payload, *, release=False):
        context, grant, ctx = self._context(reader, payload["scope"]["fleet"])
        if context["scope"] != payload["scope"]:
            raise AccessDenied("action_scope_mismatch")
        if release and context["release_id"] != payload["target"]["release_id"]:
            raise AccessDenied("permission_selection_changed")
        permission = self._read(context, grant, permission_id=payload["target"]["permission_id"])
        if (permission.task_id != payload["target"]["task_id"]
                or permission.scope.digest != payload["target"]["scope_sha256"]):
            raise AccessDenied("permission_selection_changed")
        return context, grant, ctx, permission

    def prepare(self, reader, payload):
        fields = self._metadata("prepare", payload)
        context, grant, _, permission = self._bind(reader, payload, release=True)
        expected = "approved" if payload["decision"] == "revoke" else "pending"
        if permission.state != expected:
            raise AccessDenied("permission_selection_changed")
        digest = self._digest(payload)
        self._recheck(reader, context, grant, release=True)
        return {key: payload[key] for key in fields} | {"semantic_sha256": digest}

    def admit_response(self, action, reader, result):
        if action == "context":
            context, grant, _ = self._context(reader, result["room"])
            if any(result[key] != value for key, value in context.items()):
                raise AccessDenied("permission_binding_changed")
        else:
            context, grant, _, _ = self._bind(reader, result, release=action == "prepare")
        self._recheck(reader, context, grant, release=action in {"context", "prepare"})

    def operation(self, action, reader, payload):
        try:
            fields = self._metadata(action, payload)
            context, grant, _, permission = self._bind(reader, payload, release=action == "send")
            if action == "send" and self._digest(payload) != payload["semantic_sha256"]:
                raise AccessDenied("permission_intent_changed")
            self._recheck(reader, context, grant, release=action == "send")
        except (AccessDenied, AccessUnavailable, OperationContextError, TaskQueryError) as exc:
            if action == "send":
                raise ActionNotStarted(exc) from exc
            raise
        conflict = False
        if action == "send":
            try:
                identity = RuntimeIdentity(RuntimeIdentity.current().cli, self.package.native, self.package.artifact_id)
                with mutation_admission(self.root, identity=identity,
                        expected_release=payload["target"]["release_id"]):
                    current, fresh, ctx, _ = self._bind(reader, payload, release=True)
                    if fresh != grant or current != context:
                        raise AccessDenied("permission_binding_changed")
                    def admitted(conn):
                        admit_source(conn, ctx.host_uid)
                        self._recheck(reader, context, grant, release=True)
                    decide_and_notify_permission(ctx, self.package, payload["request_id"], permission.permission_id,
                        decision=payload["decision"], scope_sha256=payload["target"]["scope_sha256"],
                        reason=payload["body"], admit_read=admitted, release_id=payload["target"]["release_id"],
                        admit_notification=lambda: self._recheck(reader, context, grant, release=True))
            except (AccessDenied, AccessUnavailable):
                raise
            except ReceiptConflict:
                conflict = True
            except Exception:
                pass  # A lost response may follow a canonical commit; inspect only.
        status, current_permission = "unknown", None
        try:
            if not conflict:
                retained = read_request(self.root, grant.fleet_uid, payload["request_id"])
                if (retained.operation != "task.permission." + payload["decision"]
                        or retained.host_uid != context["scope"]["host"] or retained.fleet_uid != grant.fleet_uid
                        or retained.caller_uid != grant.actor_uid or retained.task_id != permission.task_id
                        or retained.assignment_id != permission.assignment_id
                        or retained.semantic_sha256 != payload["semantic_sha256"]):
                    raise ReceiptConflict("permission receipt differs from this decision")
                if (len(retained.stages) == 1 and retained.stages[0].kind == "recording"
                        and len(retained.stages[0].expected_facts) == 1
                        and retained.stages[0].proof is not None
                        and retained.stages[0].proof.status == "committed"):
                    observed, recorded_release = self._read(context, grant, permission_id=permission.permission_id,
                        decision_event_id=retained.stages[0].expected_facts[0].event_id)
                    if recorded_release != payload["target"]["release_id"]:
                        raise ReceiptConflict("permission receipt belongs to another release")
                    status, current_permission = "recorded", observed.as_dict()
        except (AccessDenied, AccessUnavailable):
            raise
        except Exception:
            pass
        self._recheck(reader, context, grant)
        notification = "unknown"
        if status == "recorded":
            _, _, ctx = self._context(reader, context["room"])
            notification = inspect_permission_notification(ctx, payload["request_id"], permission, payload["decision"])
            self._recheck(reader, context, grant)
        return {key: payload[key] for key in fields} | {"status": status,
            "permission": current_permission, "execution": "not_requested", "notification": notification}
