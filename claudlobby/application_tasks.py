"""Admitted task notification workflows shared by CLI and owner HTTP.

Domain mutation, durable request and native delivery owners are unchanged.
Success results contain data only; the CLI chooses its display lines.
"""
from __future__ import annotations

from uuid import UUID

from .application_result import ApplicationOutcome
from .command_result import CommandFailure
from .task_operations import UNSPECIFIED_ASSIGNMENT


def _reference(value: str, field: str) -> str:
    # The scoped task reader identifies wrong-kind and historical references
    # and returns a concrete canonical remedy; this command never acts on one.
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise CommandFailure("invalid_argument", f"{field} requires a task or assignment reference")
    return value


def _request_id(value: str) -> str:
    try:
        if str(UUID(value)) == value:
            return value
    except (TypeError, ValueError, AttributeError):
        pass
    raise CommandFailure("invalid_argument", "--request-id requires a canonical UUID")


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise CommandFailure("invalid_argument", f"{field} requires nonempty text")
    return value


def _nudge_envelope(result, route, by, reason):
    from .task_operations import nudge_body

    return ("[Claudlobby task nudge]\n"
            f"Message: {result.message_id}\nFrom: {route.caller.alias}\n"
            f"To: {route.peer.alias}\nTask: {result.task_id}\n"
            f"Assignment: {result.assignment_id or '-'}\n"
            "Nudge: " + nudge_body(result.task_id, result.assignment_id, by, reason))


def _committed_notification(ctx, route, package, result, envelope, *, send_on_replay=True,
                            allow_enter_repair=True):
    from .message_operations import (RenderedNativeEnvelope, read_recipient_box,
                                      repair_held_delivery, send_committed_native_attempt)
    from .message_queries import receipt as observe_receipt
    from .request_receipts import locked_request

    data = {"message_id": result.message_id, "recipient_uid": result.recipient_uid,
            "notification": "unknown", "transport": "unknown",
            "transmission_recording": "unknown", "request_persisted": None,
            "receipt_observation": None, "integrity_verdict": None}
    native_returncode = None
    box_before = None
    try:
        # The task owner has returned: its request and task locks are both
        # released. This is the same request lock, never a nested one.
        with locked_request(ctx.root, ctx.fleet_uid, result.request_id) as store:
            frozen = store.load()
            if (frozen is None or frozen.intent.route != route.receipt_binding()
                    or frozen.intent.message_id != result.message_id
                    or frozen.intent.task_id != result.task_id
                    or frozen.intent.assignment_id != result.assignment_id
                    or frozen.intent.recipient_uid != route.peer.uid):
                raise ValueError("committed request differs from the frozen native route")
            if result.replayed and not send_on_replay:
                # Nudge has no uncertain-retry flag. A replay inspects any
                # retained attempt; it never turns a crash gap into a send.
                prior = frozen.message_attempts[-1] if frozen.message_attempts else None
                if prior is None:
                    data.update(transport="not_attempted", notification="pending",
                                transmission_recording="not_attempted", request_persisted=True)
                    return data
                observation = prior.observation
                data.update(transport=observation.status if observation else "unknown",
                            notification=observation.status if observation else "unknown",
                            transmission_recording=prior.recording_status,
                            request_persisted=True)
            else:
                # The box just before the send, for the chip repair (#2105).
                if allow_enter_repair:
                    box_before = read_recipient_box(route, package)
                attempt = send_committed_native_attempt(
                    route, package, store, frozen,
                    RenderedNativeEnvelope(result.message_id, envelope),
                    request_id=result.request_id)
                data.update(transport=attempt.delivery,
                            notification=attempt.delivery,
                            transmission_recording=attempt.transmission_recording,
                            request_persisted=attempt.request_persisted)
                native_returncode = (attempt.observation.native_returncode
                                     if attempt.observation else None)
        # A fresh submission, or rc 3 (#1236: the transport withheld its Enter
        # because the box never showed the payload), waits for its receipt; a
        # held box then gets the owner's Enter repair (#2105).
        waits = ((data["transport"] == "submitted"
                  or (data["transport"] == "unknown" and native_returncode == 3))
                 and (not result.replayed or send_on_replay))
        observed = observe_receipt(ctx, result.message_id, destination=route.peer.alias,
                                   wait=10 if waits else 0)
        if waits and allow_enter_repair:
            repair, observed = repair_held_delivery(
                route, package, result.message_id, first=observed, box_before=box_before,
                observe=lambda wait: observe_receipt(ctx, result.message_id,
                                                     destination=route.peer.alias, wait=wait))
            if repair is not None:
                data["enter_repair"] = repair.as_dict()
        data.update(receipt_observation=observed.receipt_observation,
                    integrity_verdict=observed.integrity_verdict)
        if (observed.exit_code == 0 and observed.receipt_observation == "received"
                and observed.integrity_verdict == "delivered"
                and observed.sender is not None and observed.sender.uid == route.caller.uid
                and observed.sender.alias == route.caller.alias
                and observed.destination is not None and observed.destination.uid == route.peer.uid
                and observed.destination.alias == route.peer.alias):
            data["notification"] = "received"
        elif observed.integrity_verdict in ("truncated", "altered"):
            data["notification"] = "failed"
    except Exception:
        # An error after the atomic task commit cannot erase its result. A
        # reservation or native send may already have happened; never retry it.
        data["notification"] = "unknown"
    return data


def _recording_failure(exc, *, fleet_name, release_id, notification=False):
    data = {"fleet": fleet_name,
            "outcome": "committed" if exc.recording == "committed" else "unknown",
            "recording": exc.recording, "task_id": exc.task_id,
            "assignment_id": exc.assignment_id, "message_id": exc.message_id,
            "recipient_uid": exc.recipient_uid,
            "request_persisted": exc.request_persisted,
            "delivery": "not_requested", "replayed": False if exc.recording == "committed" else None}
    if notification and exc.recording == "committed":
        data.update(notification="pending", transport="not_attempted",
                    receipt_observation=None, integrity_verdict=None)
        return CommandFailure("notification_failed",
                             "task recording committed; request outcome was not retained reliably; "
                             "notification was not attempted; inspect the request",
                             data=data, release_id=release_id)
    return CommandFailure("unavailable", "task recording is unconfirmed; inspect the request before retrying"
                         if exc.recording != "committed" else
                         "task committed but request outcome was not retained reliably; inspect the request",
                         data=data, release_id=release_id)


def nudge_bound_task(ctx, route, package, *, request_id, task_id, reason, by=None,
                     expected_assignment_id=UNSPECIFIED_ASSIGNMENT, admit_read=None):
    """Canonical nudge workflow for an admitted caller and frozen manager route.

    Caller owns runtime and authorization admission. Replay only inspects the
    original notification; it never repairs or fills a gap with a native send.
    """
    from .task_operations import TaskRecordingError, nudge

    _request_id(request_id)
    _reference(task_id, "TASK_ID")
    _text(reason, "reason")
    if (route.host_uid != ctx.host_uid or route.selected_fleet_uid != ctx.fleet_uid
            or route.peer_fleet_uid != ctx.fleet_uid or route.caller != ctx.caller
            or route.caller_fleet_uid != ctx.caller_fleet_uid
            or route.peer != ctx.bots[ctx.context.fleet.manager] or route.manager != route.peer
            or route.selected.paths.root != ctx.root or package != route.selected.paths.package):
        raise CommandFailure("conflict", "manager route differs from active task identities",
                             release_id=route.release_id)
    try:
        result = nudge(ctx, request_id, task_id, reason=reason, by=by,
                       route=route.receipt_binding(), expected_assignment_id=expected_assignment_id,
                       admit_read=admit_read)
    except TaskRecordingError as exc:
        raise _recording_failure(exc, fleet_name=ctx.context.fleet.name,
                                release_id=route.release_id, notification=True) from exc
    notification = _committed_notification(ctx, route, package, result,
        _nudge_envelope(result, route, by or ctx.caller.alias, reason), send_on_replay=False)
    return task_outcome(result, fleet_name=ctx.context.fleet.name,
                        release_id=route.release_id, notification_data=notification)


def feedback_bound_task(ctx, route, package, *, request_id, task_id, body,
                        expected_assignment_id, admit_read=None):
    """One linked comment and strict first notification; never a repair/retry."""
    import json
    from .task_operations import TaskRecordingError, feedback

    _request_id(request_id)
    if (route.host_uid != ctx.host_uid or route.selected_fleet_uid != ctx.fleet_uid
            or route.peer_fleet_uid != ctx.fleet_uid or route.caller != ctx.caller
            or route.caller_fleet_uid is not None or ctx.caller_fleet_uid is not None
            or route.peer != ctx.bots[ctx.context.fleet.manager] or route.manager != route.peer
            or route.selected.paths.root != ctx.root or package != route.selected.paths.package):
        raise CommandFailure("conflict", "feedback route differs from active task identities",
                             release_id=route.release_id)
    try:
        result = feedback(ctx, request_id, task_id, body=body,
            expected_assignment_id=expected_assignment_id, route=route.receipt_binding(), admit_read=admit_read)
    except TaskRecordingError as exc:
        committed = exc.recording == "committed"
        data = {"request_id": request_id, "fleet": ctx.context.fleet.name, "task_id": exc.task_id,
                "assignment_id": exc.assignment_id, "message_id": exc.message_id,
                "recipient_uid": exc.recipient_uid, "recording": exc.recording,
                "outcome": "committed" if committed else "unknown",
                "request_persisted": exc.request_persisted, "notification": "not_attempted"}
        raise CommandFailure("notification_failed" if committed else "unavailable",
            "feedback recording " + ("committed" if committed else "is unconfirmed")
            + "; notification was not attempted; inspect the original request",
            data=data, release_id=route.release_id) from exc
    # Native input collapses spaces. Escape them inside the JSON string so
    # decoding the received comment preserves every authored byte.
    comment = json.dumps(body.text, ensure_ascii=True).replace(" ", "\\u0020")
    # The quoted body is comment content, not a control instruction or a reply parent.
    envelope = ("[Claudlobby task feedback]\n"
                f"Message: {result.message_id}\nFrom: {route.caller.alias}\nTo: {route.peer.alias}\n"
                f"Task: {result.task_id}\nAssignment: {result.assignment_id or '-'}\n"
                "Comment: " + comment)
    notification = _committed_notification(ctx, route, package, result, envelope,
                                           send_on_replay=False, allow_enter_repair=False)
    data = {"request_id": request_id, "fleet": ctx.context.fleet.name, "task_id": result.task_id,
            "assignment_id": result.assignment_id, "current_task_state": result.task.state,
            "recording": result.recording, "outcome": "unchanged" if result.replayed else "committed",
            "replayed": result.replayed, **notification}
    if data["notification"] != "received" or data["request_persisted"] is not True:
        raise CommandFailure("notification_failed",
            "feedback recording committed; lead notification is unverified; inspect the original request",
            data=data, release_id=route.release_id)
    return ApplicationOutcome(data, release_id=route.release_id)


def task_outcome(result, *, fleet_name, release_id, notification_data=None):
    outcome = "unchanged" if result.replayed else "committed"
    recording = "unchanged" if result.replayed else result.recording
    data = {"fleet": fleet_name, "task_id": result.task_id,
            "assignment_id": result.assignment_id, "state": result.task.state,
            "outcome": outcome, "recording": recording,
            "delivery": result.delivery, "notification": result.notification,
            "replayed": result.replayed}
    if notification_data is not None:
        data.update(notification_data)
        if data["notification"] != "received" or data["request_persisted"] is not True:
            raise CommandFailure("notification_failed",
                                 "task recording committed; manager notification is unverified; "
                                 "inspect the request and message receipt",
                                 data=data, release_id=release_id)
    return ApplicationOutcome(data, release_id=release_id)
