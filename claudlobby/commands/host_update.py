"""Public host update result adapter; native updaters own the effects."""

from __future__ import annotations

from dataclasses import asdict
import os

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..env_tiers import ResolverUnavailable
    from ..host_update_operations import HostUpdateError, run_host_update
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError
    from ..runtime_admission import ReleaseMismatch
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
        raise CommandFailure("unavailable" if exc.effect_attempted else "conflict", str(exc),
                             data=data) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "host update requires the selected executable",
                             data=data) from exc
    except (ActivationError, ReleaseError, InvalidPathSelector, ResolverUnavailable, RuntimeError) as exc:
        raise CommandFailure("conflict", "selected host update scope cannot be verified", data=data) from exc
    return CommandOutput(asdict(result), result.release_id,
                         (f"{action} update tick completed; inspect its log for per-target outcomes.",))
