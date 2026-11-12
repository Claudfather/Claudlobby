"""Private fleet timer adapter for the session-export contract consumer."""

from dataclasses import asdict
import json
import os


def tick(args) -> int:
    if os.environ.get("PLANE_EMIT_DISABLED") == "1":
        print(
            "session-export: OFF here (PLANE_EMIT_DISABLED=1); nothing is exported or acked"
        )
        return 0
    if os.environ.get("SESSION_EXPORT_ENABLED") == "0":
        print(
            "session-export: OFF here (SESSION_EXPORT_ENABLED=0); nothing is exported"
        )
        return 0
    from ..operation_context import resolve_operation_scope
    from ..session_export import BotOutcome, export_bot

    try:
        selected, _origin = resolve_operation_scope(
            root=args.root, fleet=args.tick_fleet
        )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"session-export: scope unavailable: {exc}")
        return 0
    outcomes = []
    for bot in selected.fleet.bots:
        try:
            result = export_bot(
                selected.paths.root,
                selected.fleet.name,
                bot,
                selected.paths.bot_runtime(bot) / "data" / "claudna",
                dry_run=args.dry_run,
                fleet_state=selected.paths.fleet_state,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            result = BotOutcome(bot, failed=f"state_unavailable: {exc}")
        outcomes.append(asdict(result))
        if not getattr(args, "json", False):
            detail = (
                f"skipped: {result.skipped}"
                if result.skipped
                else f"failed: {result.failed}"
                if result.failed
                else f"{result.emitted} emitted ({result.duplicate} duplicate, {result.spooled} spooled), "
                f"{result.unexportable} unexportable, acked {result.acked}"
            )
            print(f"session-export: {bot}: {detail}")
            if args.dry_run:
                print(json.dumps(result.records))
    if getattr(args, "json", False):
        print(json.dumps({"fleet": selected.fleet.name, "bots": outcomes}))
    return 0
