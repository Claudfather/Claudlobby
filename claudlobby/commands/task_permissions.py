"""Public task permission requests and explicit local operator decisions."""

from contextlib import closing
import json
import os
import sqlite3

from ..command_result import CommandFailure, CommandOutput, execute


def register(task_children):
    parser = task_children.add_parser("permission", help="Request and decide exact task permissions")
    children = parser.add_subparsers(dest="permission_action", required=True)
    for verb in ("request", "show", "check", "approve", "decline", "revoke"):
        child = children.add_parser(verb)
        child.add_argument("--json", action="store_true")
        child.add_argument("task_id" if verb == "request" else "permission_id")
        if verb not in {"show", "check"}:
            child.add_argument("--request-id", required=True, metavar="UUID")
            child.add_argument("--reason", required=True, metavar="TEXT")
        if verb in {"request", "check"}:
            child.add_argument("--scope-file", required=True, metavar="JSON")
        if verb in {"approve", "decline", "revoke"}:
            child.add_argument("--scope-sha256", required=True,
                               help="Exact digest displayed by permission show")
        child.set_defaults(func=_dispatch, public_command="task.permission." + verb)


def _dispatch(args):
    return execute(args.public_command, lambda: dispatch(args), json_output=args.json,
                   request_id=getattr(args, "request_id", None))


def _scope(path):
    from ..task_permissions import PermissionScope
    from .checkin import _json_file
    try:
        return PermissionScope.from_dict(_json_file(path, cap=16_384))
    except (CommandFailure, ValueError) as exc:
        raise CommandFailure("invalid_argument", "scope file must contain bounded action, target and constraints JSON") from exc


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
    from ..task_permissions import decide_permission, read_permission, request_permission, require_permission
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
                with _terminal() as terminal:
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
                    result = decide_permission(ctx, args.request_id, args.permission_id,
                        decision=verb, scope_sha256=args.scope_sha256, reason=args.reason)
                data = {**result.permission.as_dict(), "request_id": result.request_id,
                        "recording": result.recording, "replayed": result.replayed,
                        "execution": result.execution}
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
