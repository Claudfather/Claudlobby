"""First public ordinary send; all recording and native effects stay in their owners."""

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


def _data(route, outcome) -> dict:
    alert = asdict(outcome.alert) if is_dataclass(outcome.alert) else None
    return {"fleet": route.selected.fleet.name, "message_id": outcome.message_id,
            "sender": {"uid": route.caller.uid, "alias": route.caller.alias},
            "destination": {"uid": route.peer.uid, "alias": route.peer.alias},
            "recording": outcome.recording, "request_persisted": outcome.request_persisted,
            "transport": outcome.delivery, "delivery": outcome.delivery,
            "receipt_observation": None, "integrity_verdict": None,
            "replayed": outcome.replayed, "alert": alert}


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
    from ..message_operations import MessageConflict, send_message
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

    release_id = None
    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no message mutations")
        request_id = _request_id(args.request_id)
        body = _body(args)
        selected, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if selected.paths.seed or origin is None or origin.bot_id is None:
            raise CommandFailure("conflict", "message send requires a generated bot context")
        bound_release = os.environ.get("CLAUDLOBBY_RELEASE_ID")
        if not bound_release or not re.fullmatch(r"r-[0-9a-f]{64}", bound_release):
            raise CommandFailure("release_mismatch", "generated caller lacks a bound release")
        with mutation_admission(selected.paths.root, identity=RuntimeIdentity.current(),
                                expected_release=bound_release) as release:
            release_id = release.release_id
            route = resolve_message_route(args.to, root=selected.paths.root,
                                          fleet=selected.fleet.name,
                                          package=selected.paths.package)
            if route.release_id != release_id or route.caller.alias != (
                    f"bot:{origin.fleet.name}/{origin.bot_id}"):
                raise CommandFailure("release_mismatch", "message route differs from selected release",
                                     release_id=release_id)
            trusted_tiers, tiers_available = _alert_tiers(route)
            outcome = send_message(route, selected.paths.package, body,
                                   request_id=request_id, kind=args.kind,
                                   retry_uncertain=args.retry_uncertain,
                                   trusted_tiers=trusted_tiers)
            data = _data(route, outcome)
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
                raise CommandFailure("recording_degraded",
                                     "message recording is degraded; inspect the request before retrying",
                                     data=data, release_id=release_id)
            if outcome.delivery != "submitted":
                code = "delivery_failed" if outcome.delivery == "failed" else "delivery_unknown"
                raise CommandFailure(code, "message transport was not confirmed; inspect the request",
                                     data=data, release_id=release_id)
            # A tmux success is only submission. This read owns the final byte
            # integrity verdict and never repairs or resends the native payload.
            try:
                ctx = bind_task_context(route.selected, origin=route.origin)
                observed = receipt(ctx, outcome.message_id, destination=route.peer.alias,
                                   wait=_RECEIPT_WAIT_S)
            except (OperationContextUnavailableError, OperationContextError,
                    MessageQueryError, PendingMigrationError, DowngradeError,
                    OSError, sqlite3.Error) as exc:
                data["receipt_observation"] = "unavailable"
                data["integrity_verdict"] = "unknown"
                raise CommandFailure("delivery_unknown",
                                     "message was submitted; final receipt proof is unavailable",
                                     data=data, release_id=release_id) from exc
            if (observed.sender is None or observed.destination is None
                    or observed.sender.uid != route.caller.uid
                    or observed.sender.alias != route.caller.alias
                    or observed.destination.uid != route.peer.uid
                    or observed.destination.alias != route.peer.alias):
                data["receipt_observation"] = observed.receipt_observation
                data["integrity_verdict"] = "unknown"
                raise CommandFailure("delivery_unknown",
                                     "message was submitted; receipt identities differ from the frozen route",
                                     data=data, release_id=release_id)
            data["receipt_observation"] = observed.receipt_observation
            data["integrity_verdict"] = observed.integrity_verdict
            if observed.integrity_verdict == "delivered":
                data["delivery"] = "received"
            elif observed.integrity_verdict in {"truncated", "altered"}:
                data["delivery"] = "failed"
                raise CommandFailure("delivery_failed",
                                     "message receiver proof shows a byte mismatch; inspect the request",
                                     data=data, release_id=release_id)
            else:
                raise CommandFailure("delivery_unknown",
                                     "message was submitted; final receiver proof is incomplete",
                                     data=data, release_id=release_id)
        return CommandOutput(data, release_id=release_id,
                             lines=(f"{outcome.message_id}\trecording={outcome.recording}\t"
                                    "delivery=received",))
    except CommandFailure:
        raise
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "selected release differs from this caller",
                             hint=exc.hint, release_id=release_id) from exc
    except (ReceiptConflict, ReceiptBusy, MessageConflict) as exc:
        raise CommandFailure("conflict", "message request conflicts with recorded history or another caller",
                             release_id=release_id) from exc
    except ReceiptError as exc:
        raise CommandFailure("conflict", "message request receipt is invalid or unsafe",
                             release_id=release_id) from exc
    except ContractViolation as exc:
        raise CommandFailure("conflict", "message capture or Plane contract is invalid",
                             release_id=release_id) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid message root or fleet selector",
                             release_id=release_id) from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "message identity registry is unavailable",
                             release_id=release_id) from exc
    except (MessageContextError, OperationContextError, BotNotFoundError,
            ActivationError, PlanError) as exc:
        raise CommandFailure("conflict", "active generated message scope or destination is invalid",
                             release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched",
                             release_id=release_id) from exc
    except (PendingMigrationError, DowngradeError, OSError, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "message scope or storage is unavailable",
                             release_id=release_id) from exc
