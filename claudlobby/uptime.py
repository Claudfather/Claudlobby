"""Keepalive log aggregation — per-bot uptime, MTBR, restart-rate metrics.

Parses keepalive.log files (written by lib/keepalive.sh) and computes:
- Uptime percentage over configurable windows (24h / 7d / 30d)
- Restart count and MTBR (mean time between restarts)
- Time-in-BUSY vs time-in-IDLE breakdown
- First-boot timestamp for the current process
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Keepalive log line format (from keepalive.sh):
#   2026-05-17T15:28:33-04:00 BUSY — active processing
#   2026-05-17T15:28:33-04:00 RESTART — session dead, systemctl ...
_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})\s+"
    r"(BUSY|IDLE|RESTART|UNKNOWN|SKIP)\b"
)

WINDOWS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# Max duration to attribute to a single keepalive interval.  Keepalive runs
# every 60s (via keepalive-all) so gaps >10 min likely mean the bot (or the
# host) was down — don't credit that time to any state.
_MAX_INTERVAL_SECS = 600


def parse_keepalive_log(log_path: Path) -> list[tuple[datetime, str]]:
    """Parse a keepalive.log file into (timestamp, state) entries."""
    entries: list[tuple[datetime, str]] = []
    if not log_path.exists():
        return entries
    for line in log_path.read_text().splitlines():
        m = _LOG_LINE_RE.match(line)
        if m:
            ts = datetime.fromisoformat(m.group(1))
            entries.append((ts, m.group(2)))
    return entries


def collect_bot_logs(bot_dir: Path) -> list[tuple[datetime, str]]:
    """Collect all keepalive entries for a bot, including rotated logs."""
    entries: list[tuple[datetime, str]] = []

    # Rotated logs first (keepalive.log.1, .2, ...) — oldest data
    for rotated in sorted(bot_dir.glob("keepalive.log.*")):
        if rotated.suffix != ".gz":
            entries.extend(parse_keepalive_log(rotated))

    # Current log
    entries.extend(parse_keepalive_log(bot_dir / "keepalive.log"))

    entries.sort(key=lambda e: e[0])
    return entries


def compute_metrics(
    entries: list[tuple[datetime, str]],
    window: timedelta,
    now: datetime | None = None,
) -> dict:
    """Compute uptime metrics for a time window.

    Returns:
        uptime_pct      - % of window the bot was up (BUSY + IDLE + SKIP)
        restart_count   - RESTART events in window
        mtbr_seconds    - mean time between restarts (None if 0 restarts)
        busy_seconds    - seconds in BUSY
        idle_seconds    - seconds in IDLE
        unknown_seconds - seconds in UNKNOWN/SKIP
        first_boot      - ISO timestamp of first entry after last restart
        entries_in_window - log entries in window
    """
    if now is None:
        now = datetime.now(timezone.utc)

    cutoff = now - window
    windowed = [(ts, state) for ts, state in entries if ts >= cutoff]

    if not windowed:
        return {
            "uptime_pct": 0.0,
            "restart_count": 0,
            "mtbr_seconds": None,
            "busy_seconds": 0,
            "idle_seconds": 0,
            "unknown_seconds": 0,
            "first_boot": None,
            "entries_in_window": 0,
        }

    busy_secs = 0.0
    idle_secs = 0.0
    unknown_secs = 0.0
    restart_count = 0
    last_restart_ts: datetime | None = None

    for i, (ts, state) in enumerate(windowed):
        if i + 1 < len(windowed):
            duration = (windowed[i + 1][0] - ts).total_seconds()
        else:
            duration = (now - ts).total_seconds()

        duration = min(duration, _MAX_INTERVAL_SECS)

        if state == "RESTART":
            restart_count += 1
            last_restart_ts = ts
        elif state == "BUSY":
            busy_secs += duration
        elif state == "IDLE":
            idle_secs += duration
        elif state in ("UNKNOWN", "SKIP"):
            unknown_secs += duration

    up_secs = busy_secs + idle_secs + unknown_secs
    total_secs = window.total_seconds()
    uptime_pct = min((up_secs / total_secs) * 100, 100.0) if total_secs else 0.0

    mtbr = None
    if restart_count > 0:
        mtbr = round(up_secs / restart_count)

    # First boot: first non-RESTART entry after most recent restart
    first_boot = None
    if last_restart_ts is not None:
        for ts, state in windowed:
            if ts > last_restart_ts and state != "RESTART":
                first_boot = ts.isoformat()
                break
    elif windowed:
        first_boot = windowed[0][0].isoformat()

    return {
        "uptime_pct": round(uptime_pct, 1),
        "restart_count": restart_count,
        "mtbr_seconds": mtbr,
        "busy_seconds": round(busy_secs),
        "idle_seconds": round(idle_secs),
        "unknown_seconds": round(unknown_secs),
        "first_boot": first_boot,
        "entries_in_window": len(windowed),
    }


def entries_from_plane(pr, conn, fleet: str, bot: str, since_iso: str) -> list[tuple[datetime, str]]:
    """The (timestamp, state) entries `compute_metrics` consumes, from the
    plane's keepalive entries (cutover B2 — the heartbeat samples, the
    dead-session fact, the restart transitions) instead of keepalive.log."""
    out: list[tuple[datetime, str]] = []
    for at, state in pr.keepalive_entries(conn, fleet, bot, since_iso):
        try:
            ts = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append((ts, state))
    return out


def aggregate_fleet(
    bots_dir: Path,
    windows: list[str] | None = None,
    bot_filter: str | None = None,
    bot_dirs: list[Path] | None = None,
    entries_for=None,
) -> dict:
    """Aggregate metrics for all bots (or one) in a fleet's runtime dir.

    ``bot_dirs``: the FULLY MATERIALIZED listing from the caller's
    ``scan_dir`` — when given, no re-enumeration happens here. The glob
    fallback re-opens the directory after the caller's probe, and
    ``Path.glob`` swallows a mid-iteration OSError, so a live bot behind a
    benign entry vanished silently at rc 0 (external round 4, probed).
    CLI callers must pass it; the fallback survives only for direct
    library use against dirs the caller already trusts."""
    if windows is None:
        windows = list(WINDOWS.keys())

    now = datetime.now(timezone.utc)
    results: dict[str, dict] = {}

    if bot_dirs is not None:
        candidates = []
        for bd in sorted(bot_dirs):
            try:
                if (bd / "bot.conf").is_file():
                    candidates.append(bd / "bot.conf")
            except OSError:
                continue
    else:
        candidates = sorted(bots_dir.glob("*/bot.conf"))

    for bot_conf in candidates:
        bot_dir = bot_conf.parent
        bot_name = bot_dir.name
        if bot_filter and bot_name != bot_filter:
            continue

        # ``entries_for`` (cutover B2): the plane's entries once the events write
        # is retired; the log otherwise — the same arithmetic over the same pairs.
        entries = entries_for(bot_dir) if entries_for is not None else collect_bot_logs(bot_dir)
        results[bot_name] = {}
        for w in windows:
            results[bot_name][w] = compute_metrics(entries, WINDOWS[w], now=now)

    return results


# -- Output formatters -----------------------------------------------------


def format_table(results: dict, window: str = "24h") -> str:
    """Render metrics as an aligned text table."""
    em = "\u2014"
    lines: list[str] = []
    hdr = (
        f"{'Bot':<15} {'Uptime':>7} {'Restarts':>9} "
        f"{'MTBR':>10} {'Busy':>8} {'Idle':>8} {'First Boot':<20}"
    )
    lines.append(hdr)
    lines.append("\u2500" * len(hdr))

    for bot_name, win_data in sorted(results.items()):
        m = win_data.get(window, {})
        if not m or m.get("entries_in_window", 0) == 0:
            lines.append(
                f"{bot_name:<15} {em:>7} {em:>9} {em:>10} {em:>8} {em:>8} {em:<20}"
            )
            continue

        uptime = f"{m['uptime_pct']}%"
        restarts = str(m["restart_count"])
        mtbr = (
            _fmt_duration(m["mtbr_seconds"])
            if m["mtbr_seconds"] is not None
            else "\u221e"
        )
        busy = _fmt_duration(m["busy_seconds"])
        idle = _fmt_duration(m["idle_seconds"])
        boot = (m.get("first_boot") or em)[:19]

        lines.append(
            f"{bot_name:<15} {uptime:>7} {restarts:>9} "
            f"{mtbr:>10} {busy:>8} {idle:>8} {boot:<20}"
        )

    return "\n".join(lines)


def format_json(results: dict) -> str:
    """Render full results as JSON."""
    return json.dumps(results, indent=2)


def _fmt_duration(seconds: float | int) -> str:
    """Human-readable duration from seconds."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"
