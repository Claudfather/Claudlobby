"""Uptime metrics — per-bot uptime, MTBR, restart-rate from the plane.

The (instant, state) pairs come from the plane alone (F18 closure R2b): the
``bot.heartbeat`` samples keepalive records each tick (BUSY / IDLE / UNKNOWN),
the ``bot.session_up = false`` fact of a dead session (DOWN — no uptime, like
the log's gap once was) and the ``keepalive_restart`` fleet events (RESTART),
through ``claudlobby/_runtime_scripts/plane-readers.py::keepalive_entries``. Computes:
- Uptime over configurable windows (24h / 7d / 30d) as the share of OBSERVED
  time the session was up, beside the share of the window observed (#891).
  Missing history is neither up nor down: `plane prune` ages samples out at
  30 days, a record starts late, the host or keepalive goes quiet, and a
  wall-clock denominator read each of those as downtime.
- Restart episodes and MTBR (mean time between restarts)
- Time-in-BUSY vs time-in-IDLE breakdown
- First-boot timestamp for the current process
The keepalive.log parser is gone with the file it parsed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

WINDOWS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# Max duration to attribute to a single keepalive interval.  Keepalive runs
# every 60s (via keepalive-all) so gaps >10 min likely mean the bot (or the
# host) was down — don't credit that time to any state.
_MAX_INTERVAL_SECS = 600

# The samples that say the session was up. HELD (#2070) is a live session whose
# input box holds text that was never submitted: up, and counted where those
# ticks counted while keepalive could only call them UNKNOWN.
_UP_STATES = frozenset({"BUSY", "IDLE", "UNKNOWN", "SKIP", "HELD"})


def _restart_episodes(entries: list[tuple[datetime, str]]) -> list[tuple[datetime, bool]]:
    """``[(start, seen_up)]`` for each restart episode in time-ordered *entries*.

    An episode is a run of RESTART and DOWN rows with no up sample between them
    and no gap over ``_MAX_INTERVAL_SECS``, holding at least one RESTART: the
    dead ticks while a session comes back are ONE restart, not one per
    kickstart (#1616 counted 15 for one boot).

    ``seen_up`` is whether the bot was seen up within ``_MAX_INTERVAL_SECS``
    before the episode began, the same cap the uptime arithmetic credits a
    sample for. Only then is it the bot's restart. After a longer stretch with
    no up sample (a host outage, a keepalive or recording gap, a session that
    stayed down) it is not. With no earlier up sample at all (the record starts
    there, or a prune aged the samples out and kept the restart event) nothing
    shows a silence, and the episode counts.
    """
    episodes: list[tuple[datetime, bool]] = []
    last_up: datetime | None = None
    start = last = up_before = None
    restarted = False
    for ts, state in [*entries, (None, "")]:
        in_run = state in ("RESTART", "DOWN")
        if start is not None and (not in_run or (ts - last).total_seconds() > _MAX_INTERVAL_SECS):
            if restarted:
                seen_up = up_before is None or (start - up_before).total_seconds() <= _MAX_INTERVAL_SECS
                episodes.append((start, seen_up))
            start = None
        if in_run:
            if start is None:
                start, up_before, restarted = ts, last_up, False
            last = ts
            restarted = restarted or state == "RESTART"
        elif state in _UP_STATES:
            last_up = ts
    return episodes


def compute_metrics(
    entries: list[tuple[datetime, str]],
    window: timedelta,
    now: datetime | None = None,
) -> dict:
    """Compute uptime metrics for a time window.

    Observed time is measured between samples (the heartbeat verdicts and the
    DOWN fact), each interval capped at ``_MAX_INTERVAL_SECS``. A RESTART event
    marks an episode and is not itself an observation, so an event whose
    samples have aged out adds no time. Restart episodes and MTBR read the
    whole of *entries*, so a gap is measured back past the window's edge.

    Returns:
        uptime_pct      - % of OBSERVED time the bot was up (BUSY + IDLE +
                          UNKNOWN/SKIP/HELD); None when nothing was observed
        observed_pct    - % of the window observed
        observed_seconds - seconds observed, up or down
        restart_count   - restart episodes in window the bot was seen up just
                          before (see ``_restart_episodes``)
        restarts_after_silence - episodes in window that followed over
                          ``_MAX_INTERVAL_SECS`` with no up sample: not counted,
                          not in MTBR
        restart_events  - RESTART rows in window, every kickstart included
        mtbr_seconds    - mean gap between consecutive counted episodes, for
                          those starting in window (None without a gap)
        busy_seconds    - seconds in BUSY
        idle_seconds    - seconds in IDLE
        unknown_seconds - seconds in UNKNOWN/SKIP/HELD
        down_seconds    - seconds observed DOWN (a dead session)
        first_boot      - ISO timestamp of the first up sample after the last
                          restart (the first entry when none is in window)
        entries_in_window - entries in window
    """
    if now is None:
        now = datetime.now(timezone.utc)

    entries = sorted(entries, key=lambda e: e[0])
    cutoff = now - window
    windowed = [(ts, state) for ts, state in entries if ts >= cutoff]

    episodes = _restart_episodes(entries)
    counted = [start for start, seen_up in episodes if seen_up]
    gaps = [(b - a).total_seconds() for a, b in zip(counted, counted[1:]) if b >= cutoff]

    busy_secs = 0.0
    idle_secs = 0.0
    unknown_secs = 0.0
    down_secs = 0.0

    samples = [(ts, state) for ts, state in windowed if state != "RESTART"]
    for i, (ts, state) in enumerate(samples):
        if i + 1 < len(samples):
            duration = (samples[i + 1][0] - ts).total_seconds()
        else:
            duration = (now - ts).total_seconds()

        duration = max(0.0, min(duration, _MAX_INTERVAL_SECS))

        if state == "BUSY":
            busy_secs += duration
        elif state == "IDLE":
            idle_secs += duration
        elif state in _UP_STATES:
            unknown_secs += duration
        elif state == "DOWN":
            down_secs += duration

    up_secs = busy_secs + idle_secs + unknown_secs
    observed_secs = up_secs + down_secs
    total_secs = window.total_seconds()
    uptime_pct = round(up_secs / observed_secs * 100, 1) if observed_secs else None
    observed_pct = round(min(observed_secs / total_secs * 100, 100.0), 1) if total_secs else 0.0

    # First boot: first up sample after the most recent restart (the DOWN sample
    # keepalive lands with a RESTART is the dead session, not its boot)
    last_restart_ts = max((ts for ts, state in windowed if state == "RESTART"), default=None)
    first_boot = None
    if last_restart_ts is not None:
        for ts, state in windowed:
            if ts > last_restart_ts and state in _UP_STATES:
                first_boot = ts.isoformat()
                break
    elif windowed:
        first_boot = windowed[0][0].isoformat()

    return {
        "uptime_pct": uptime_pct,
        "observed_pct": observed_pct,
        "observed_seconds": round(observed_secs),
        "restart_count": sum(1 for start in counted if start >= cutoff),
        "restarts_after_silence": sum(1 for start, seen_up in episodes if not seen_up and start >= cutoff),
        "restart_events": sum(1 for _, state in windowed if state == "RESTART"),
        "mtbr_seconds": round(sum(gaps) / len(gaps)) if gaps else None,
        "busy_seconds": round(busy_secs),
        "idle_seconds": round(idle_secs),
        "unknown_seconds": round(unknown_secs),
        "down_seconds": round(down_secs),
        "first_boot": first_boot,
        "entries_in_window": len(windowed),
    }


def entries_from_plane(pr, conn, fleet: str, bot: str, since_iso: str) -> list[tuple[datetime, str]]:
    """The (timestamp, state) entries `compute_metrics` consumes, from the
    plane's keepalive entries — the heartbeat samples, the dead-session fact
    (DOWN), the restart transitions. The only provider (F18 closure R2b)."""
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
    *,
    entries_for,
) -> dict:
    """Aggregate metrics for all bots (or one) in a fleet's runtime dir.

    ``entries_for(bot_dir)`` → the bot's (instant, state) pairs, REQUIRED:
    the plane's (``entries_from_plane``) — there is no file fallback.

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

        entries = entries_for(bot_dir)
        results[bot_name] = {}
        for w in windows:
            results[bot_name][w] = compute_metrics(entries, WINDOWS[w], now=now)

    return results


# -- Output formatters -----------------------------------------------------


def format_table(results: dict, window: str = "24h") -> str:
    """Render metrics as an aligned text table, then what its percentages divide
    by and any restarts the count left out (#891)."""
    em = "\u2014"
    lines: list[str] = []
    notes: list[str] = []
    hdr = (
        f"{'Bot':<15} {'Up (obs)':>8} {'Observed':>8} {'Restarts':>9} "
        f"{'MTBR':>10} {'Busy':>8} {'Idle':>8} {'First Boot':<20}"
    )
    lines.append(hdr)
    lines.append("\u2500" * len(hdr))

    for bot_name, win_data in sorted(results.items()):
        m = win_data.get(window, {})
        if not m or m.get("entries_in_window", 0) == 0:
            lines.append(
                f"{bot_name:<15} {em:>8} {em:>8} {em:>9} {em:>10} {em:>8} {em:>8} {em:<20}"
            )
            continue

        uptime = em if m.get("uptime_pct") is None else f"{m['uptime_pct']}%"
        observed = em if m.get("observed_pct") is None else f"{m['observed_pct']}%"
        restarts = str(m["restart_count"])
        if m["mtbr_seconds"] is not None:
            mtbr = _fmt_duration(m["mtbr_seconds"])
        else:
            # no restart in the window, or restarts with no gap between them yet
            mtbr = "\u221e" if not m["restart_count"] else em
        busy = _fmt_duration(m["busy_seconds"])
        idle = _fmt_duration(m["idle_seconds"])
        boot = (m.get("first_boot") or em)[:19]

        lines.append(
            f"{bot_name:<15} {uptime:>8} {observed:>8} {restarts:>9} "
            f"{mtbr:>10} {busy:>8} {idle:>8} {boot:<20}"
        )
        silent = m.get("restarts_after_silence", 0)
        if silent:
            notes.append(
                f"{bot_name}: {silent} restart{'' if silent == 1 else 's'} not counted:"
                f" {'it' if silent == 1 else 'each'} followed over {_MAX_INTERVAL_SECS // 60} min"
                " with no up sample (a host outage or a recording gap)"
            )

    lines.append(
        "Up (obs) is the share of observed time the session was up; Observed is the share of"
        " the window with a keepalive sample on record. Unobserved time is neither up nor down."
    )
    lines.extend(notes)
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
