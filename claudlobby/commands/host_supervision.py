"""Operator-only selected-fleet orphan supervision reap."""

from __future__ import annotations

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..context import resolve_paths
    from ..operation_context import OperationContextError
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError
    from ..runtime_admission import ReleaseMismatch
    from ..supervision_reap import SupervisionReapError, reap_selected_orphans

    if args.seed:
        raise CommandFailure("conflict", "orphan supervision reap requires an active fleet")
    try:
        root = resolve_paths(root=args.root).root
        result = reap_selected_orphans(root=root, fleet=args.fleet, bot=args.bot,
                                       dry_run=args.dry_run)
    except SupervisionReapError as exc:
        raise CommandFailure("conflict", str(exc)) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid fleet root or selector") from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "orphan reap requires the selected executable") from exc
    except (ActivationError, PlanError, ReleaseError, OperationContextError) as exc:
        raise CommandFailure("conflict", "selected fleet or release cannot be verified") from exc
    except OSError as exc:
        raise CommandFailure("unavailable", "orphan units cannot be inspected") from exc
    data = {"fleet": result.fleet, "bot": args.bot, "dry_run": result.dry_run,
            "paths": [str(path) for path in result.paths], "count": len(result.paths)}
    verb = "would reap" if result.dry_run else "reaped"
    lines = tuple(f"{verb} orphan unit: {path}" for path in result.paths)
    if not lines:
        lines = ("no orphan units to reap",)
    return CommandOutput(data, release_id=result.release_id, lines=lines)
