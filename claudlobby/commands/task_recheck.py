"""Canonical, fleet-scoped Task recheck and its private scheduled entrypoint."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import math
import os
import re
import sqlite3
from uuid import UUID, uuid4

from ..command_result import CommandFailure, CommandOutput, execute


def _validate(args) -> None:
    try:
        if str(UUID(args.request_id)) != args.request_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise CommandFailure("invalid_argument", "--request-id requires a canonical UUID") from exc
    for name in ("max_age_h", "repeat_h"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0:
            raise CommandFailure("invalid_argument", f"--{name.replace('_', '-')} must be finite and nonnegative")
    if args.by is not None and not re.fullmatch(
            r"(?:human:[^\s:/]+|bot:[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)", args.by):
        raise CommandFailure("invalid_argument", "invalid --by provenance")


def _dispatch(args) -> CommandOutput:
    from ..activation_identity import read_selected_identity_bindings
    from ..activation_state import read_selection
    from ..message_context import resolve_message_route
    from ..operation_context import resolve_operation_scope, resolve_task_mutation_context
    from ..plane.db import connect_ro, db_file
    from ..plane.schema_state import require_current_schema
    from ..runtime_admission import RuntimeIdentity, mutation_admission
    from ..task_recheck import digest, recheck, select

    _validate(args)
    if args.seed:
        raise CommandFailure("conflict", "seed configuration has no task recheck")
    selected, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
    if selected.paths.seed:
        raise CommandFailure("conflict", "seed configuration has no task recheck")
    root = selected.paths.root
    stamp = read_selection(root)
    if stamp is None:
        raise CommandFailure("conflict", "active task selection is unavailable")
    release_id = stamp["release_id"]
    if args.dry_run:
        # A preview cannot call the mutation binder: a cold local human would
        # register an identity before the dry-run returned.
        bindings = read_selected_identity_bindings(root, selected.fleet.name,
                                                   package=selected.paths.package)
        if bindings["manager"] != selected.fleet.manager or set(bindings["bots"]) != set(selected.fleet.bots):
            raise CommandFailure("conflict", "active task bindings differ from the selected fleet")
        with closing(connect_ro(db_file(root))) as conn:
            conn.execute("BEGIN")
            require_current_schema(conn)
            fleet_row = conn.execute("SELECT uid FROM identity_registry WHERE kind='fleet' AND alias=?",
                                     (selected.fleet.name,)).fetchone()
            manager_row = conn.execute("SELECT uid FROM identity_registry WHERE kind='actor' AND alias=?",
                                       (f"bot:{selected.fleet.name}/{selected.fleet.manager}",)).fetchone()
            if (fleet_row is None or fleet_row[0] != bindings["fleet_uid"]
                    or manager_row is None or manager_row[0] != bindings["bots"][selected.fleet.manager]):
                raise CommandFailure("conflict", "frozen fleet identities differ from Plane")
            selection = select(conn, root=root, fleet_uid=bindings["fleet_uid"],
                               now=datetime.now(timezone.utc), max_age_h=args.max_age_h,
                               repeat_h=args.repeat_h)
        if read_selection(root) != stamp:
            raise CommandFailure("conflict", "active task selection changed during preview")
        body = (digest(selection, fleet=selected.fleet.name, manager=selected.fleet.manager,
                       max_age_h=args.max_age_h, by=args.by or "current caller")
                if selection.rows else None)
        return CommandOutput({"fleet": selected.fleet.name, "manager": selected.fleet.manager,
                              "dry_run": True, "task_ids": [t.task_id for t in selection.rows],
                              "held": selection.held, "uncertain": selection.uncertain,
                              "uncertain_request_ids": list(selection.uncertain_request_ids),
                              "waiting": selection.waiting, "overflow": selection.overflow,
                              "issues": [asdict(issue) for issue in selection.issues],
                              "digest": body}, release_id=release_id,
                             lines=(body or "recheck: no task is due",))

    bound_release = os.environ.get("CLAUDLOBBY_RELEASE_ID") if origin is not None else None
    if origin is not None and not bound_release:
        raise CommandFailure("release_mismatch", "generated caller lacks a bound release")
    with mutation_admission(root, identity=RuntimeIdentity.current(),
                            expected_release=bound_release) as release:
        release_id = release.release_id
        ctx = resolve_task_mutation_context(root=root, fleet=selected.fleet.name,
                                            package=selected.paths.package)
        route = resolve_message_route(selected.fleet.manager, root=root,
                                      fleet=selected.fleet.name, package=selected.paths.package,
                                      caller_context=ctx if origin is None else None)
        if (route.release_id != release_id or route.host_uid != ctx.host_uid
                or route.selected_fleet_uid != ctx.fleet_uid
                or route.caller != ctx.caller or route.caller_fleet_uid != ctx.caller_fleet_uid
                or route.peer != ctx.bots[selected.fleet.manager] or route.manager != route.peer):
            raise CommandFailure("conflict", "manager route differs from frozen task identities",
                                 release_id=release_id)
        result = recheck(ctx, args.request_id, route=route.receipt_binding(),
                         max_age_h=args.max_age_h, repeat_h=args.repeat_h, by=args.by)
        notification = None
        if result.message_id is not None:
            from .task_write import _committed_notification
            notification = _committed_notification(ctx, route, selected.paths.package, result,
                                                   result.body or "", send_on_replay=False)
    data = {"fleet": selected.fleet.name, "manager": selected.fleet.manager,
            "task_ids": list(result.task_ids),
            "bookkeeping_asks": len(result.task_ids),
            "bookkeeping_delivery": "no_individual_proof" if result.message_id else "not_sent",
            "held": None if result.replayed else result.selection.held,
            "uncertain": None if result.replayed else result.selection.uncertain,
            "uncertain_request_ids": list(result.selection.uncertain_request_ids),
            "waiting": None if result.replayed else result.selection.waiting,
            "overflow": None if result.replayed else result.selection.overflow,
            "issues": [asdict(issue) for issue in result.selection.issues],
            "replayed": result.replayed, "recording": "committed",
            "message_id": result.message_id, "recipient_uid": result.recipient_uid,
            "request_persisted": True,
            "notification": "pending" if result.message_id else "not_requested"}
    if notification is not None:
        data.update(notification)
        if notification["notification"] != "received" or notification["request_persisted"] is not True:
            raise CommandFailure("notification_failed",
                                 "recheck asks committed; manager digest is unverified; inspect request and receipt",
                                 data=data, release_id=release_id)
    if result.selection.uncertain:
        ids = ", ".join(result.selection.uncertain_request_ids)
        lines = (f"recheck: {result.selection.uncertain} task(s) held for uncertain prior delivery;"
                 f" inspect request {ids}",)
    else:
        lines = (f"recheck: {len(result.task_ids)} task(s) named in one manager digest",)
    return CommandOutput(data, release_id=release_id, lines=lines)


def dispatch(args) -> CommandOutput:
    from ..active_config import ActivationError as ActiveConfigError
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..message_context import MessageContextError
    from ..operation_context import OperationContextError, OperationContextUnavailableError
    from ..paths import InvalidPathSelector
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError
    from ..releases import ReleaseError
    from ..request_receipts import ReceiptBusy, ReceiptConflict, ReceiptError
    from ..runtime_admission import ReleaseMismatch
    from ..task_operations import TaskRecordingError
    from ..task_queries import TaskQueryError
    from ..task_recheck import RecheckUnresolved
    from ..task_state import TaskStateError

    try:
        return _dispatch(args)
    except CommandFailure:
        raise
    except RecheckUnresolved as exc:
        raise CommandFailure("unavailable", str(exc),
                             data={"issues": [asdict(issue) for issue in exc.issues]}) from exc
    except TaskRecordingError as exc:
        raise CommandFailure("unavailable", str(exc),
                             data={"recording": exc.recording,
                                   "request_persisted": exc.request_persisted,
                                   "message_id": exc.message_id}) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "selected release differs from this caller",
                             hint=exc.hint) from exc
    except (OperationContextUnavailableError, PendingMigrationError, DowngradeError,
            sqlite3.Error, OSError) as exc:
        raise CommandFailure("unavailable", "recheck identity or Plane storage is unavailable",
                             retryable=True) from exc
    except (ReceiptBusy, ReceiptConflict) as exc:
        raise CommandFailure("conflict", "recheck request conflicts with recorded history or another sweep") from exc
    except ReceiptError as exc:
        raise CommandFailure("invalid_argument", "recheck request receipt is invalid") from exc
    except TaskQueryError as exc:
        raise CommandFailure(exc.code, str(exc), hint=exc.hint,
                             retryable=exc.retryable) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid recheck root or fleet selector") from exc
    except (ActiveConfigError, ActivationError, PlanError, BotNotFoundError,
            MessageContextError, OperationContextError, TaskStateError, ReleaseError) as exc:
        raise CommandFailure("conflict", "active recheck scope or state is incomplete") from exc


def tick(args) -> int:
    """Generated timer adapter: one fresh request, the same public owner."""
    if os.environ.get("TASK_RECHECK_ENABLED") == "0":
        print("task-recheck: OFF here (TASK_RECHECK_ENABLED=0); no re-check will be sent")
        return 0
    args.fleet = args.tick_fleet
    args.request_id = str(uuid4())
    args.max_age_h = 48.0
    args.repeat_h = 24.0
    args.dry_run = False
    args.by = None
    args.public_command = "task.recheck"
    return execute("task.recheck", lambda: dispatch(args), json_output=False,
                   request_id=args.request_id)
