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
}


def collect_events(
    bots_dir: Path | str,
    *,
    bot: str | None = None,
    event_type: str | None = None,
    source: str | None = None,
    critical_only: bool = False,
) -> list[dict]:
    """Read JSONL events from all bots, optionally filtered."""
    bots_dir = Path(bots_dir)
    events: list[dict] = []

    bot_dirs = [bots_dir / bot] if bot else sorted(bots_dir.iterdir())

    for bd in bot_dirs:
        events_path = bd / "data" / "events"
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
            if k in ("session", "unit", "tool", "script", "state", "detail", "event"):
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
    )

    if args.json:
        for ev in events:
            print(json.dumps(ev, separators=(",", ":")))
    else:
        if args.tail:
            events = events[-args.tail :]
        print(format_event_table(events))

    return 0
