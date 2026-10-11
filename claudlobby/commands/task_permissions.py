"""Public task permission requests and explicit local operator decisions."""

from contextlib import closing
import json
import os
import sqlite3

from ..command_result import CommandFailure, CommandOutput


def _scope(path):
    from ..task_permissions import PermissionScope
    from .checkin import _json_file
    try:
        return PermissionScope.from_dict(_json_file(path, cap=16_384))
    except (CommandFailure, ValueError) as exc:
        raise CommandFailure("invalid_argument", "scope file must contain bounded action, target and constraints JSON") from exc


def _notification(request_id, permission, decision):
    from uuid import UUID, uuid5
    from ..message_payload import MessageBody

    child = str(uuid5(UUID(request_id), "claudlobby.permission.notification.v1"))
    body = MessageBody.from_input(
        "[Claudlobby permission decision]\n"
        f"Decision: {decision}\nPermission: {permission.permission_id}\n"
        f"Task: {permission.task_id}\nAssignment: {permission.assignment_id or '-'}\n"
        f"Decision request: {request_id}\n"
        "Read the permission record for its exact scope and reason. "
        "Check permission again immediately before the action; this notice is not an execution ticket.")
    return child, body


def inspect_permission_notification(ctx, request_id, permission, decision):
    """Inspect only the original decision's targeted notice; never send or repair."""
    from ..message_queries import receipt
    from ..request_queries import read_request
    from ..request_receipts import semantic_digest

    try:
        child, body = _notification(request_id, permission, decision)
        saved = read_request(ctx.root, ctx.fleet_uid, child)
        if (saved.operation != "message.send" or saved.host_uid != ctx.host_uid
                or saved.fleet_uid != ctx.fleet_uid or saved.caller_uid != ctx.caller.uid
                or saved.recipient_uid != permission.requester.uid or saved.message_id is None
                or saved.semantic_sha256 != semantic_digest({"body": body.text.encode("utf-8"), "kind": "notice"})
                or saved.route is None or saved.route.caller_alias != ctx.caller.alias
                or saved.route.caller_fleet_uid is not None
                or saved.route.recipient_alias != permission.requester.alias
                or saved.route.peer_fleet_uid != ctx.fleet_uid):
            return "unknown"
        observed = receipt(ctx, saved.message_id, destination=permission.requester.alias, wait=0)
        if (observed.sender is None or observed.sender.uid != ctx.caller.uid
                or observed.sender.alias != ctx.caller.alias
                or observed.destination is None or observed.destination.uid != permission.requester.uid
                or observed.destination.alias != permission.requester.alias):
            return "unknown"
        if observed.receipt_observation == "received" and observed.integrity_verdict == "delivered":
            return "received"
        if observed.integrity_verdict in {"altered", "truncated"}:
            return "failed"
    except Exception:
        pass  # Missing evidence cannot certify delivery or license a resend.
    return "unknown"


def decide_and_notify_permission(ctx, package, request_id, permission_id, *, decision,
                                 scope_sha256, reason, release_id=None, admit_read=None,
                                 admit_notification=None):
    """Canonical decision first, then one durable notice to its original requester.

    The caller holds runtime admission and authenticates the human. A replay or
    receipt read only inspects notification evidence; a crash gap never sends.
    """
    from ..message_context import resolve_message_route
    from ..task_permissions import decide_permission
    from .message_write import deliver_bound_message

    result = decide_permission(ctx, request_id, permission_id, decision=decision,
        scope_sha256=scope_sha256, reason=reason, release_id=release_id, admit_read=admit_read)
    if not result.replayed:
        try:
            permission = result.permission
            target = next(name for name, actor in ctx.bots.items() if actor == permission.requester)
            route = resolve_message_route(target, root=ctx.root, fleet=ctx.context.fleet.name,
                                          package=package, caller_context=ctx)
            if (route.host_uid != ctx.host_uid or route.selected_fleet_uid != ctx.fleet_uid
                    or route.peer_fleet_uid != ctx.fleet_uid or route.caller != ctx.caller
                    or route.peer != permission.requester or route.caller_fleet_uid is not None
                    or release_id is not None and route.release_id != release_id):
                raise ValueError("permission notification route changed")
            if admit_notification is not None:
                admit_notification()
            child, body = _notification(request_id, permission, decision)
            deliver_bound_message(route, body=body, request_id=child, kind="notice",
                                  caller_context=ctx, require_durable_request=True)
        except Exception:
            pass  # The recorded decision survives an unavailable bot or carrier.
    return result, inspect_permission_notification(ctx, request_id, result.permission, decision)


def dispatch(args):
    from ..activation_identity import read_selected_identity_bindings
    from ..activation_state import ActivationError, read_selection
    from ..config_plan import PlanError
    from ..operation_context import (
        OperationContextError, resolve_operation_scope, resolve_task_mutation_context,
    )
    from ..plane.db import connect_ro, db_file
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError, require_current_schema
    from ..releases import ReleaseError
    from ..request_receipts import ReceiptError
    from ..runtime_admission import ReleaseMismatch, RuntimeIdentity, mutation_admission
    from ..task_operations import TaskRecordingError
    from ..task_permissions import read_permission, request_permission, require_permission
    from ..task_queries import TaskQueryError
    from ..task_state import TaskStateError
    from .host_owner import _GENERATED, _approve, _terminal
    from .task_write import _request_id

    release_id = None
    verb = args.permission_action
    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no task permissions")
        if verb not in {"show", "check"}:
            _request_id(args.request_id)
        scope = _scope(args.scope_file) if verb in {"request", "check"} else None
        selected, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        root = selected.paths.root
        if verb == "show":
            selection = read_selection(root)
            if selection is None:
                raise CommandFailure("conflict", "no active host selection")
            release_id = selection["release_id"]
            bindings = read_selected_identity_bindings(root, selected.fleet.name, package=selected.paths.package)
            with closing(connect_ro(db_file(root))) as conn:
                conn.execute("BEGIN")
                require_current_schema(conn)
                permission = read_permission(conn, args.permission_id,
                    root=root, host_uid=bindings["host_uid"], fleet_uid=bindings["fleet_uid"])
            if read_selection(root) != selection:
                raise CommandFailure("conflict", "host selection changed during permission read")
            data = permission.as_dict()
        else:
            is_decision = verb in {"approve", "decline", "revoke"}
            if is_decision and (origin is not None or any(key in os.environ for key in (
                    *_GENERATED, "CLAUDLOBBY_RELEASE_ID"))):
                raise CommandFailure("conflict", "permission decisions require a local operator, not a generated bot or timer")
            if not is_decision and (origin is None or origin.bot_id is None):
                raise CommandFailure("conflict", "permission request/check requires a generated bot caller")
            expected = os.environ.get("CLAUDLOBBY_RELEASE_ID") if origin else None
            if origin and not expected:
                raise CommandFailure("release_mismatch", "generated caller lacks a bound release")
            preview = None
            if is_decision:
                # No identity registration or activation lock while the human
                # considers a decision. Re-admit the same release after approval.
                with _terminal(root) as terminal:
                    with mutation_admission(root, identity=RuntimeIdentity.current()) as release:
                        expected = release_id = release.release_id
                        bindings = read_selected_identity_bindings(root, selected.fleet.name,
                                                                   package=selected.paths.package)
                        with closing(connect_ro(db_file(root))) as conn:
                            conn.execute("BEGIN")
                            require_current_schema(conn)
                            preview = read_permission(conn, args.permission_id, root=root,
                                host_uid=bindings["host_uid"], fleet_uid=bindings["fleet_uid"])
                        if preview.scope.digest != args.scope_sha256:
                            raise CommandFailure("conflict", "selected scope differs from the permission request")
                    terminal.write(json.dumps(preview.as_dict(), ensure_ascii=True, indent=2) + "\n")
                    _approve(terminal, verb.upper())
            with mutation_admission(root, identity=RuntimeIdentity.current(), expected_release=expected) as release:
                release_id = release.release_id
                if preview is not None:
                    with closing(connect_ro(db_file(root))) as conn:
                        conn.execute("BEGIN")
                        require_current_schema(conn)
                        current = read_permission(conn, args.permission_id, root=root,
                            host_uid=bindings["host_uid"], fleet_uid=bindings["fleet_uid"])
                    if current != preview:
                        raise CommandFailure("conflict", "permission changed during confirmation; inspect it again")
                ctx = resolve_task_mutation_context(root=root, fleet=selected.fleet.name,
                                                    package=selected.paths.package)
                if verb == "request":
                    result = request_permission(ctx, args.request_id, args.task_id,
                                                scope=scope, reason=args.reason)
                elif verb == "check":
                    with closing(connect_ro(db_file(root))) as conn:
                        conn.execute("BEGIN")
                        require_current_schema(conn)
                        permission = require_permission(ctx, conn, args.permission_id, scope=scope)
                    return CommandOutput({**permission.as_dict(), "allowed": True,
                        "execution": "not_requested"}, release_id=release_id,
                        lines=(f"{permission.permission_id}\tapproved for exact scope; no effect executed",))
                else:
                    result, notification = decide_and_notify_permission(ctx, selected.paths.package,
                        args.request_id, args.permission_id,
                        decision=verb, scope_sha256=args.scope_sha256, reason=args.reason)
                data = {**result.permission.as_dict(), "request_id": result.request_id,
                        "recording": result.recording, "replayed": result.replayed,
                        "execution": result.execution}
                if is_decision:
                    data["notification"] = notification
        return CommandOutput(data, release_id=release_id,
            lines=(json.dumps(data, ensure_ascii=True, sort_keys=True),))
    except CommandFailure:
        raise
    except TaskRecordingError as exc:
        raise CommandFailure("unavailable", str(exc), release_id=release_id,
            data={"recording": exc.recording, "execution": "not_requested",
                  "request_id": exc.request_id}) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", str(exc), release_id=release_id) from exc
    except TaskQueryError as exc:
        raise CommandFailure(exc.code, str(exc), release_id=release_id) from exc
    except (ReceiptError, TaskStateError, ActivationError, PlanError, OperationContextError) as exc:
        raise CommandFailure("conflict", str(exc), release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", str(exc), release_id=release_id) from exc
    except (OSError, sqlite3.Error, PendingMigrationError, DowngradeError) as exc:
        raise CommandFailure("unavailable", "permission storage or active identity is unavailable",
                             release_id=release_id) from exc
