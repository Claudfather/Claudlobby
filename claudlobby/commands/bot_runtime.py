"""Public selected-release bot start, persistent stop and intentional restart."""

from dataclasses import asdict
from pathlib import Path

from ..command_result import CommandFailure, CommandOutput


def lifecycle_notes(result) -> tuple[str, ...]:
    """What a stop or a start owes its caller when its record did not land (#2243)."""
    if result.stop_record_kept:
        return ("The start could not remove this bot's stop record (data/.stopped in its "
                "directory); until it is removed, a loss of the bot's unit reads as a "
                "deliberate stop and pages no one.",)
    if result.state == "stopped" and result.recording == "degraded":
        return ("The stop's local record or its plane row did not land; "
                "run bot stop again to record it.",)
    return ()


_CAUSE_LIMIT = 500


def failure_cause(exc: BaseException) -> str:
    """The failure's own message, as a refusal shows it (#2487).

    That message names the unit or file that stopped a native read. An inventory
    message can carry a native tool's whole stderr, so a refusal keeps only its first
    line, cut to 500 characters, or names the exception's class when it has no message.
    """
    text = next(iter(str(exc).strip().splitlines()), "")
    if len(text) > _CAUSE_LIMIT:
        text = text[:_CAUSE_LIMIT - 3] + "..."
    return text or type(exc).__name__


def drift_cause(exc: BaseException | None, data: dict) -> str | None:
    """Name generated units that differ from the committed configuration (#2954).

    Only a drift refusal (`UnitDriftError`) is named: its text is built from the
    units' paths and fixed state words, so it carries no configuration value. Its
    first line, cut as `failure_cause` cuts it, goes in `data` as `cause`, and every
    unit as `drift`, as `host update` names them (#2945). Any other failure returns
    None, and its text stays out of the refusal (#2487).
    """
    from ..config_units import UnitDriftError

    if not isinstance(exc, UnitDriftError):
        return None
    data["cause"] = failure_cause(exc)
    data["drift"] = [dict(row) for row in exc.rows]
    return data["cause"]


def exiting_hint(command: str) -> str:
    """What to do while a stopped bot's session is still exiting (#2981): wait as
    long as a stop waits for it, then run the command again."""
    from ..bot_operations import _STOP_SETTLE_S

    return (f"a stopped bot's session is still exiting: wait {_STOP_SETTLE_S} s, then run "
            f"{command} again; until the session has gone, the lifecycle doors refuse "
            "that bot before any effect")


def native_state_refusal(subject: str, exc: Exception, data: dict) -> CommandFailure:
    """A native read that failed, refused with the failure's own message (#2487).

    Without that message the caller holds exit 6 and nothing to inspect. The JSON
    result also carries it as `data.cause`. A read refused only because a stopped
    bot's session is still exiting is temporary (#2981): it is retryable, its hint
    names the wait, and `data.exiting` counts each unit's processes.
    """
    from ..supervision_inventory import SessionExiting

    cause = failure_cause(exc)
    message = f"{subject} native state cannot be established: {cause}"
    if isinstance(exc, SessionExiting):
        return CommandFailure("unavailable", message, retryable=True, hint=exiting_hint("the same command"),
                              data={**data, "cause": cause, "exiting": exc.processes})
    return CommandFailure("unavailable", message, data={**data, "cause": cause})


def still_exiting_failure(exc, action: str, bot: str, reason: str | None, data: dict) -> CommandFailure:
    """A stopped bot's session still in its unit's cgroup after the quiet check (#2981).

    A stop that ran made every native step and the unit is gone; only the session
    it started had not exited when the stop's wait ended. The answer says so,
    counts what is left, and names the wait and the command to run again.
    """
    data.update(native_outcome="unknown" if exc.effect_attempted else "unattempted",
                release_id=exc.release_id, target=exc.target, session="exiting",
                remaining_processes=exc.processes)
    if exc.recording is not None:
        data["recording"] = exc.recording
    left = f"{exc.processes} {'process' if exc.processes == 1 else 'processes'} left in its unit's cgroup"
    if action == "stop" and exc.effect_attempted:
        message = (f"bot stop de-enrolled {bot}, and its session was still exiting when the "
                   f"stop's {exc.settle_s} s wait ended: {left}")
        keep = " (with the same --reason: a stop records its reason again)" if reason else ""
        hint = (f"the stop is recorded and needs nothing more; to confirm it, wait {exc.settle_s} s "
                f"and run bot stop {bot} again{keep}: while the session is still exiting, that and "
                "every other lifecycle door refuse this bot before any effect and say so")
    else:
        message = f"{bot}'s stopped session is still exiting: {left}; this is temporary"
        hint = exiting_hint(f"bot {action} {bot}")
    return CommandFailure("unavailable", message, data=data, release_id=exc.release_id,
                          retryable=True, hint=hint)


def _canonical_uuid(value: str) -> bool:
    from uuid import UUID

    try:
        return str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def own_compaction_lines(result) -> tuple[str, ...]:
    """What a bot's `compact` on itself recorded (#1053). It never types."""
    who = f"{result.fleet}/{result.bot}"
    request_id = (result.request or {}).get("request_id")
    line = {
        "requested": f"{who}: compaction requested (request {request_id}); nothing was typed, "
                     "and nothing acts on a request yet.",
        "pending": f"{who}: a compaction request is already pending (request {request_id}); "
                   "nothing new was recorded.",
        "replayed": f"{who}: this request ID was already recorded; nothing new was recorded.",
        "cancelled": f"{who}: compaction request {request_id} cancelled.",
        "none_pending": f"{who}: no compaction request is pending; nothing was cancelled.",
    }[result.outcome]
    if result.recording == "degraded":
        line += " Its plane row did not land; the request file stands."
    return (line,)


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..bot_operations import (BotLifecycleError, HandoffDirectoryRefused, HandoffRequestRefused,
                                  SessionStillExiting, check_handoff, control_bot, handoff_bot,
                                  set_bot_running)
    from ..compact_request import CompactRequestResult
    from ..config_plan import PlanError
    from ..context import resolve_paths
    from ..operation_context import OperationContextError
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError
    from ..supervision_inventory import InventoryError

    if args.seed:
        raise CommandFailure("conflict", "bot lifecycle requires an active host, not seed configuration")
    if not args.bot_id or Path(args.bot_id).name != args.bot_id or args.bot_id in {".", ".."}:
        raise CommandFailure("invalid_argument", "supply one exact bot ID")
    action = args.public_command.removeprefix("bot.")
    ceiling = getattr(args, "ceiling", None)
    if ceiling is not None and ceiling <= 0:
        raise CommandFailure("invalid_argument", "--ceiling must be a positive integer")
    running = action != "stop"
    reason = getattr(args, "reason", None)
    if reason is not None:
        from ..stop_record import StopRecordError, check_reason
        try:
            check_reason(reason)
        except StopRecordError as exc:
            raise CommandFailure("invalid_argument", str(exc)) from exc
    request_id = getattr(args, "request_id", None)
    if request_id is not None and not _canonical_uuid(request_id):
        raise CommandFailure("invalid_argument", "--request-id requires a canonical UUID")
    data = {"fleet": args.fleet, "bot": args.bot_id, "requested": action,
            "native_outcome": "unattempted", "runtime_state": "unknown"}
    try:
        root = resolve_paths(root=args.root).root
        if action == "handoff" and getattr(args, "check", False):
            result = check_handoff(root=root, fleet=args.fleet, bot=args.bot_id)
        elif action == "handoff":
            result = handoff_bot(root=root, fleet=args.fleet, bot=args.bot_id)
        elif action in {"interrupt", "compact"}:
            result = control_bot(root=root, fleet=args.fleet, bot=args.bot_id, control=action,
                                 request_id=request_id, cancel=getattr(args, "cancel", False))
        else:
            result = set_bot_running(root=root, fleet=args.fleet, bot=args.bot_id,
                                     running=running, restart=action == "restart",
                                     ceiling=ceiling, reason=reason,
                                     fresh=getattr(args, "fresh", False))
    except BotLifecycleError as exc:
        if exc.source_revision is not None:
            data.update(source_revision=exc.source_revision, config_fingerprint=exc.config_fingerprint)
        if exc.busy:
            raise CommandFailure("conflict", str(exc), data=data, retryable=True,
                                 hint="retry after the running bot lifecycle operation finishes") from exc
        if isinstance(exc, SessionStillExiting):
            raise still_exiting_failure(exc, action, args.bot_id, reason, data) from exc
        if exc.effect_attempted:
            data["native_outcome"] = "unknown"
            data["release_id"] = exc.release_id
            data["target"] = exc.target
            if action in {"interrupt", "compact"}:
                data["outcome"] = "unknown"
                message = f"bot {action} submission is unverified; inspect its private session"
                hint = "inspect the exact private session before considering another control"
            elif action == "handoff":
                data["handoff"] = "unknown"
                data["reason"] = exc.handoff_reason or "unverified"
                message = str(exc) if exc.handoff_reason else "bot handoff outcome is unverified; inspect its private session"
                if exc.handoff_reason == "pending":
                    # #2924, #2927: a save in the file the readers read resolves the earlier
                    # request: the session's own file when it has one, else .claude/session.md.
                    hint = (f"the earlier request resolves once {args.bot_id}'s session saves a handoff: "
                            f"ask it to save one, then read claudlobby --json bot handoff {args.bot_id} "
                            "--check until it reads saved; do not automatically resend the handoff")
                elif isinstance(exc, HandoffDirectoryRefused):
                    # #2927: --check cannot read saved until the directory is repaired, so "until it
                    # reads saved" would name a wait with no end. It reads a refused directory unknown
                    # (file_unverified), and is itself refused while .claude is redirected, at its read
                    # of the effective configuration (#2935). The session's own directory is refused
                    # only when it is redirected or not this user's.
                    hint = (f"a directory on the way to {args.bot_id}'s session handoff is redirected "
                            "or not this user's, or one above the session's own directory cannot be "
                            "searched or read, and --check cannot read the handoff as saved until it "
                            "is repaired: repair it first, then read claudlobby --json bot handoff "
                            f"{args.bot_id} --check; do not automatically resend the handoff")
                else:
                    hint = (f"the handoff can still land: read claudlobby --json bot handoff {args.bot_id} "
                            "--check until it reads saved; do not automatically resend the handoff")
            else:
                message = "bot lifecycle effect is unverified; inspect native state"
                hint = "inspect the exact bot's native unit and private session before retrying"
            raise CommandFailure("unavailable", message, data=data, release_id=exc.release_id,
                                 hint=hint) from exc
        if isinstance(exc, HandoffRequestRefused):
            # #2935: nothing was recorded or sent, so --check has nothing to read. The answer
            # names the directory and its repair, and the handoff is asked again after it.
            raise CommandFailure(
                "unavailable", str(exc), data=data,
                hint=(f"{exc.repair} ({args.bot_id}'s {exc.directory} directory), then ask for the "
                      f"handoff again: claudlobby --json bot handoff {args.bot_id}. Nothing was sent, "
                      "so --check has no request to read")) from exc
        if exc.unavailable:
            # A refused stop or restart says why (#2748): a restart refusal is often a
            # checkpoint refusal, and the generic line hid which.
            message = (str(exc) if action in {"stop", "restart"}
                       else "bot native or session readiness cannot be established")
            raise CommandFailure("unavailable", message,
                                 data=data,
                                 hint="inspect the exact private session; use bot restart for an intentional bounce") from exc
        raise CommandFailure("conflict", str(exc), data=data) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid bot root or fleet selector", data=data) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected bot release differs from this executable", data=data) from exc
    except (ActivationError, PlanError, OperationContextError) as exc:
        message = "selected bot configuration or activation is incomplete"
        if drift_cause(exc, data):  # a drifted unit stopped the bot's binding (#2954)
            message = f"{message}: {data['cause']}"
        raise CommandFailure("conflict", message, data=data) from exc
    except (InventoryError, OSError) as exc:
        raise native_state_refusal("bot", exc, data) from exc
    if action == "handoff" and getattr(args, "check", False):
        data = {key: value for key, value in asdict(result).items() if value is not None}
        when = "".join((f"; requested {result.requested_at}" if result.requested_at else "",
                        f"; written {result.written_at}" if result.written_at else ""))
        return CommandOutput(data, release_id=result.release_id,
                             lines=(f"{result.fleet}/{result.bot}: handoff {result.handoff} "
                                    f"({result.reason}){when}.",))
    if action == "handoff":
        data = asdict(result)
        data["native_outcome"] = "observed" if result.handoff == "saved" else "skipped"
        lines = (f"{result.fleet}/{result.bot}: handoff saved and verified in the private session."
                 if result.handoff == "saved" else
                 f"{result.fleet}/{result.bot}: handoff skipped ({result.reason}); no new handoff verified.",)
        return CommandOutput(data, release_id=result.release_id, lines=lines)
    if action == "compact" and isinstance(result, CompactRequestResult):
        data = asdict(result)
        data["native_outcome"] = "unattempted"
        return CommandOutput(data, release_id=result.release_id, lines=own_compaction_lines(result))
    if action in {"interrupt", "compact"}:
        data = asdict(result)
        data["native_outcome"] = "submitted"
        return CommandOutput(data, release_id=result.release_id,
                             lines=(f"{result.fleet}/{result.bot}: {action} submitted to private session; completion unverified.",))
    data = {key: value for key, value in asdict(result).items() if value is not None}
    data["native_outcome"] = "unattempted" if result.state == "requested" else "observed"
    data["runtime_state"] = "unknown" if result.state == "requested" else result.state
    if result.state == "stopped":
        lines = (f"{result.fleet}/{result.bot}: de-enrolled and stopped; "
                 "the declared bot and retained identity remain selected.",)
    elif result.state == "requested":
        kind = " into a fresh session" if result.handoff == "fresh_session" else ""
        lines = (f"{result.fleet}/{result.bot}: self restart{kind} requested; "
                 f"request={result.request_id}; final outcome in {result.log_path}.",)
    elif action == "restart" and result.handoff == "fresh_session":
        lines = (f"{result.fleet}/{result.bot}: restarted into a fresh session; readiness="
                 f"{result.readiness}; it resumes from the saved handoff when that is recent "
                 "enough (the RESUME line in its startup.log says which).",)
    elif action == "restart":
        lines = (f"{result.fleet}/{result.bot}: supervised session restarted; "
                 f"readiness={result.readiness}; handoff={result.handoff}.",)
    else:
        lines = (f"{result.fleet}/{result.bot}: supervised running; "
                 f"readiness={result.readiness}.",)
    return CommandOutput(data, release_id=result.release_id, lines=lines + lifecycle_notes(result))
