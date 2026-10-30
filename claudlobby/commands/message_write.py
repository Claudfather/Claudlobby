"""Public communication mutations; recording and native effects stay in their owners."""

from __future__ import annotations

from pathlib import Path
import os
import re
import sqlite3
import stat
from uuid import UUID

from ..command_result import CommandFailure, CommandOutput


def _request_id(value: str) -> str:
    try:
        if str(UUID(value)) == value:
            return value
    except (TypeError, ValueError, AttributeError):
        pass
    raise CommandFailure("invalid_argument", "--request-id requires a canonical UUID")


def _body(args):
    from ..message_payload import MessageBody, MessagePayloadError
    from ..plane.registries import cap_for

    if args.text is not None:
        value = args.text
    else:
        path = Path(args.file).expanduser()
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise CommandFailure("invalid_argument", "message file must be a regular file")
            with os.fdopen(fd, "rb") as stream:
                fd = None
                value = stream.read(cap_for("communication", "body") + 1)
        except OSError as exc:
            raise CommandFailure("invalid_argument", "message file is unavailable") from exc
        finally:
            if fd is not None:
                os.close(fd)
    try:
        return MessageBody.from_input(value)
    except MessagePayloadError as exc:
        raise CommandFailure("invalid_argument", "message body is empty, invalid or over the byte cap") from exc


def _answer_human(selected, origin, parent, body, *, request_id, release_id) -> CommandOutput:
    """A generated bot's answer to its human sender (#2068): recorded on the
    Plane, carried by nothing, read with `message wait --for reply`."""
    from ..message_context import resolve_human_reply_route
    from ..message_operations import record_reply_to_human
    from ..task_operations import TaskActor

    route = resolve_human_reply_route(TaskActor(parent.sender.uid, parent.sender.alias),
                                      root=selected.paths.root, fleet=selected.fleet.name,
                                      package=selected.paths.package)
    if route.release_id != release_id:
        raise CommandFailure("release_mismatch", "message route differs from selected release",
                             release_id=release_id)
    if (route.caller.alias != f"bot:{origin.fleet.name}/{origin.bot_id}"
            or route.caller.uid != parent.destination.uid):
        raise CommandFailure("conflict", "reply route differs from recorded parent participants",
                             release_id=release_id)
    outcome = record_reply_to_human(route, selected.paths.package, body, request_id=request_id,
                                    parent_message_id=parent.message_id)
    data = {"fleet": route.selected.fleet.name, "message_id": outcome.message_id,
            "sender": {"uid": route.caller.uid, "alias": route.caller.alias},
            "destination": {"uid": route.peer.uid, "alias": route.peer.alias},
            "recording": outcome.recording, "request_persisted": outcome.request_persisted,
            "transport": "not_requested", "delivery": "not_requested",
            "receipt_observation": None, "integrity_verdict": None,
            "replayed": outcome.replayed, "alert": None, "reply_to_message_id": parent.message_id}
    if outcome.recording != "committed":
        raise CommandFailure(
            "unavailable", f"message {outcome.message_id}: recording {outcome.recording}. A reply to a "
            "human has no carrier, so it exists only once it is recorded",
            data=data, release_id=release_id, retryable=True,
            hint=f"Retry the same request UUID {request_id}; a retry cannot duplicate the answer.")
    return CommandOutput(data, release_id=release_id,
                         lines=(f"{outcome.message_id}\trecording=committed\tdelivery=not_requested",))


def deliver_bound_message(route, *, body=None, report=None, request_id,
                          kind="chat", parent_message_id=None,
                          retry_uncertain=False, caller_context=None,
                          require_durable_request=False) -> CommandOutput:
    """CLI presentation over the shared admitted message workflow."""
    from ..application_messages import deliver_bound_message as deliver

    result = deliver(route, body=body, report=report, request_id=request_id,
                     kind=kind, parent_message_id=parent_message_id,
                     retry_uncertain=retry_uncertain, caller_context=caller_context,
                     require_durable_request=require_durable_request)
    data = result.data
    return CommandOutput(data, release_id=result.release_id,
                         lines=(f"{data['message_id']}\trecording={data['recording']}\t"
                                "delivery=received",))


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..message_context import MessageContextError, resolve_message_route
    from ..message_operations import MessageConflict, MessageIdentityUnavailable
    from ..message_queries import MessageQueryError, show_message
    from ..operation_context import (OperationContextError, OperationContextUnavailableError,
                                     bind_task_context, resolve_operation_scope,
                                     resolve_task_mutation_context)
    from ..paths import InvalidPathSelector
    from ..plane.contracts import ContractViolation
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError
    from ..releases import ReleaseError
    from ..report_payload import ReportPayload, ReportPayloadError
    from ..request_receipts import ReceiptBusy, ReceiptConflict, ReceiptError
    from ..runtime_admission import ReleaseMismatch, RuntimeIdentity, mutation_admission

    release_id = None
    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no message mutations")
        request_id = _request_id(args.request_id)
        is_report = args.public_command == "fleet.reports.submit"
        body = report = None
        if is_report:
            report = ReportPayload(args.status, summary=args.summary, percent=args.percent,
                                   pr_url=args.pr, pr_role=args.pr_role,
                                   artifacts=tuple(args.artifact), issues=tuple(args.issue), skill=args.skill)
        else:
            body = _body(args)
        selected, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if selected.paths.seed:
            raise CommandFailure("conflict", "seed configuration has no message mutations")
        if is_report and (origin is None or origin.bot_id is None):
            raise CommandFailure("conflict", "unlinked reports require a generated bot caller")
        if is_report and origin.fleet.name != selected.fleet.name:
            raise CommandFailure("conflict", "unlinked reports go to the caller's own fleet manager")
        bound_release = os.environ.get("CLAUDLOBBY_RELEASE_ID") if origin is not None else None
        if origin is not None and (origin.bot_id is None or not bound_release
                                   or not re.fullmatch(r"r-[0-9a-f]{64}", bound_release)):
            raise CommandFailure("release_mismatch", "generated caller lacks a bound release")
        with mutation_admission(selected.paths.root, identity=RuntimeIdentity.current(),
                                expected_release=bound_release) as release:
            release_id = release.release_id
            human_ctx = (resolve_task_mutation_context(root=selected.paths.root,
                         fleet=selected.fleet.name, package=selected.paths.package)
                         if origin is None else None)
            parent_message_id = None
            target = args.to if args.public_command == "message.send" else None
            if is_report:
                target = selected.fleet.manager
            if args.public_command == "message.reply":
                parent_message_id = args.message_id
                parent_ctx = (human_ctx if human_ctx is not None else
                              bind_task_context(selected, origin=origin))
                parent = show_message(parent_ctx, parent_message_id)
                addressed = (parent.destination is not None
                             and parent.destination.uid == parent_ctx.caller.uid
                             and parent.destination.alias == parent_ctx.caller.alias)
                if (addressed and origin is not None and parent.sender.fleet_uid is None
                        and parent.sender.alias.startswith("human:")):
                    return _answer_human(selected, origin, parent, body,
                                         request_id=request_id, release_id=release_id)
                if not addressed or not parent.sender.alias.startswith("bot:") or parent.sender.fleet_uid is None:
                    raise CommandFailure("conflict", "reply requires a recorded bot or human sender and this caller as recipient",
                                         release_id=release_id)
                target = parent.sender.alias.removeprefix("bot:")
            route = resolve_message_route(target, root=selected.paths.root,
                                          fleet=selected.fleet.name,
                                          package=selected.paths.package,
                                          caller_context=human_ctx)
            if (route.release_id != release_id
                    or human_ctx is not None and route.caller != human_ctx.caller
                    or origin is not None and route.caller.alias !=
                    f"bot:{origin.fleet.name}/{origin.bot_id}"):
                raise CommandFailure("release_mismatch", "message route differs from selected release",
                                     release_id=release_id)
            if parent_message_id is not None and (
                    route.caller.uid != parent.destination.uid
                    or route.peer.uid != parent.sender.uid
                    or route.peer.alias != parent.sender.alias
                    or route.peer_fleet_uid != parent.sender.fleet_uid):
                raise CommandFailure("conflict", "reply route differs from recorded parent participants",
                                     release_id=release_id)
            return deliver_bound_message(
                route, body=body, report=report, request_id=request_id,
                kind=args.kind if args.public_command == "message.send" else "chat",
                parent_message_id=parent_message_id,
                retry_uncertain=args.retry_uncertain, caller_context=human_ctx)
    except CommandFailure:
        raise
    except ReportPayloadError as exc:
        raise CommandFailure("invalid_argument", "invalid report fields; inspect command help",
                             release_id=release_id) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "selected release differs from this caller",
                             hint=exc.hint, release_id=release_id) from exc
    except ReceiptBusy as exc:
        raise CommandFailure("conflict", "another invocation holds this request",
                             hint=f"Retry the same request UUID {request_id} after it exits.",
                             retryable=True, release_id=release_id) from exc
    except (ReceiptConflict, MessageConflict) as exc:
        raise CommandFailure("conflict", "message request conflicts with recorded history or another caller",
                             release_id=release_id) from exc
    except ReceiptError as exc:
        raise CommandFailure("conflict", "message request receipt is invalid or unsafe",
                             release_id=release_id) from exc
    except MessageQueryError as exc:
        raise CommandFailure(exc.code, "reply parent is not readable in this caller's scope",
                             release_id=release_id) from exc
    except ContractViolation as exc:
        raise CommandFailure("conflict", "message capture or Plane contract is invalid",
                             release_id=release_id) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid message root or fleet selector",
                             release_id=release_id) from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "message identity registry is unavailable",
                             retryable=True, release_id=release_id) from exc
    except MessageIdentityUnavailable as exc:
        raise CommandFailure("unavailable", "local human identity proof is unavailable",
                             retryable=True, release_id=release_id) from exc
    except (MessageContextError, OperationContextError, BotNotFoundError,
            ActivationError, PlanError) as exc:
        raise CommandFailure("conflict", "active message scope or destination is invalid",
                             release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched",
                             release_id=release_id) from exc
    except (PendingMigrationError, DowngradeError, OSError, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "message scope or storage is unavailable",
                             release_id=release_id) from exc
