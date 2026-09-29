"""Public status and uptime routes over the existing native and Plane readers."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError, read_selection
    from ..status import collect_fleet_status, format_bot_detail, format_json, format_table
    from .orientation import _context

    context = _context(args)
    try:
        selected = read_selection(context.paths.root)
    except ActivationError as exc:
        raise CommandFailure("conflict", "release selection cannot be read") from exc
    release_id = selected["release_id"] if selected else None
    if args.public_command == "fleet.uptime":
        return _uptime(args, context, release_id)

    if args.public_command == "bot.status" and args.bot_id not in context.fleet.bots:
        raise CommandFailure("not_found", "bot is not declared in the selected fleet")
    statuses = collect_fleet_status(context.fleet, context.paths)
    switch_states = None
    try:
        from ..switches import resolve
        switch_states = resolve(context.paths, context.fleet)
    except Exception:
        # Status remains readable when the optional switch header cannot be resolved.
        pass
    if args.public_command == "bot.status":
        status = next((row for row in statuses if row.name == args.bot_id), None)
        if status is None:
            raise CommandFailure("unavailable", "declared bot status could not be collected")
        data = {"fleet": context.fleet.name,
                "bot": json.loads(format_json([status], context.fleet.name))["bots"][0]}
        lines = (format_bot_detail(status).rstrip(),)
    else:
        data = json.loads(format_json(statuses, context.fleet.name, switch_states))
        lines = (format_table(statuses, context.fleet.name, switch_states).rstrip(),)
    return CommandOutput(data, release_id=release_id, lines=lines)


def _uptime(args, context, release_id: str | None) -> CommandOutput:
    from ..brief import plane_session
    from ..source_state import scan_dir, unreachable_line
    from ..uptime import WINDOWS, aggregate_fleet, entries_from_plane, format_table
    from .core import _coverage_line

    if args.bot is not None and args.bot not in context.fleet.bots:
        raise CommandFailure("not_found", "bot is not declared in the selected fleet")
    bots_dir = context.paths.runtime_bots
    probe, bot_dirs = scan_dir(bots_dir)
    if not probe.reachable:
        raise CommandFailure("unavailable", unreachable_line("the runtime bots dir", probe))
    windows = [args.window] if args.window else list(WINDOWS)
    plane, note = plane_session(context.paths, context.fleet.name)
    if plane is None:
        raise CommandFailure("unavailable", f"uptime Plane source is unavailable: {note}")
    since = (datetime.now(timezone.utc) - max(WINDOWS.values())).isoformat()
    try:
        results = aggregate_fleet(
            bots_dir, windows=windows, bot_filter=args.bot, bot_dirs=bot_dirs,
            entries_for=lambda bot_dir: entries_from_plane(
                plane.pr, plane.conn, plane.fleet, bot_dir.name, since))
        coverage = {w: _coverage_line(plane, WINDOWS[w].total_seconds()) for w in windows}
    except (plane.pr.PlaneUnreachable, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "uptime Plane source could not answer") from exc
    finally:
        plane.close()
    display_window = args.window or "24h"
    data = {"fleet": context.fleet.name, "bots": results, "coverage": coverage,
            "source": "Plane keepalive samples and restart transitions"}
    lines = (format_table(results, window=display_window), coverage[display_window])
    return CommandOutput(data, release_id=release_id, lines=lines)
