"""Public communication mutations; recording and native effects stay in their owners."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
import os
import re
import sqlite3
import stat
import sys
from uuid import UUID

from ..command_result import CommandFailure, CommandOutput


_RECEIPT_WAIT_S = 10


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


def _data(route, outcome, *, parent_message_id=None) -> dict:
    alert = asdict(outcome.alert) if is_dataclass(outcome.alert) else None
    data = {"fleet": route.selected.fleet.name, "message_id": outcome.message_id,
            "sender": {"uid": route.caller.uid, "alias": route.caller.alias},
            "destination": {"uid": route.peer.uid, "alias": route.peer.alias},
            "recording": outcome.recording, "request_persisted": outcome.request_persisted,
            "transport": outcome.delivery, "delivery": outcome.delivery,
            "receipt_observation": None, "integrity_verdict": None,
            "replayed": outcome.replayed, "alert": alert}
    if parent_message_id is not None:
        data["reply_to_message_id"] = parent_message_id
    return data


def _effect_failure(code, message, *, data, request_id, release_id):
    """Keep native-effect evidence visible in human output as well as JSON."""
    detail = (f"message {data.get('message_id') or 'unknown'}: "
              f"recording {data.get('recording', 'unknown')}; "
              f"request persisted {str(data.get('request_persisted', False)).lower()}; "
              f"delivery {data.get('delivery', 'unknown')}")
    if data.get("alert") is not None:
        channels = data["alert"]
        detail += "; fleet alert " + ", ".join(
            f"{name}={channels[name]['status']}" for name in ("manager", "telegram"))
    return CommandFailure(code, f"{detail}. {message}", data=data, release_id=release_id,
                          hint=f"Inspect claudlobby --json request show {request_id}; "
                               "do not automatically resend.")


def _alert_tiers(route):
    # Ask the runtime's existing tier-order owner for the selected fleet.
    # A caller's ambient bot env can belong to another fleet under --fleet.
    from ..env_tiers import resolve
    from ..recording_alerts import _TIER_KEYS
    try:
        selected = resolve(route.selected.paths, bot_name=route.selected.fleet.manager,
                           fleet_name=route.selected.fleet.name)
    except (OSError, ValueError, RuntimeError):
        return {}, False
    return {key: selected[key].value for key in _TIER_KEYS if key in selected}, True


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..message_context import MessageContextError, resolve_message_route
    from ..message_operations import (MessageConflict, MessageIdentityUnavailable,
                                      send_message, send_unlinked_report)
    from ..message_queries import MessageQueryError, receipt, show_message
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
                if (parent.destination is None
                        or parent.destination.uid != parent_ctx.caller.uid
                        or parent.destination.alias != parent_ctx.caller.alias
                        or not parent.sender.alias.startswith("bot:")
                        or parent.sender.fleet_uid is None):
                    raise CommandFailure("conflict", "reply requires a recorded bot sender and this caller as recipient",
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
            trusted_tiers, tiers_available = _alert_tiers(route)
            if is_report:
                outcome = send_unlinked_report(route, selected.paths.package, report,
                                               request_id=request_id,
                                               retry_uncertain=args.retry_uncertain,
                                               trusted_tiers=trusted_tiers)
            else:
                outcome = send_message(route, selected.paths.package, body,
                                       request_id=request_id,
                                       kind="answer" if parent_message_id is not None else args.kind,
                                       parent_message_id=parent_message_id,
                                       retry_uncertain=args.retry_uncertain,
                                       trusted_tiers=trusted_tiers)
            data = _data(route, outcome, parent_message_id=parent_message_id)
            if is_report:
                data["report_status"] = report.status
                data["task_id"] = data["assignment_id"] = None
            if outcome.code == "recording_degraded":
                if not tiers_available:
                    if data["alert"] is not None:
                        from ..recording_alerts import ChannelOutcome
                        availability = data["alert"]["telegram"]["debounce_available"]
                        data["alert"]["telegram"] = asdict(
                            ChannelOutcome("failed", availability, "selected_tiers_unavailable"))
                    print(f"recording-alert: fleet={route.selected.fleet.name} request={request_id} "
                          "channel=telegram status=failed reason=selected_tiers_unavailable",
                          file=sys.stderr)
                raise _effect_failure("recording_degraded",
                                     "message recording is degraded; inspect the request before retrying",
                                     data=data, request_id=request_id, release_id=release_id)
            if outcome.delivery != "submitted":
                code = "delivery_failed" if outcome.delivery == "failed" else "delivery_unknown"
                raise _effect_failure(code, "message transport was not confirmed; inspect the request",
                                     data=data, request_id=request_id, release_id=release_id)
            # A tmux success is only submission. This read owns the final byte
            # integrity verdict and never repairs or resends the native payload.
            try:
                ctx = (human_ctx if human_ctx is not None else
                       bind_task_context(route.selected, origin=route.origin))
                observed = receipt(ctx, outcome.message_id, destination=route.peer.alias,
                                   wait=_RECEIPT_WAIT_S)
            except (OperationContextUnavailableError, OperationContextError,
                    MessageQueryError, PendingMigrationError, DowngradeError,
                    OSError, sqlite3.Error) as exc:
                data["receipt_observation"] = "unavailable"
                data["integrity_verdict"] = "unknown"
                raise _effect_failure("delivery_unknown",
                                     "message was submitted; final receipt proof is unavailable",
                                     data=data, request_id=request_id, release_id=release_id) from exc
            if (observed.sender is None or observed.destination is None
                    or observed.sender.uid != route.caller.uid
                    or observed.sender.alias != route.caller.alias
                    or observed.destination.uid != route.peer.uid
                    or observed.destination.alias != route.peer.alias):
                data["receipt_observation"] = observed.receipt_observation
                data["integrity_verdict"] = "unknown"
                raise _effect_failure("delivery_unknown",
                                     "message was submitted; receipt identities differ from the frozen route",
                                     data=data, request_id=request_id, release_id=release_id)
            data["receipt_observation"] = observed.receipt_observation
            data["integrity_verdict"] = observed.integrity_verdict
            if observed.integrity_verdict == "delivered":
                data["delivery"] = "received"
            elif observed.integrity_verdict in {"truncated", "altered"}:
                data["delivery"] = "failed"
                raise _effect_failure("delivery_failed",
                                     "message receiver proof shows a byte mismatch; inspect the request",
                                     data=data, request_id=request_id, release_id=release_id)
            else:
                raise _effect_failure("delivery_unknown",
                                     "message was submitted; final receiver proof is incomplete",
                                     data=data, request_id=request_id, release_id=release_id)
        return CommandOutput(data, release_id=release_id,
                             lines=(f"{outcome.message_id}\trecording={outcome.recording}\t"
                                    "delivery=received",))
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
