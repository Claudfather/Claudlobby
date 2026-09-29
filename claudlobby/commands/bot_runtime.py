"""Public selected-release bot start, persistent stop and intentional restart."""

from dataclasses import asdict
from pathlib import Path

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..bot_operations import BotLifecycleError, handoff_bot, set_bot_running
    from ..config_plan import PlanError
    from ..context import resolve_paths
    from ..operation_context import OperationContextError
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError
    from ..runtime_admission import ReleaseMismatch
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
    data = {"fleet": args.fleet, "bot": args.bot_id, "requested": action,
            "native_outcome": "unattempted", "runtime_state": "unknown"}
    try:
        root = resolve_paths(root=args.root).root
        if action == "handoff":
            result = handoff_bot(root=root, fleet=args.fleet, bot=args.bot_id)
        else:
            result = set_bot_running(root=root, fleet=args.fleet, bot=args.bot_id,
                                     running=running, restart=action == "restart",
                                     ceiling=ceiling)
    except BotLifecycleError as exc:
        if exc.effect_attempted:
            data["native_outcome"] = "unknown"
            data["release_id"] = exc.release_id
            data["target"] = exc.target
            if action == "handoff":
                data["handoff"] = "unknown"
                data["reason"] = exc.handoff_reason or "unverified"
                message = str(exc) if exc.handoff_reason else "bot handoff outcome is unverified; inspect its private session"
                hint = "inspect the private session and handoff file; do not automatically resend the handoff"
            else:
                message = "bot lifecycle effect is unverified; inspect native state"
                hint = "inspect the exact bot's native unit and private session before retrying"
            raise CommandFailure("unavailable", message, data=data, release_id=exc.release_id,
                                 hint=hint) from exc
        if exc.unavailable:
            message = str(exc) if action == "stop" else "bot native or session readiness cannot be established"
            raise CommandFailure("unavailable", message,
                                 data=data,
                                 hint="inspect the exact private session; use bot restart for an intentional bounce") from exc
        raise CommandFailure("conflict", str(exc), data=data) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid bot root or fleet selector", data=data) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "bot operation requires the selected executable and release",
                             data=data) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected bot release differs from this executable", data=data) from exc
    except (ActivationError, PlanError, OperationContextError) as exc:
        raise CommandFailure("conflict", "selected bot configuration or activation is incomplete", data=data) from exc
    except (InventoryError, OSError) as exc:
        raise CommandFailure("unavailable", "bot native state cannot be established", data=data) from exc
    if action == "handoff":
        data = asdict(result)
        data["native_outcome"] = "observed" if result.handoff == "saved" else "skipped"
        lines = (f"{result.fleet}/{result.bot}: handoff saved and verified in the private session."
                 if result.handoff == "saved" else
                 f"{result.fleet}/{result.bot}: handoff skipped ({result.reason}); no new handoff verified.",)
        return CommandOutput(data, release_id=result.release_id, lines=lines)
    data = {key: value for key, value in asdict(result).items() if value is not None}
    data["native_outcome"] = "unattempted" if result.state == "requested" else "observed"
    data["runtime_state"] = "unknown" if result.state == "requested" else result.state
    if result.state == "stopped":
        lines = (f"{result.fleet}/{result.bot}: de-enrolled and stopped; "
                 "the declared bot and retained identity remain selected.",)
    elif result.state == "requested":
        lines = (f"{result.fleet}/{result.bot}: self restart requested; "
                 f"request={result.request_id}; final outcome in {result.log_path}.",)
    elif action == "restart":
        lines = (f"{result.fleet}/{result.bot}: supervised session restarted; "
                 f"readiness={result.readiness}; handoff={result.handoff}.",)
    else:
        lines = (f"{result.fleet}/{result.bot}: supervised running; "
                 f"readiness={result.readiness}.",)
    return CommandOutput(data, release_id=result.release_id, lines=lines)
