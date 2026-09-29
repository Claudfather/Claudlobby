"""Public selected-fleet lifecycle and read-only reconciliation."""

from __future__ import annotations

from dataclasses import asdict

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..context import resolve_paths
    from ..fleet_operations import (FleetLifecycleError, reconcile_fleet,
                                    set_fleet_running)
    from ..operation_context import OperationContextError
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError
    from ..runtime_admission import ReleaseMismatch
    from ..supervision_inventory import InventoryError

    action = args.public_command.removeprefix("fleet.")
    if args.seed:
        raise CommandFailure("conflict", "fleet runtime requires an active host")
    if action not in {"start", "stop", "restart", "reconcile", "reload"}:
        raise CommandFailure("invalid_argument", "unsupported fleet runtime action")
    if action == "reconcile" and getattr(args, "workers", False):
        raise CommandFailure("invalid_argument", "reconcile covers every declared bot")
    data = {"fleet": args.fleet, "requested": action,
            "workers_only": bool(getattr(args, "workers", False)),
            "completed": [], "failed_bot": None}
    try:
        root = resolve_paths(root=args.root).root
        if action == "reload":
            from ..fleet_reload import FleetReloadError, reload_fleet
            try:
                result = reload_fleet(root=root, fleet=args.fleet)
            except FleetReloadError as exc:
                data["native_outcome"] = "unknown" if exc.effect_attempted else "unattempted"
                raise CommandFailure(exc.code, str(exc), data=data) from exc
            data = {"fleet": result.fleet, "release_id": result.release_id,
                    "plugins_refreshed": list(result.plugins_refreshed),
                    "bots_marked": list(result.bots_marked),
                    "bots_marked_count": len(result.bots_marked),
                    "configuration_changed": False, "enrollment_changed": False}
            lines = (f"{result.fleet}: refreshed {len(result.plugins_refreshed)} plugin(s); "
                     f"marked {len(result.bots_marked)} running bot(s) for idle reload.",)
        elif action == "reconcile":
            result = reconcile_fleet(root=root, fleet=args.fleet)
            data = {"fleet": result.fleet, "release_id": result.release_id,
                    "bots": [asdict(bot) for bot in result.bots],
                    "source": "selected configuration and native inventory"}
            lines = tuple(f"{result.fleet}/{bot.bot}: {bot.state} "
                          f"(declared={bot.declared}, enrolled={bot.enrolled}, "
                          f"native={bot.native_active}, session={bot.session})"
                          for bot in result.bots)
        else:
            result = set_fleet_running(root=root, fleet=args.fleet, action=action,
                                       workers_only=bool(getattr(args, "workers", False)))
            data = {"fleet": result.fleet, "release_id": result.release_id,
                    "requested": result.action, "workers_only": result.workers_only,
                    "completed": [asdict(bot) for bot in result.completed], "failed_bot": None}
            lines = tuple(f"{bot.fleet}/{bot.bot}: {bot.state}; readiness={bot.readiness}"
                          for bot in result.completed)
        return CommandOutput(data, release_id=result.release_id, lines=lines)
    except FleetLifecycleError as exc:
        data["completed"] = [asdict(bot) for bot in exc.completed]
        data["failed_bot"] = exc.bot
        data["native_outcome"] = "unknown" if exc.effect_attempted else "unattempted"
        data["release_id"] = exc.release_id
        raise CommandFailure("unavailable" if exc.effect_attempted or exc.unavailable else "conflict",
                             str(exc), data=data, release_id=exc.release_id,
                             hint="inspect the failed bot before resuming the remaining sweep"
                             if exc.bot else None) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid fleet root or selector", data=data) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "fleet operation requires the selected executable and release",
                             data=data) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected fleet release differs from this executable",
                             data=data) from exc
    except (ActivationError, PlanError, OperationContextError) as exc:
        raise CommandFailure("conflict", "selected fleet configuration or activation is incomplete",
                             data=data) from exc
    except (InventoryError, OSError) as exc:
        raise CommandFailure("unavailable", "fleet native state cannot be established", data=data) from exc
