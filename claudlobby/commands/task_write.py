"""Public task mutations over the durable task and message owners."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import os
import re
import sqlite3
from uuid import UUID

from ..command_result import CommandFailure, CommandOutput
from ..task_operations import UNSPECIFIED_ASSIGNMENT


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


def _optional(value: str | None, field: str, pattern: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not re.fullmatch(pattern, value)):
        raise CommandFailure("invalid_argument", f"invalid {field}")
    return value


def _body(path: str | None) -> str | None:
    if path is None:
        return None
    from ..plane.registries import FIELD_POLICY

    file = Path(path).expanduser()
    try:
        if not file.is_file():
            raise OSError("not a regular file")
        cap = FIELD_POLICY[("work_item", "body")]["cap"]
        with file.open("rb") as stream:
            raw = stream.read(cap + 1)
        if len(raw) > cap:
            raise CommandFailure("invalid_argument", "task body exceeds the allowed byte limit")
        if b"\x00" in raw:
            raise CommandFailure("invalid_argument", "task body must be text without null bytes")
        return raw.decode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise CommandFailure("invalid_argument", "task body must be UTF-8 text") from exc
    except OSError as exc:
        raise CommandFailure("invalid_argument", "task body file is unavailable") from exc


def _deadline(value: str | None) -> str | None:
    if value is None or value == "none":  # omitted: fleet default; none: open-ended
        return value
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", value):
        raise CommandFailure("invalid_argument", "--expected-by requires RFC3339 with a timezone")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CommandFailure("invalid_argument", "--expected-by is not a valid instant") from exc
    return value


def _inputs(args) -> dict:
    _request_id(args.request_id)
    if args.public_command == "task.feedback":
        from ..message_payload import MessageBody, MessagePayloadError
        from ..plane.ids import ID_PATTERNS

        try:
            body = MessageBody.from_input(args.text)
        except MessagePayloadError as exc:
            raise CommandFailure("invalid_argument", str(exc)) from exc
        if not isinstance(args.actor, str) or not re.fullmatch(r"human:[^\s:/]+", args.actor):
            raise CommandFailure("invalid_argument", "--actor requires an existing local human: alias")
        expected = args.expected_assignment
        if expected != "none" and (not isinstance(expected, str)
                or not re.fullmatch(ID_PATTERNS["assignment"], expected)):
            raise CommandFailure("invalid_argument", "--expected-assignment requires a canonical assignment or none")
        return {"task_id": _reference(args.task_id, "TASK_ID"), "body": body,
                "expected_assignment_id": None if expected == "none" else expected}
    if args.public_command == "task.admit":
        return {"title": _text(args.title, "--title"), "body": _body(args.body_file),
                "repo": _optional(args.repo, "--repo", r"[^/\s]+/[^/\s]+"),
                "project_key": _optional(args.project, "--project", r"[a-z][a-z0-9-]*"),
                "workstream_id": _optional(args.workstream, "--workstream", r"\S+"),
                "by": _optional(args.by, "--by", r"(?:human:[^\s:/]+|bot:[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)")}
    if args.public_command == "task.assign":
        return {"task_id": _reference(args.task_id, "TASK_ID"),
                "bot_id": _optional(args.bot, "--bot", r"[A-Za-z0-9_-]+"),
                "expected_by": _deadline(args.expected_by),
                "checkin_id": _optional(args.checkin, "--checkin", r"ck_[0-9a-f]{32}"),
                "by": _optional(args.by, "--by", r"(?:human:[^\s:/]+|bot:[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)")}
    if args.public_command == "task.withdraw":
        return {"task_id": _reference(args.task_id, "TASK_ID"),
                "reason": _text(args.reason, "--reason"),
                "by": _optional(args.by, "--by", r"(?:human:[^\s:/]+|bot:[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)")}
    if args.public_command == "task.escalate":
        return {"task_id": _reference(args.task_id, "TASK_ID"),
                "question": _text(args.question, "--question"),
                "by": _optional(args.by, "--by", r"(?:human:[^\s:/]+|bot:[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)")}
    if args.public_command == "task.nudge":
        return {"task_id": _reference(args.task_id, "TASK_ID"),
                "reason": _text(args.reason, "--reason"),
                "by": _optional(args.by, "--by", r"(?:human:[^\s:/]+|bot:[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)")}
    if args.public_command == "task.reassign":
        return {"task_id": _reference(args.task_id, "TASK_ID"),
                "bot_id": _optional(args.bot, "--bot", r"[A-Za-z0-9_-]+"),
                "reason": _text(args.reason, "--reason"),
                "expected_by": _deadline(args.expected_by),
                "by": _optional(args.by, "--by", r"(?:human:[^\s:/]+|bot:[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)")}
    if args.public_command in _REPORTS:
        from ..report_payload import ReportPayload, ReportPayloadError

        verb = args.public_command.split(".", 1)[1]
        field = "summary" if verb in ("progress", "complete") else "reason"
        try:
            report = ReportPayload(_REPORTS[args.public_command],
                                   **{field: _text(getattr(args, field), "--" + field)},
                                   percent=args.percent, pr_url=args.pr, pr_role=args.pr_role,
                                   artifacts=tuple(args.artifact), issues=tuple(args.issue),
                                   skill=args.skill)
        except ReportPayloadError as exc:
            raise CommandFailure("invalid_argument", "invalid linked report fields") from exc
        return {"assignment_id": _reference(args.assignment_id, "ASSIGNMENT_ID"),
                "report": report}
    return {"assignment_id": _reference(args.assignment_id, "ASSIGNMENT_ID")}


_REPORTS = {"assignment.progress": "progress", "assignment.block": "blocked",
            "assignment.return": "blocked", "assignment.complete": "completed",
            "assignment.fail": "failed"}


def _report_envelope(result, route, report) -> str:
    # The typed report's JSON escaping keeps authored text inside one data
    # value; the task/assignment/message header is generated from frozen IDs.
    return ("[Claudlobby linked report]\n"
            f"Message: {result.message_id}\nFrom: {route.caller.alias}\n"
            f"To: {route.peer.alias}\nTask: {result.task_id}\n"
            f"Assignment: {result.assignment_id}\nReport: {report.to_body()}")


def _nudge_envelope(result, route, by, reason):
    from ..task_operations import nudge_body

    return ("[Claudlobby task nudge]\n"
            f"Message: {result.message_id}\nFrom: {route.caller.alias}\n"
            f"To: {route.peer.alias}\nTask: {result.task_id}\n"
            f"Assignment: {result.assignment_id or '-'}\n"
            "Nudge: " + nudge_body(result.task_id, result.assignment_id, by, reason))


def _committed_notification(ctx, route, package, result, envelope, *, send_on_replay=True,
                            allow_enter_repair=True):
    from ..message_operations import (RenderedNativeEnvelope, read_recipient_box,
                                      repair_held_delivery, send_committed_native_attempt)
    from ..message_queries import receipt as observe_receipt
    from ..request_receipts import locked_request

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


def _task_output(result, *, fleet_name, release_id, notification_data=None):
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
    lines = (f"{result.task_id}\t{result.assignment_id or '-'}\t{result.task.state}\t"
             f"recording={recording}\tdelivery={result.delivery}\t"
             f"notification={data['notification']}",)
    return CommandOutput(data, release_id=release_id, lines=lines)


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
    from ..task_operations import TaskRecordingError, nudge

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
    return _task_output(result, fleet_name=ctx.context.fleet.name,
                        release_id=route.release_id, notification_data=notification)


def feedback_bound_task(ctx, route, package, *, request_id, task_id, body,
                        expected_assignment_id, admit_read=None):
    """One linked comment and strict first notification; never a repair/retry."""
    import json
    from ..task_operations import TaskRecordingError, feedback

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
    # The quoted body is comment content, not a control instruction or a reply parent.
    envelope = ("[Claudlobby task feedback]\n"
                f"Message: {result.message_id}\nFrom: {route.caller.alias}\nTo: {route.peer.alias}\n"
                f"Task: {result.task_id}\nAssignment: {result.assignment_id or '-'}\n"
                "Comment: " + json.dumps(body.text, ensure_ascii=True))
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
    return CommandOutput(data, release_id=route.release_id,
        lines=(f"{result.task_id}\t{result.message_id}\trecording=committed\tnotification=received",))


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..message_context import MessageContextError, resolve_message_route
    from ..operation_context import (OperationContextError, OperationContextUnavailableError,
                                     bind_task_context, resolve_operation_scope, resolve_task_mutation_context)
    from ..paths import InvalidPathSelector
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError
    from ..releases import ReleaseError
    from ..request_receipts import ReceiptBusy, ReceiptConflict, ReceiptError
    from ..runtime_admission import ReleaseMismatch, RuntimeIdentity, mutation_admission
    from ..task_operations import (TaskRecordingError, accept, admit, assign, block,
                                   complete, escalate, fail, progress, reassign,
                                   return_assignment, withdraw)
    from ..task_queries import TaskQueryError
    from ..task_state import TaskStateError

    release_id = None
    fleet_name = None
    notification_data = None
    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no task mutations")
        values = _inputs(args)
        selected, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if args.public_command == "task.feedback" and origin is not None:
            raise CommandFailure("conflict", "task feedback requires an explicit local human caller")
        fleet_name = selected.fleet.name
        if selected.paths.seed:
            raise CommandFailure("conflict", "seed configuration has no task mutations")
        bound_release = None
        if origin is not None:
            bound_release = os.environ.get("CLAUDLOBBY_RELEASE_ID")
            if not bound_release or not re.fullmatch(r"r-[0-9a-f]{64}", bound_release):
                raise CommandFailure("release_mismatch", "generated caller lacks a bound release")
        root = selected.paths.root
        with mutation_admission(root, identity=RuntimeIdentity.current(),
                                expected_release=bound_release) as release:
            release_id = release.release_id
            ctx = (bind_task_context(selected, operator_alias=args.actor)
                   if args.public_command == "task.feedback" else
                   resolve_task_mutation_context(root=root, fleet=selected.fleet.name,
                                                 package=selected.paths.package))
            if args.public_command in _REPORTS or args.public_command in {"task.nudge", "task.feedback"}:
                if args.public_command in _REPORTS and (origin is None or origin.bot_id is None):
                    raise CommandFailure("conflict", "linked assignment reports require a generated bot caller",
                                         release_id=release_id)
                route = resolve_message_route(selected.fleet.manager, root=root,
                                              fleet=selected.fleet.name,
                                              package=selected.paths.package,
                                              caller_context=ctx if origin is None else None)
                if (route.release_id != release_id or route.host_uid != ctx.host_uid
                        or route.selected_fleet_uid != ctx.fleet_uid
                        or route.caller_fleet_uid != ctx.caller_fleet_uid
                        or route.caller != ctx.caller
                        or route.peer != ctx.bots[selected.fleet.manager]
                        or route.manager != route.peer):
                    raise CommandFailure("conflict", "manager route differs from active task identities",
                                         release_id=release_id)
            if args.public_command in _REPORTS:
                reporters = {"assignment.progress": progress, "assignment.block": block,
                             "assignment.return": return_assignment,
                             "assignment.complete": complete, "assignment.fail": fail}
                result = reporters[args.public_command](ctx, args.request_id, values["assignment_id"],
                                                        values["report"], route=route.receipt_binding())
                notification_data = _committed_notification(
                    ctx, route, selected.paths.package, result,
                    _report_envelope(result, route, values["report"]))
            elif args.public_command == "task.feedback":
                return feedback_bound_task(ctx, route, selected.paths.package,
                                           request_id=args.request_id, **values)
            elif args.public_command == "task.nudge":
                return nudge_bound_task(ctx, route, selected.paths.package,
                    request_id=args.request_id, task_id=values["task_id"],
                    reason=values["reason"], by=values["by"])
            elif args.public_command == "task.admit":
                result = admit(ctx, args.request_id, **values)
            elif args.public_command == "task.assign":
                result = assign(ctx, args.request_id, values.pop("task_id"), **values)
            elif args.public_command == "task.withdraw":
                result = withdraw(ctx, args.request_id, values.pop("task_id"), **values)
            elif args.public_command == "task.escalate":
                result = escalate(ctx, args.request_id, values.pop("task_id"), **values)
            elif args.public_command == "task.reassign":
                result = reassign(ctx, args.request_id, values.pop("task_id"), **values)
            else:
                result = accept(ctx, args.request_id, values["assignment_id"])
        return _task_output(result, fleet_name=fleet_name, release_id=release_id,
                            notification_data=notification_data)
    except CommandFailure:
        raise
    except TaskRecordingError as exc:
        # nudge_bound_task translates its own recording failure.
        raise _recording_failure(exc, fleet_name=fleet_name, release_id=release_id,
                                 notification=args.public_command in _REPORTS) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "selected release differs from this caller",
                             hint=exc.hint) from exc
    except (OperationContextUnavailableError, PendingMigrationError, DowngradeError,
            sqlite3.Error, OSError) as exc:
        raise CommandFailure("unavailable", "task identity or storage is unavailable",
                             retryable=args.public_command != "task.feedback", release_id=release_id) from exc
    except (ReceiptConflict, ReceiptBusy) as exc:
        raise CommandFailure("conflict", "task request conflicts with recorded history or another caller",
                             release_id=release_id) from exc
    except ReceiptError as exc:
        raise CommandFailure("invalid_argument", "task request receipt is invalid",
                             release_id=release_id) from exc
    except TaskQueryError as exc:
        raise CommandFailure(exc.code, str(exc), hint=exc.hint,
                             retryable=exc.retryable and args.public_command != "task.feedback",
                             release_id=release_id) from exc
    except (ActivationError, PlanError, OperationContextError, MessageContextError, BotNotFoundError,
            TaskStateError) as exc:
        raise CommandFailure("conflict", "active task scope or state is incomplete",
                             hint=f"{exc}; inspect claudlobby host releases" if isinstance(exc, ActivationError) else None,
                             release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched") from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid task root or fleet selector") from exc
