"""claudlobby events — tail/filter JSONL events across all bots."""

from __future__ import annotations

import json
from pathlib import Path

# Critical event types — fleet health problems that need attention.
CRITICAL_TYPES = {
    "session_missing",
    "service_down",
    "activity_stuck",
    "script_error",
    "overdue_dispatch",
    "bridge_down",
    "reload_failed",
    "restart_failed",
    "rc_timeout",
}


def collect_events(
    bots_dir: Path | str,
    *,
    bot: str | None = None,
    event_type: str | None = None,
    source: str | None = None,
    critical_only: bool = False,
    fleet_events_dir: Path | str | None = None,
) -> list[dict]:
    """Read JSONL events from all bots and the fleet-level ledger, filtered."""
    bots_dir = Path(bots_dir)
    events: list[dict] = []

    # (dir, bot-to-match): per-bot rows are selected by which directory they
    # came from, so they need no field match. The fleet ledger holds every bot's
    # rows in one file — plus events that outlive, or never had, a bot dir
    # (host-job alerts, script_error breadcrumbs, a teardown receipt whose whole
    # purpose is surviving the dir it documents) — so --bot must key on the row.
    bot_dirs = [bots_dir / bot] if bot else sorted(bots_dir.iterdir())
    ledgers = [(bd / "data" / "events", None) for bd in bot_dirs]
    if fleet_events_dir is not None:
        ledgers.append((Path(fleet_events_dir), bot))

    for events_path, want_bot in ledgers:
        if not events_path.is_dir():
            continue
        for f in sorted(events_path.glob("*.jsonl")):
            try:
                for line in f.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if want_bot and ev.get("bot") != want_bot:
                        continue
                    if event_type and ev.get("type") != event_type:
                        continue
                    if source and ev.get("source") != source:
                        continue
                    if critical_only and ev.get("type") not in CRITICAL_TYPES:
                        continue
                    events.append(ev)
            except OSError:
                continue

    events.sort(key=lambda e: e.get("ts", ""))
    return events


def format_event_table(events: list[dict]) -> str:
    """Format events as a human-readable table."""
    if not events:
        return "No events found."

    lines = []
    header = f"{'TIME':<22} {'BOT':<10} {'TYPE':<20} {'SOURCE':<10} {'DETAIL'}"
    lines.append(header)
    lines.append("-" * len(header))

    for ev in events:
        ts = ev.get("ts", "?")
        if len(ts) > 19:
            ts = ts[:19]
        bot_name = ev.get("bot", "?")
        ev_type = ev.get("type", "?")
        ev_source = ev.get("source", "?")
        data = ev.get("data", {})

        detail_parts = []
        for k, v in data.items():
            # actor/reason carry a teardown's who and why. Without them the row
            # falls back to raw JSON and is truncated mid-field, so the one
            # question a teardown record exists to answer goes unanswered.
            if k in (
                "session",
                "unit",
                "tool",
                "script",
                "state",
                "detail",
                "event",
                "actor",
                "reason",
            ):
                detail_parts.append(f"{k}={v}")
        detail = ", ".join(detail_parts) or json.dumps(data, separators=(",", ":"))
        if len(detail) > 60:
            detail = detail[:57] + "..."

        lines.append(f"{ts:<22} {bot_name:<10} {ev_type:<20} {ev_source:<10} {detail}")

    return "\n".join(lines)


def cmd_events(args) -> int:
    """CLI entry point for ``claudlobby events``."""
    from ._helpers import _resolve_paths

    paths = _resolve_paths(args)
    bots_dir = paths.runtime_bots

    if not bots_dir.is_dir():
        print(f"No bots directory at {bots_dir}")
        return 1

    events = collect_events(
        bots_dir,
        bot=args.bot,
        event_type=args.type,
        source=args.source,
        critical_only=args.critical,
        fleet_events_dir=paths.root / "state" / "events",
    )

    if args.json:
        for ev in events:
            print(json.dumps(ev, separators=(",", ":")))
    else:
        if args.tail:
            events = events[-args.tail :]
        print(format_event_table(events))

    return 0
