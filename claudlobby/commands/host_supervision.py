"""The committed Git runtime's supervised units: the failed-units read and the operator-only orphan reap."""

from __future__ import annotations

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    if args.public_command == "host.supervision.stop":
        return _stop(args)
    if args.public_command == "host.supervision.failed":
        return _failed(args)
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..context import resolve_paths
    from ..operation_context import OperationContextError
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError
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
    except (ActivationError, PlanError, ReleaseError, OperationContextError) as exc:
        raise CommandFailure("conflict", "selected fleet or release cannot be verified") from exc
    except OSError as exc:
        raise CommandFailure("unavailable", "orphan units cannot be inspected") from exc
    from ..runtime_admission import runtime_provenance
    data = {**runtime_provenance(result), "fleet": result.fleet, "bot": args.bot, "dry_run": result.dry_run,
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
        raise CommandFailure("conflict", "the committed Git configuration cannot be verified") from exc
    except OSError as exc:
        raise CommandFailure("unavailable", "failed units cannot be read") from exc
    lines = tuple(f"{unit['state']}: {unit['unit']} ({unit['scope']}"
                  + (f" {unit['fleet']}" if unit["fleet"] else "") + (f"/{unit['bot']}" if unit["bot"] else "")
                  + f"), {unit['surface']} {unit['detail']}" for unit in data["units"])
    return CommandOutput(data, release_id=data["release_id"],
                         lines=lines or (f"no owned unit is failed ({data['declared']} declared)",))


def _stop(args) -> CommandOutput:
    from dataclasses import asdict
    from ..job_operations import JobStopError, stop_job
    from ..host_job_operations import HostJobError
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..releases import ReleaseError
    from ..supervision_inventory import InventoryError
    from .host import _operator_shell
    from .releases import _host_root

    root = _host_root(args, allow_fleet=args.fleet_job is not None)
    _operator_shell(root)
    if args.fleet_job and not args.fleet:
        raise CommandFailure("invalid_argument", "--fleet-job requires an explicit --fleet selector")
    name = args.host_job or args.fleet_job
    try:
        result = stop_job(root, name, fleet=args.fleet if args.fleet_job else None)
    except JobStopError as exc:
        raise CommandFailure("unavailable" if exc.attempted else "conflict", str(exc),
                             data=exc.data, hint="inspect retained exact member outcomes; do not replay uncertain effects") from exc
    except (HostJobError, ActivationError, PlanError, ReleaseError, InventoryError, OSError, RuntimeError) as exc:
        raise CommandFailure("conflict", "job stop admission or native ownership is unavailable",
                             data={"name": name, "native_outcome": "unattempted"}) from exc
    return CommandOutput(asdict(result), lines=(f"{name}: stopped and de-enrolled; generated declarations retained.",))
