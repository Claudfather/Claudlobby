"""claudlobby events — tail/filter JSONL events across all bots."""

from __future__ import annotations

import json
import sys
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
    coverage: dict | None = None,
) -> list[dict]:
    """Read JSONL events from all bots and the fleet-level ledger, filtered.

    ``coverage``, when a dict is passed in, is filled with what this read could
    and could not reach: ``sources_total`` / ``sources_read`` and the ``absent``
    and ``unreadable`` paths. An out-param rather than a changed return type
    because ``brief.py`` and eleven tests already consume the list — the caller
    that wants the bound asks for it, and nothing else has to change.

    The reason it is worth asking for: a bot with no ``data/events`` directory and
    a bot whose events were all filtered out contribute the same nothing, so
    ``No events found.`` can mean "the fleet is quiet" or "no instrument has ever
    written here". Same class as #1216, and here it is a COVERAGE question rather
    than a refusal — partial data is still worth having, so the read continues and
    states its floor instead of failing.
    """
    bots_dir = Path(bots_dir)
    events: list[dict] = []
    absent: list[str] = []
    unreadable: list[str] = []
    sources_total = 0
    sources_read = 0

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
        sources_total += 1
        if not events_path.is_dir():
            absent.append(str(events_path))
            continue
        sources_read += 1
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
                unreadable.append(str(f))
                continue

    events.sort(key=lambda e: e.get("ts", ""))
    if coverage is not None:
        coverage.update(
            {
                "sources_total": sources_total,
                "sources_read": sources_read,
                "absent": absent,
                "unreadable": unreadable,
            }
        )
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


def coverage_line(coverage: dict) -> str:
    """One line stating what the read could not reach — or "" when it reached all.

    Silent on full coverage on purpose. A bound printed on every healthy run is
    wallpaper within a week, and a disclosure people have learned to skip is worse
    than none: it is the same false assurance with an alibi. So this speaks only
    when the count really is a floor.

    An absent events dir is normal for a bot that has not emitted yet, which is
    why absence is COUNTED but not itemised, while an unreadable file is named —
    that one is a fault someone can fix.
    """
    total = coverage.get("sources_total", 0)
    read = coverage.get("sources_read", 0)
    unreadable = coverage.get("unreadable", [])
    if not total or (read == total and not unreadable):
        return ""
    bits = [f"coverage: read {read} of {total} event source(s)"]
    if read < total:
        bits.append(f"{total - read} had no events directory")
    if unreadable:
        shown = ", ".join(unreadable[:3])
        more = f" (+{len(unreadable) - 3} more)" if len(unreadable) > 3 else ""
        bits.append(f"unreadable: {shown}{more}")
    return " — ".join(bits)


def cmd_events(args) -> int:
    """CLI entry point for ``claudlobby events``."""
    from ._helpers import _resolve_paths

    paths = _resolve_paths(args)
    bots_dir = paths.runtime_bots

    if not bots_dir.is_dir():
        print(f"No bots directory at {bots_dir}")
        return 1

    coverage: dict = {}
    events = collect_events(
        bots_dir,
        bot=args.bot,
        event_type=args.type,
        source=args.source,
        critical_only=args.critical,
        fleet_events_dir=paths.root / "state" / "events",
        coverage=coverage,
    )

    if args.json:
        for ev in events:
            print(json.dumps(ev, separators=(",", ":")))
    else:
        if args.tail:
            events = events[-args.tail :]
        print(format_event_table(events))
        line = coverage_line(coverage)
        if line:
            print(line)

    # Nothing readable at all is a refusal, not a quiet fleet: "No events found."
    # over zero reachable sources is a claim about the estate drawn from an
    # instrument that was never wired. Distinct from SOME sources missing, which
    # is a coverage note above and keeps rc 0 because partial data is still data.
    if coverage.get("sources_total") and not coverage.get("sources_read"):
        print(
            "no event source was readable — the count above is not evidence "
            "the fleet is quiet",
            file=sys.stderr if args.json else sys.stdout,
        )
        return 1

    return 0
