"""Admitted message orchestration shared by CLI and owner HTTP adapters.

Authentication/runtime admission stay with callers; canonical effects and receipt
proof stay with the existing message owners. No CLI parsing or success rendering.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import sqlite3
import sys

from .application_result import ApplicationOutcome
from .command_result import CommandFailure

_RECEIPT_WAIT_S = 10


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
    from .env_tiers import resolve
    from .recording_alerts import _TIER_KEYS
    try:
        selected = resolve(route.selected.paths, bot_name=route.selected.fleet.manager,
                           fleet_name=route.selected.fleet.name)
    except (OSError, ValueError, RuntimeError):
        return {}, False
    return {key: selected[key].value for key in _TIER_KEYS if key in selected}, True


def deliver_bound_message(route, *, body=None, report=None, request_id,
                          kind="chat", parent_message_id=None,
                          retry_uncertain=False, caller_context=None,
                          require_durable_request=False) -> ApplicationOutcome:
    """Send and observe an authorized, frozen route under the caller's runtime admission.

    The caller must already hold runtime mutation admission and authorize the
    route and any reply parent. This helper does not authenticate a browser or
    authorize an actor; a browser gateway must do that before calling it.
    ``caller_context`` is a prebound TaskOperationContext for a human caller;
    generated callers are bound from the route's origin for receipt lookup.
    Owner sends require a durable request and native reservation before effects;
    ordinary CLI callers retain the default best-effort recording behavior.
    """
    from .message_operations import (read_recipient_box, repair_held_delivery,
                                      send_message, send_unlinked_report)
    from .message_queries import MessageQueryError, receipt
    from .operation_context import (OperationContextError, OperationContextUnavailableError,
                                     bind_task_context)
    from .plane.migrations import DowngradeError
    from .plane.schema_state import PendingMigrationError

    package = route.selected.paths.package
    release_id = route.release_id
    if ((caller_context is None) != (route.origin is not None)
            or caller_context is not None and caller_context.caller != route.caller):
        raise CommandFailure("conflict", "message caller context differs from the frozen route",
                             release_id=release_id)
    if report is not None and require_durable_request:
        raise CommandFailure("conflict", "durable message admission does not accept a report",
                             release_id=release_id)

    trusted_tiers, tiers_available = _alert_tiers(route)
    # The box just before the send, for the chip repair (#2105).
    box_before = read_recipient_box(route, package)
    if report is not None:
        outcome = send_unlinked_report(route, package, report,
                                       request_id=request_id,
                                       retry_uncertain=retry_uncertain,
                                       trusted_tiers=trusted_tiers)
    else:
        outcome = send_message(route, package, body,
                               request_id=request_id,
                               kind="answer" if parent_message_id is not None else kind,
                               parent_message_id=parent_message_id,
                               retry_uncertain=retry_uncertain,
                               require_durable_request=require_durable_request,
                               trusted_tiers=trusted_tiers)
    data = _data(route, outcome, parent_message_id=parent_message_id)
    if report is not None:
        data["report_status"] = report.status
        data["task_id"] = data["assignment_id"] = None
    if outcome.code == "recording_degraded":
        if not tiers_available and (data["alert"] is None
                                    or data["alert"]["telegram"]["status"] == "unconfigured"):
            if data["alert"] is not None:
                from .recording_alerts import ChannelOutcome
                availability = data["alert"]["telegram"]["debounce_available"]
                data["alert"]["telegram"] = asdict(
                    ChannelOutcome("failed", availability, "selected_tiers_unavailable"))
            print(f"recording-alert: fleet={route.selected.fleet.name} request={request_id} "
                  "channel=telegram status=failed reason=selected_tiers_unavailable",
                  file=sys.stderr)
        raise _effect_failure("recording_degraded",
                             "message recording is degraded; inspect the request before retrying",
                             data=data, request_id=request_id, release_id=release_id)
    # rc 3 (#1236): the box never showed the payload, so the transport
    # withheld its Enter. The text can still land; the receipt wait and
    # the held-box repair below decide (#2105).
    withheld = outcome.delivery == "unknown" and getattr(outcome, "native_returncode", None) == 3
    if outcome.delivery != "submitted" and not withheld:
        code = "delivery_failed" if outcome.delivery == "failed" else "delivery_unknown"
        raise _effect_failure(code, "message transport was not confirmed; inspect the request",
                             data=data, request_id=request_id, release_id=release_id)
    # A tmux success is only submission. This read owns the final byte
    # integrity verdict. It never resends the native payload; when no
    # receipt came, the owner may press the Enter a held box waits for.
    try:
        ctx = (caller_context if caller_context is not None else
               bind_task_context(route.selected, origin=route.origin))
        observed = receipt(ctx, outcome.message_id, destination=route.peer.alias,
                           wait=_RECEIPT_WAIT_S)
        # A strict owner replay only inspects retained evidence. Even an Enter
        # repair is a native effect and must not follow a repeated request UUID.
        if not (require_durable_request and outcome.replayed):
            repair, observed = repair_held_delivery(
                route, package, outcome.message_id, first=observed,
                box_before=box_before,
                observe=lambda wait: receipt(ctx, outcome.message_id,
                                             destination=route.peer.alias, wait=wait))
            if repair is not None:
                data["enter_repair"] = repair.as_dict()
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
                             f"message was submitted; {observed.reason}"
                             if observed.code == "unavailable" and observed.reason else
                             "message was submitted; final receiver proof is incomplete",
                             data=data, request_id=request_id, release_id=release_id)
    return ApplicationOutcome(data, release_id=release_id)
