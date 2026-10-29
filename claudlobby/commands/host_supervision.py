"""The selected release's supervised units: the failed-units read and the operator-only orphan reap."""

from __future__ import annotations

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    if args.public_command == "host.supervision.failed":
        return _failed(args)
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



def _failed(args) -> CommandOutput:
    """Read-only: the owned units failed or running now (#2532)."""
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..context import resolve_paths
    from ..failed_units import read_failed_units
    from ..paths import InvalidPathSelector
    from ..supervision_inventory import InventoryError

    try:
        data = read_failed_units(resolve_paths(root=args.root).root)
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid root selector") from exc
    except InventoryError as exc:
        raise CommandFailure("unavailable", f"failed units cannot be read: {exc}") from exc
    except (ActivationError, PlanError) as exc:
        raise CommandFailure("conflict", "the selected release or its plan cannot be verified") from exc
    except OSError as exc:
        raise CommandFailure("unavailable", "failed units cannot be read") from exc
    lines = tuple(f"{unit['state']}: {unit['unit']} ({unit['scope']}"
                  + (f" {unit['fleet']}" if unit["fleet"] else "") + (f"/{unit['bot']}" if unit["bot"] else "")
                  + f"), {unit['surface']} {unit['detail']}" for unit in data["units"])
    return CommandOutput(data, release_id=data["release_id"],
                         lines=lines or (f"no owned unit is failed ({data['declared']} declared)",))
