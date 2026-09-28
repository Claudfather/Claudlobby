"""Public generated-manager assignment delivery over the strict domain owner."""

from __future__ import annotations

import os
import re
import sqlite3
from types import SimpleNamespace

from ..command_result import CommandFailure, CommandOutput


_RECEIPT_WAIT_S = 10


def _data(route, result) -> dict:
    return {"fleet": route.selected.fleet.name, "task_id": result.task_id,
            "assignment_id": result.assignment_id, "message_id": result.message_id,
            "sender": {"uid": route.caller.uid, "alias": route.caller.alias},
            "destination": {"uid": route.peer.uid, "alias": route.peer.alias},
            "recording": result.recording, "transmission_recording": result.transmission_recording,
            "request_persisted": result.request_persisted, "transport": result.delivery,
            "delivery": result.delivery, "replayed": result.replayed,
            "attempt_no": result.attempt_no, "receipt_observation": None,
            "integrity_verdict": None, "reason": result.reason}


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..assignment_delivery import deliver
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..message_context import MessageContextError, resolve_message_route
    from ..message_operations import MessageConflict
    from ..message_queries import MessageQueryError, receipt
    from ..operation_context import (OperationContextError, OperationContextUnavailableError,
                                     bind_task_context, resolve_operation_scope)
    from ..paths import InvalidPathSelector
    from ..plane.contracts import ContractViolation
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError
    from ..releases import ReleaseError
    from ..request_receipts import ReceiptBusy, ReceiptConflict, ReceiptError
    from ..runtime_admission import ReleaseMismatch, RuntimeIdentity, mutation_admission
    from ..task_operations import TaskRecordingError, _reader
    from ..task_queries import TaskQueryError, show_assignment
    from ..task_state import TaskStateError
    from .message_write import _body, _request_id

    release_id = None
    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no assignment delivery")
        request_id = _request_id(args.request_id)
        if not isinstance(args.assignment_id, str) or not args.assignment_id.strip() or "\0" in args.assignment_id:
            raise CommandFailure("invalid_argument", "ASSIGNMENT_ID requires a task assignment reference")
        body = _body(SimpleNamespace(text=None, file=args.file))
        selected, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if selected.paths.seed or origin is None or origin.bot_id != selected.fleet.manager:
            raise CommandFailure("conflict", "assignment delivery requires a generated fleet manager")
        bound_release = os.environ.get("CLAUDLOBBY_RELEASE_ID")
        if not bound_release or not re.fullmatch(r"r-[0-9a-f]{64}", bound_release):
            raise CommandFailure("release_mismatch", "generated manager lacks a bound release")
        with mutation_admission(selected.paths.root, identity=RuntimeIdentity.current(),
                                expected_release=bound_release) as release:
            release_id = release.release_id
            ctx = bind_task_context(selected, origin=origin)
            if (ctx.caller != ctx.bots.get(selected.fleet.manager)
                    or ctx.caller_fleet_uid != ctx.fleet_uid):
                raise CommandFailure("conflict", "selected fleet manager identity is not the caller",
                                     release_id=release_id)
            with _reader(ctx) as conn:
                view = show_assignment(conn, args.assignment_id, fleet_uid=ctx.fleet_uid)
            workers = [name for name, actor in ctx.bots.items()
                       if actor.uid == view.assignment.assignee_uid]
            if len(workers) != 1:
                raise CommandFailure("conflict", "assignment worker is not a unique active bot",
                                     release_id=release_id)
            route = resolve_message_route(workers[0], root=selected.paths.root,
                                          fleet=selected.fleet.name,
                                          package=selected.paths.package)
            if (route.release_id != release_id or route.host_uid != ctx.host_uid
                    or route.selected_fleet_uid != ctx.fleet_uid
                    or route.caller_fleet_uid != ctx.caller_fleet_uid
                    or route.caller != ctx.caller or route.peer != ctx.bots[workers[0]]
                    or route.manager != ctx.caller):
                raise CommandFailure("conflict", "assignment route differs from active task identities",
                                     release_id=release_id)
            result = deliver(ctx, route, selected.paths.package, request_id,
                             args.assignment_id, body, retry_uncertain=args.retry_uncertain)
            data = _data(route, result)
            if result.delivery != "submitted":
                code = "delivery_failed" if result.delivery == "failed" else "delivery_unknown"
                raise CommandFailure(code,
                                     "assignment intent recorded; native delivery was not confirmed; inspect request",
                                     data=data, release_id=release_id)
            try:
                observed = receipt(ctx, result.message_id, destination=route.peer.alias,
                                   wait=_RECEIPT_WAIT_S)
            except (OperationContextUnavailableError, OperationContextError,
                    MessageQueryError, PendingMigrationError, DowngradeError,
                    OSError, sqlite3.Error) as exc:
                data["receipt_observation"] = "unavailable"
                data["integrity_verdict"] = "unknown"
                raise CommandFailure("delivery_unknown",
                                     "assignment was submitted; final receipt proof is unavailable",
                                     data=data, release_id=release_id) from exc
            data["receipt_observation"] = observed.receipt_observation
            data["integrity_verdict"] = observed.integrity_verdict
            if (observed.sender is None or observed.destination is None
                    or observed.sender.uid != route.caller.uid
                    or observed.sender.alias != route.caller.alias
                    or observed.destination.uid != route.peer.uid
                    or observed.destination.alias != route.peer.alias):
                data["integrity_verdict"] = "unknown"
                raise CommandFailure("delivery_unknown",
                                     "assignment was submitted; receipt identities differ from the frozen route",
                                     data=data, release_id=release_id)
            if (observed.exit_code == 0 and observed.receipt_observation == "received"
                    and observed.integrity_verdict == "delivered"):
                data["delivery"] = "received"
            elif observed.integrity_verdict in {"truncated", "altered"}:
                data["delivery"] = "failed"
                raise CommandFailure("delivery_failed",
                                     "assignment receiver proof shows a byte mismatch; inspect request",
                                     data=data, release_id=release_id)
            else:
                raise CommandFailure("delivery_unknown",
                                     "assignment was submitted; final receiver proof is incomplete",
                                     data=data, release_id=release_id)
            if result.request_persisted is not True:
                raise CommandFailure("delivery_unknown",
                                     "assignment received; request history was not retained reliably; inspect request",
                                     data=data, release_id=release_id)
        return CommandOutput(data, release_id=release_id,
                             lines=(f"{result.task_id}\t{result.assignment_id}\t{result.message_id}\t"
                                    "recording=committed\tdelivery=received",))
    except CommandFailure:
        raise
    except TaskRecordingError as exc:
        data = {"fleet": selected.fleet.name if "selected" in locals() else None,
                "task_id": exc.task_id, "assignment_id": exc.assignment_id,
                "message_id": exc.message_id, "recording": exc.recording,
                "request_persisted": exc.request_persisted, "transport": "not_requested",
                "delivery": "not_requested", "receipt_observation": None,
                "integrity_verdict": None}
        code = "delivery_unknown" if exc.recording == "committed" else "unavailable"
        message = ("assignment intent committed; native delivery was not attempted; inspect the request"
                   if exc.recording == "committed" else
                   "assignment recording is unconfirmed; inspect the request")
        raise CommandFailure(code, message, data=data, release_id=release_id) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "selected release differs from this caller",
                             hint=exc.hint, release_id=release_id) from exc
    except (ReceiptConflict, ReceiptBusy, MessageConflict) as exc:
        raise CommandFailure("conflict", "assignment request conflicts with recorded history or route",
                             release_id=release_id) from exc
    except ReceiptError as exc:
        raise CommandFailure("conflict", "assignment request receipt is invalid or unsafe",
                             release_id=release_id) from exc
    except TaskQueryError as exc:
        raise CommandFailure(exc.code, str(exc), hint=exc.hint,
                             retryable=exc.retryable, release_id=release_id) from exc
    except ContractViolation as exc:
        raise CommandFailure("conflict", "assignment capture or Plane contract is invalid",
                             release_id=release_id) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid assignment root or fleet selector",
                             release_id=release_id) from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "assignment identity registry is unavailable",
                             release_id=release_id) from exc
    except (MessageContextError, OperationContextError, BotNotFoundError,
            ActivationError, PlanError, TaskStateError) as exc:
        raise CommandFailure("conflict", "active assignment scope or state is invalid",
                             release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched",
                             release_id=release_id) from exc
    except (PendingMigrationError, DowngradeError, OSError, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "assignment scope or storage is unavailable",
                             release_id=release_id) from exc
