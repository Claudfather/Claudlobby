"""Public host update result adapter; native updaters own the effects."""

from __future__ import annotations

from dataclasses import asdict
import os

from ..command_result import CommandFailure, CommandOutput


def _named(exc: BaseException, data: dict) -> str | None:
    """Put what stopped the update in `data` as `cause`, and return a hint or None (#2945).

    The cause is the failure's first line, cut to 500 characters, as the lifecycle
    doors name theirs (`failure_cause`, #2487). Generated units that differ from the
    committed configuration are listed in full as `drift`. A resolver failure is
    named by its kind only: the resolver's own output stays with
    `claudlobby host env tiers`, which shows it.
    """
    from ..config_units import UnitDriftError
    from ..env_tiers import ResolverUnavailable
    from .bot_runtime import failure_cause

    if isinstance(exc, ResolverUnavailable):
        data["cause"] = "the .env tier resolver is unavailable"
        return "claudlobby host env tiers shows why"
    data["cause"] = failure_cause(exc)
    if isinstance(exc, UnitDriftError):
        data["drift"] = [dict(row) for row in exc.rows]
    return None


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..env_tiers import ResolverUnavailable
    from ..host_update_operations import HostUpdateError, run_host_update
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError
    from .releases import _host_root

    if any(key in os.environ for key in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE")):
        raise CommandFailure("conflict", "host update requires an operator or host timer context")
    if args.fleet or args.seed:
        raise CommandFailure("invalid_argument", "host update cannot select a fleet")
    action = "runtime" if args.public_command == "host.update.runtime" else "siblings"
    root = _host_root(args)
    dry_run = bool(getattr(args, "dry_run", False))
    data = {"action": action, "dry_run": dry_run, "native_outcome": "unattempted"}
    try:
        result = run_host_update(root, action, dry_run=dry_run,
                                 scheduled=os.environ.get("CLAUDLOBBY_UPDATE_SCHEDULED") == "1")
    except HostUpdateError as exc:
        data["native_outcome"] = "unknown" if exc.effect_attempted else "unattempted"
        message, hint = str(exc), None
        if exc.__cause__ is not None:
            # The failure it wraps, such as a job binding that could not be proved (#2945).
            hint = _named(exc.__cause__, data)
            message = f"{message}: {data['cause']}"
        raise CommandFailure("unavailable" if exc.effect_attempted else "conflict", message,
                             data=data, hint=hint) from exc
    except (ActivationError, PlanError, ReleaseError, InvalidPathSelector, ResolverUnavailable, RuntimeError) as exc:
        hint = _named(exc, data)
        raise CommandFailure("conflict", f"selected host update scope cannot be verified: {data['cause']}",
                             data=data, hint=hint) from exc
    data = asdict(result)
    if result.source_revision is None:
        data.pop("source_revision")
        data.pop("config_fingerprint")
    return CommandOutput(data, result.release_id,
                         (f"{action} update tick completed; inspect its log for per-target outcomes.",))
