"""Fleet health dashboard — ``claudlobby status``.

Aggregates live state from four sources:

1. **fleet-state.json** — canonical bot status (idle/working/blocked/offline)
2. **tmux** — session presence (alive or not)
3. **systemd / launchd** — service supervision state
4. **keepalive.log** — last heartbeat timestamp and pane state (BUSY/IDLE)

Output is a one-screen table for the operator's morning check.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import FleetConfig
from .paths import Paths
from .uptime import _fmt_duration

log = logging.getLogger("claudlobby.status")

# -- Heartbeat age thresholds (seconds) ------------------------------------
_HEARTBEAT_FRESH_SECS = 120  # < 2 min: green, healthy
_HEARTBEAT_WARN_SECS = 600  # < 10 min: default color
_HEARTBEAT_STALE_SECS = 3600  # < 1 hr: yellow warning; beyond = yellow hours

# -- ANSI helpers (disabled when NO_COLOR set or not a tty) ----------------

_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _sgr(code: str, text: str) -> str:
    if not _COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(t: str) -> str:
    return _sgr("32", t)


def _yellow(t: str) -> str:
    return _sgr("33", t)


def _red(t: str) -> str:
    return _sgr("31", t)


def _dim(t: str) -> str:
    return _sgr("2", t)


def _bold(t: str) -> str:
    return _sgr("1", t)


# -- Data collection -------------------------------------------------------


@dataclass
class BotStatus:
    name: str
    # fleet-state.json
    state: str = "unknown"  # idle/working/blocked/offline/unknown
    current_task: str | None = None
    last_completed: str | None = None
    # tmux
    tmux_alive: bool = False
    # systemd/launchd
    service_active: bool = False
    service_sub: str = ""  # e.g. "running", "exited", "dead"
    # keepalive
    last_heartbeat: datetime | None = None
    pane_state: str = ""  # BUSY/IDLE/UNKNOWN
    # utilization (populated by collect_fleet_status)
    busy_pct_today: float | None = None
    idle_since: datetime | None = None
    current_task_age_secs: int | None = None


def _check_tmux_sessions() -> set[str]:
    """Return set of active tmux session names."""
    try:
        out = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return set()
        return {line.strip() for line in out.stdout.splitlines() if line.strip()}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()


def _check_systemd_service(bot_id: str) -> tuple[bool, str]:
    """Check systemd user unit. Returns (active, sub_state)."""
    unit = f"{bot_id}.service"
    try:
        out = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=ActiveState,SubState",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return False, "not-found"
        props = {}
        for line in out.stdout.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                props[k.strip()] = v.strip()
        active = props.get("ActiveState", "") == "active"
        sub = props.get("SubState", "unknown")
        return active, sub
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, "unknown"


def _check_launchd_service(bot_id: str, service_label: str) -> tuple[bool, str]:
    """Check launchd service. Returns (active, sub_state)."""
    try:
        out = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{service_label}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return False, "not-found"
        return True, "running"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, "unknown"


def _parse_keepalive_log(bot_dir: Path) -> tuple[datetime | None, str]:
    """Read last line of keepalive.log. Returns (timestamp, pane_state)."""
    log_path = bot_dir / "keepalive.log"
    if not log_path.is_file():
        return None, ""
    try:
        # Read last 4KB — enough for the last few lines
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", errors="replace")
        lines = [l for l in tail.splitlines() if l.strip()]
        if not lines:
            return None, ""
        last = lines[-1]
        # Format: "2026-05-16T23:06:47-04:00 IDLE — at prompt"
        #      or "2026-05-16T23:06:47-04:00 BUSY — working"
        parts = last.split(None, 2)
        if len(parts) < 2:
            return None, ""
        try:
            ts = datetime.fromisoformat(parts[0])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return None, ""
        pane = parts[1].rstrip(" —\u2014")
        return ts, pane
    except OSError:
        return None, ""


def _read_service_label(bot_dir: Path) -> str:
    """Extract BOT_SERVICE from bot.conf."""
    conf = bot_dir / "bot.conf"
    if not conf.is_file():
        return ""
    try:
        for line in conf.read_text().splitlines():
            if line.startswith("BOT_SERVICE="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def collect_fleet_status(
    fleet: FleetConfig,
    paths: Paths,
) -> list[BotStatus]:
    """Collect status for all bots in the fleet."""
    from .utilization import compute_bot_utilization, load_fleet_state

    state_data = load_fleet_state(paths)
    bots_state = state_data.get("bots", {})
    tmux_sessions = _check_tmux_sessions()
    is_linux = platform.system() == "Linux"
    now = datetime.now(timezone.utc)

    results: list[BotStatus] = []

    for bot_id in fleet.bots:
        bs = BotStatus(name=bot_id)
        bot_dir = paths.bot_runtime(bot_id)

        # fleet-state.json
        if bot_id in bots_state:
            entry = bots_state[bot_id]
            bs.state = entry.get("status", "unknown")
            bs.current_task = entry.get("current_task")
            bs.last_completed = entry.get("last_completed")

        # tmux
        bs.tmux_alive = bot_id in tmux_sessions

        # service supervision
        if is_linux:
            bs.service_active, bs.service_sub = _check_systemd_service(bot_id)
        else:
            label = _read_service_label(bot_dir)
            if label:
                bs.service_active, bs.service_sub = _check_launchd_service(
                    bot_id, label
                )

        # keepalive heartbeat
        bs.last_heartbeat, bs.pane_state = _parse_keepalive_log(bot_dir)

        # utilization (from keepalive logs)
        if bot_dir.is_dir():
            util = compute_bot_utilization(
                bot_id, bot_dir, bots_state.get(bot_id, {}), now=now
            )
            bs.busy_pct_today = util.busy_pct_today
            bs.current_task_age_secs = util.current_task_age_secs
            bs.idle_since = util.idle_since

        results.append(bs)

    return results


# -- Rendering --------------------------------------------------------------


def _health_indicator(bs: BotStatus) -> str:
    """Single-char health: green dot, yellow dot, red dot."""
    if not bs.tmux_alive or not bs.service_active:
        return _red("x")
    if bs.last_heartbeat:
        age = (
            datetime.now(timezone.utc) - bs.last_heartbeat.astimezone(timezone.utc)
        ).total_seconds()
        if age > _HEARTBEAT_WARN_SECS:
            return _yellow("~")
    if bs.state == "blocked":
        return _yellow("!")
    return _green("o")


def _state_display(bs: BotStatus) -> str:
    """Colorized state string."""
    s = bs.state
    if s == "idle":
        return _dim(s)
    if s == "working":
        return _green(s)
    if s == "blocked":
        return _yellow(s)
    if s == "offline":
        return _red(s)
    return s


def _heartbeat_display(bs: BotStatus) -> str:
    """Relative heartbeat age."""
    if bs.last_heartbeat is None:
        return _dim("--")
    age = (
        datetime.now(timezone.utc) - bs.last_heartbeat.astimezone(timezone.utc)
    ).total_seconds()
    if age < _HEARTBEAT_FRESH_SECS:
        return _green(f"{int(age)}s ago")
    if age < _HEARTBEAT_STALE_SECS:
        mins = int(age / 60)
        return f"{mins}m ago" if age < _HEARTBEAT_WARN_SECS else _yellow(f"{mins}m ago")
    hours = int(age / 3600)
    return _yellow(f"{hours}h ago")


def _service_display(bs: BotStatus) -> str:
    if bs.service_active:
        return _green("up")
    if bs.service_sub == "not-found":
        return _dim("--")
    return _red("down")


def _tmux_display(bs: BotStatus) -> str:
    if bs.tmux_alive:
        if bs.pane_state == "BUSY":
            return _green("busy")
        if bs.pane_state == "IDLE":
            return "idle"
        return "up"
    return _red("down")


def _truncate(text: str | None, width: int) -> str:
    if not text:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "\u2026"


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _visible_len(s: str) -> int:
    """Length of string excluding ANSI escape codes."""
    return len(_ANSI_RE.sub("", s))


def _pad(s: str, width: int) -> str:
    """Left-align s to width, accounting for ANSI escapes."""
    pad_needed = width - _visible_len(s)
    return s + " " * max(0, pad_needed)


def _busy_pct_display(bs: BotStatus) -> str:
    """Busy % today."""
    if bs.busy_pct_today is None:
        return _dim("--")
    pct = f"{bs.busy_pct_today:.0f}%"
    if bs.busy_pct_today >= 70:
        return _green(pct)
    if bs.busy_pct_today >= 30:
        return pct
    return _dim(pct)


def _idle_since_display(bs: BotStatus) -> str:
    """Relative idle duration."""
    if bs.idle_since is None:
        return _dim("--")
    age = (
        datetime.now(timezone.utc) - bs.idle_since.astimezone(timezone.utc)
    ).total_seconds()
    if age < 0:
        return _dim("--")
    return _fmt_duration(int(age))


def _task_age_display(bs: BotStatus) -> str:
    """Current task age."""
    if bs.current_task_age_secs is None:
        return _dim("--")
    s = _fmt_duration(bs.current_task_age_secs)
    if bs.current_task_age_secs > 7200:
        return _yellow(s)
    return s


def format_table(statuses: list[BotStatus], fleet_name: str) -> str:
    """Format the status table as a string."""
    lines: list[str] = []

    # Header
    lines.append(_bold(f"Fleet: {fleet_name}"))
    lines.append("")

    if not statuses:
        lines.append("  No bots defined.")
        return "\n".join(lines) + "\n"

    # Column widths
    name_w = max(len(bs.name) for bs in statuses)
    name_w = max(name_w, 4)  # min "NAME" header

    # Table header
    hdr = (
        f"  {'':1}  "
        f"{'NAME':<{name_w}}  "
        f"{'STATE':<8}  "
        f"{'SVC':<4}  "
        f"{'TMUX':<4}  "
        f"{'HEARTBEAT':<10}  "
        f"{'BUSY%':<6}  "
        f"{'IDLE':<8}  "
        f"{'TASK AGE':<8}  "
        f"ACTIVITY"
    )
    lines.append(_dim(hdr))
    lines.append(_dim("  " + "\u2500" * (len(hdr) - 2)))

    for bs in statuses:
        indicator = _health_indicator(bs)
        activity = bs.current_task or bs.last_completed or ""
        activity = _truncate(activity, 40)
        if bs.current_task:
            activity_display = activity
        elif bs.last_completed:
            activity_display = _dim(activity)
        else:
            activity_display = ""

        row = (
            f"  {indicator}  "
            f"{_pad(bs.name, name_w)}  "
            f"{_pad(_state_display(bs), 8)}  "
            f"{_pad(_service_display(bs), 4)}  "
            f"{_pad(_tmux_display(bs), 4)}  "
            f"{_pad(_heartbeat_display(bs), 10)}  "
            f"{_pad(_busy_pct_display(bs), 6)}  "
            f"{_pad(_idle_since_display(bs), 8)}  "
            f"{_pad(_task_age_display(bs), 8)}  "
            f"{activity_display}"
        )
        lines.append(row)

    # Summary
    up_count = sum(1 for bs in statuses if bs.tmux_alive and bs.service_active)
    total = len(statuses)
    working = sum(1 for bs in statuses if bs.state == "working")
    blocked = sum(1 for bs in statuses if bs.state == "blocked")

    lines.append("")
    summary_parts = [f"{up_count}/{total} up"]
    if working:
        summary_parts.append(f"{working} working")
    if blocked:
        summary_parts.append(_yellow(f"{blocked} blocked"))
    lines.append("  " + ", ".join(summary_parts))

    return "\n".join(lines) + "\n"


def format_bot_detail(bs: BotStatus) -> str:
    """Detailed view for a single bot (claudlobby status --bot <name>)."""
    lines: list[str] = []
    lines.append(_bold(bs.name))
    lines.append("")
    lines.append(f"  State:      {_state_display(bs)}")
    lines.append(f"  Service:    {_service_display(bs)} ({bs.service_sub})")
    lines.append(f"  Tmux:       {_tmux_display(bs)}")
    lines.append(f"  Heartbeat:  {_heartbeat_display(bs)}")

    lines.append(f"  Busy today: {_busy_pct_display(bs)}")
    lines.append(f"  Idle since: {_idle_since_display(bs)}")
    lines.append(f"  Task age:   {_task_age_display(bs)}")

    if bs.current_task:
        lines.append(f"  Task:       {bs.current_task}")
    if bs.last_completed:
        lines.append(f"  Last:       {bs.last_completed}")

    return "\n".join(lines) + "\n"


def format_json(statuses: list[BotStatus], fleet_name: str) -> str:
    """JSON output for scripting."""
    bots = []
    for bs in statuses:
        bots.append(
            {
                "name": bs.name,
                "state": bs.state,
                "service_active": bs.service_active,
                "service_sub": bs.service_sub,
                "tmux_alive": bs.tmux_alive,
                "last_heartbeat": (
                    bs.last_heartbeat.isoformat() if bs.last_heartbeat else None
                ),
                "pane_state": bs.pane_state or None,
                "busy_pct_today": bs.busy_pct_today,
                "idle_since": (bs.idle_since.isoformat() if bs.idle_since else None),
                "current_task_age_secs": bs.current_task_age_secs,
                "current_task": bs.current_task,
                "last_completed": bs.last_completed,
            }
        )
    return json.dumps({"fleet": fleet_name, "bots": bots}, indent=2) + "\n"
