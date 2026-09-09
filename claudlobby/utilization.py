"""Worker utilization rollup — busy/idle % per bot over rolling windows.

The busy/idle series is the plane's ``bot.heartbeat`` samples (F18 closure
R2b: ``claudlobby.plane.utilization.heartbeat_series`` is the ONE reader,
shared with the operator plane's surface; keepalive.log is gone), joined with
fleet-state.json for the declared status and current task. No new data
collection — pure aggregation of recorded samples.

Two access paths, deliberately distinct:

1. ``claudlobby status`` calls ``compute_bot_utilization`` directly, recomputing
   at display time to render its BUSY%/IDLE/TASK-AGE columns. It does NOT read
   the JSON below — its numbers never depend on that file being fresh.
2. ``fleet-utilization.sh`` persists the rollup to
   ``state/fleet-utilization.json`` (``--summary`` emits a one-line digest).
   That file is the intended feed for a manager-bot dispatch consumer that is
   not yet wired; until that consumer exists the writer stays on-demand — no
   timer schedules it and nothing reads the file.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .paths import Paths
from .uptime import _MAX_INTERVAL_SECS, _fmt_duration

log = logging.getLogger("claudlobby.utilization")

_STALL_THRESHOLD_SECS = int(os.environ.get("UTILIZATION_STALL_SECS", "7200"))


class PlaneUnreachable(RuntimeError):
    """The plane cannot answer (no db, unreadable, no schema): the rollup
    refuses rather than rendering every bot 0% busy — unreachable is not
    empty (#1216)."""


def fleet_heartbeat_series(conn, fleet: str, now: datetime) -> dict[str, list[tuple[datetime, str]]]:
    """``{bot name (lower-cased): [(instant, state), ...]}`` for one fleet from
    the plane — the alias's tail is the bot's name, matched case-insensitively
    like every per-bot plane read (a case-variant alias mints a second
    identity; the series are merged in time order)."""
    from .plane.utilization import heartbeat_series      # lazy: that module imports the math from here
    out: dict[str, list[tuple[datetime, str]]] = {}
    for alias, entries in heartbeat_series(conn, now=now, fleet=fleet).items():
        out.setdefault(alias.rsplit("/", 1)[-1].lower(), []).extend(entries)
    for entries in out.values():
        entries.sort(key=lambda e: e[0])
    return out


@dataclass
class BotUtilization:
    """Per-bot utilization metrics."""

    name: str
    busy_pct_24h: float = 0.0
    busy_pct_7d: float = 0.0
    idle_since: datetime | None = None
    current_task_age_secs: int | None = None
    current_task: str | None = None
    state: str = "unknown"
    stall: bool = False


def _compute_busy_pct(
    entries: list[tuple[datetime, str]],
    window: timedelta,
    now: datetime,
) -> float:
    """Compute busy % from keepalive entries over a time window.

    Busy % = BUSY seconds / (BUSY + IDLE seconds). Excludes downtime
    (gaps > 10 min) and UNKNOWN/RESTART from both numerator and denominator
    so the metric reflects how the bot spends its *up* time.
    """
    cutoff = now - window
    windowed = [(ts, state) for ts, state in entries if ts >= cutoff]
    if not windowed:
        return 0.0

    busy_secs = 0.0
    idle_secs = 0.0

    for i, (ts, state) in enumerate(windowed):
        if i + 1 < len(windowed):
            duration = (windowed[i + 1][0] - ts).total_seconds()
        else:
            duration = (now - ts).total_seconds()
        # cap a gap at 10 min of the preceding state (pinned legacy choice), and
        # CLAMP a negative duration to 0: a log never produced one, but a
        # time-skewed or out-of-order sample series can, and a negative
        # denominator renders as -50% / 300% (plane gauntlet, probed)
        duration = max(0.0, min(duration, _MAX_INTERVAL_SECS))

        if state == "BUSY":
            busy_secs += duration
        elif state == "IDLE":
            idle_secs += duration

    total = busy_secs + idle_secs
    if total == 0:
        return 0.0
    return round((busy_secs / total) * 100, 1)


def _find_state_transition(
    entries: list[tuple[datetime, str]],
    target_state: str,
) -> datetime | None:
    """Find the start of the current run of ``target_state`` at the tail.

    Walks backward from the end of entries. If the last entry matches
    ``target_state``, keeps walking back while entries match. Returns
    the timestamp of the first entry in the contiguous run, or None if
    the last entry doesn't match ``target_state``.
    """
    if not entries:
        return None
    if entries[-1][1] != target_state:
        return None

    transition_ts = entries[-1][0]
    for i in range(len(entries) - 2, -1, -1):
        if entries[i][1] != target_state:
            break
        transition_ts = entries[i][0]
    return transition_ts


def load_fleet_state(paths: Paths) -> dict:
    """Read fleet-state.json. Returns empty dict on missing/corrupt."""
    state_path = Path(
        os.environ.get(
            "FLEET_STATE_PATH",
            str(paths.root / "state" / "fleet-state.json"),
        )
    )
    if not state_path.is_file():
        return {}
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def compute_bot_utilization(
    bot_name: str,
    entries: list[tuple[datetime, str]],
    fleet_state_bot: dict,
    now: datetime | None = None,
) -> BotUtilization:
    """Compute utilization for a single bot from its (instant, state) series
    (the plane's, via ``fleet_heartbeat_series``; empty = no sample recorded)."""
    if now is None:
        now = datetime.now(timezone.utc)

    entries = [
        (
            ts.astimezone(timezone.utc)
            if ts.tzinfo
            else ts.replace(tzinfo=timezone.utc),
            state,
        )
        for ts, state in entries
    ]

    busy_today = _compute_busy_pct(entries, timedelta(hours=24), now)
    busy_7d = _compute_busy_pct(entries, timedelta(days=7), now)

    state = fleet_state_bot.get("status", "unknown")
    current_task = fleet_state_bot.get("current_task")

    idle_since = None
    if entries and entries[-1][1] == "IDLE":
        idle_since = _find_state_transition(entries, "IDLE")

    current_task_age_secs = None
    if entries and entries[-1][1] == "BUSY":
        ts = _find_state_transition(entries, "BUSY")
        if ts:
            current_task_age_secs = int((now - ts).total_seconds())

    stall = (
        current_task_age_secs is not None
        and current_task_age_secs > _STALL_THRESHOLD_SECS
    )

    return BotUtilization(
        name=bot_name,
        busy_pct_24h=busy_today,
        busy_pct_7d=busy_7d,
        idle_since=idle_since,
        current_task_age_secs=current_task_age_secs,
        current_task=current_task,
        state=state,
        stall=stall,
    )


def compute_fleet_utilization(
    bots_dir: Path,
    paths: Paths,
    bot_names: list[str] | None = None,
    now: datetime | None = None,
    *,
    fleet: str | None = None,
) -> list[BotUtilization]:
    """Compute utilization for all bots (or a subset) in a fleet, from the
    plane. *fleet* names the plane's rows (default: the overlay's name); a
    plane that cannot answer raises ``PlaneUnreachable`` — never a rollup of
    zeros."""
    if now is None:
        now = datetime.now(timezone.utc)
    from .brief import plane_session
    plane, note = plane_session(paths, fleet=fleet)     # the ONE plane door (root mode resolves its fleet)
    if plane is None:
        raise PlaneUnreachable(note)
    try:
        series = fleet_heartbeat_series(plane.conn, plane.fleet, now)
    except Exception as exc:                          # a schema the reader cannot use: refuse
        raise PlaneUnreachable(f"the plane could not answer: {exc}") from exc
    finally:
        plane.close()

    fleet_state = load_fleet_state(paths)
    bots_state = fleet_state.get("bots", {})

    results: list[BotUtilization] = []
    if bot_names is None:
        bot_names = (
            sorted(
                d.name
                for d in bots_dir.iterdir()
                if d.is_dir() and (d / "bot.conf").is_file()
            )
            if bots_dir.is_dir()
            else []
        )

    for name in bot_names:
        util = compute_bot_utilization(name, series.get(name.lower(), []),
                                       bots_state.get(name, {}), now=now)
        results.append(util)

    return results


def write_utilization_json(
    results: list[BotUtilization],
    paths: Paths,
    now: datetime | None = None,
) -> Path:
    """Write fleet-utilization.json to the state directory."""
    if now is None:
        now = datetime.now(timezone.utc)

    state_dir = paths.runtime / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    out_path = state_dir / "fleet-utilization.json"

    data: dict = {
        "updated": now.isoformat(),
        "bots": {},
    }
    for u in results:
        data["bots"][u.name] = {
            "busy_pct_24h": u.busy_pct_24h,
            "busy_pct_7d": u.busy_pct_7d,
            "idle_since": u.idle_since.isoformat() if u.idle_since else None,
            "current_task_age_secs": u.current_task_age_secs,
            "current_task": u.current_task,
            "state": u.state,
            "stall": u.stall,
        }

    out_path.write_text(json.dumps(data, indent=2) + "\n")
    return out_path


def format_utilization_summary(results: list[BotUtilization]) -> str:
    """One-line fleet summary for Telegram digest."""
    parts: list[str] = []
    for u in results:
        if u.state == "working" and u.current_task_age_secs is not None:
            age = _fmt_duration(u.current_task_age_secs)
            parts.append(f"{u.name} heads-down {age}")
        elif u.idle_since:
            idle_secs = (datetime.now(timezone.utc) - u.idle_since).total_seconds()
            parts.append(f"{u.name} idle {_fmt_duration(int(idle_secs))}")
        else:
            parts.append(f"{u.name} {int(u.busy_pct_24h)}% busy")
    return "team utilization: " + ", ".join(parts) if parts else "no bots"


# Public names for the ONE definition of busy/idle math, so the plane's
# heartbeat-series reader (claudlobby.plane.utilization) imports a stable
# contract rather than a private helper an unwitting rename could break.
compute_busy_pct = _compute_busy_pct
find_state_transition = _find_state_transition
