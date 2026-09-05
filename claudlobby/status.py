"""Fleet health dashboard — ``claudlobby status``.

Aggregates live state from four sources:

1. **fleet-state.json** — canonical bot status (idle/working/blocked/offline)
2. **tmux** — session presence (alive or not)
3. **systemd / launchd** — service supervision state
4. **the plane** — the newest ``bot.heartbeat`` sample per bot (last heartbeat
   + pane state, BUSY/IDLE) and the heartbeat series behind the utilization
   columns (F18 closure R2b; keepalive.log is gone). A plane that cannot answer
   renders those columns ``unknown``, flags health ``?`` and says why under the
   table — never blank-as-healthy.

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
from .paths import Paths, tmux_socket_for_bot
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

# SubState sentinel for "nobody got an answer" — the supervisor was unreachable,
# too slow, or never asked. Whether the unit is up is UNKNOWN, not false, and it
# is rendered as a third state rather than as a failure (#1044).
#
# Deliberately NOT the string "unknown": systemd reports a literal SubState of
# "unknown" from a call that SUCCEEDED, and conflating a real answer with the
# absence of one is the inverse of this bug.
_SVC_UNDETERMINED = "undetermined"


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
    # Defaults to the sentinel, not "": collect_fleet_status runs neither check
    # on a host that is not Linux and has no service label, and "we never asked"
    # must not render as "we asked and it is down".
    service_sub: str = _SVC_UNDETERMINED  # e.g. "running", "exited", "dead"
    # the plane's newest heartbeat sample
    last_heartbeat: datetime | None = None
    pane_state: str = ""  # BUSY/IDLE/UNKNOWN
    # why the plane could not answer — heartbeat, pane state and utilization
    # are then UNKNOWN, not absent; "" when it answered
    plane_unreachable: str = ""
    # utilization (populated by collect_fleet_status)
    busy_pct_24h: float | None = None
    idle_since: datetime | None = None
    current_task_age_secs: int | None = None

    @property
    def service_undetermined(self) -> bool:
        """True when nothing ever answered, so up-or-down is not known."""
        return self.service_sub == _SVC_UNDETERMINED


def _check_tmux_sessions(fleet, paths) -> set[str]:
    """Live bot session names across every per-bot tmux server.

    Each bot runs its own server (``tmux -L <socket>``, socket == BOT_SERVICE),
    so a single global ``tmux ls`` is blind to every other socket. Query each
    declared bot's own socket and union the live sessions. Relies on the pinned
    TMUX_TMPDIR (=/tmp, tmux's own default) so a socket name resolves to the
    server start-bot.sh created it on.
    """
    alive: set[str] = set()
    for bot_id in fleet.bots:
        try:
            socket = tmux_socket_for_bot(paths.bot_runtime(bot_id)) or (
                f"{fleet.service_prefix}.{bot_id}"
            )
        except ValueError:
            # SSOT resolver fail-fasts on a misconfigured bot when FLEET_NAME is
            # set; fall back to the canonical service-name socket so one bad bot
            # doesn't blank the whole dashboard.
            socket = f"{fleet.service_prefix}.{bot_id}"
        try:
            out = subprocess.run(
                ["tmux", "-L", socket, "has-session", "-t", bot_id],
                capture_output=True,
                timeout=5,
            )
            if out.returncode == 0:
                alive.add(bot_id)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return alive


def _check_systemd_service(bot_id: str, service_label: str = "") -> tuple[bool, str]:
    """Check systemd user unit. Returns (active, sub_state).

    Keys off the BOT_SERVICE label the installer names the unit after
    (``com.<fleet>.<bot>.service``), mirroring the launchd path; falls back to
    the bare bot id only for a pre-BOT_SERVICE bot.conf.
    """
    label = service_label or bot_id
    unit = f"{label}.service"
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
        # The 5s timeout expires under host load — exactly when this dashboard
        # gets read.
        return False, _SVC_UNDETERMINED


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
        return False, _SVC_UNDETERMINED


def _latest_heartbeats(conn, fleet_name: str) -> dict[str, tuple[datetime, str]]:
    """``{bot name (lower-cased): (instant, BUSY|IDLE|UNKNOWN)}`` — the plane's
    NEWEST ``bot.heartbeat`` sample per instance of *fleet_name*, through the
    presence derivation's own query (``LATEST_HEARTBEAT_SQL``: newest by
    ledger order, stamped with its ``ingested_at`` — the freshness clock
    presence reads, robust to a producer's skewed clock)."""
    from .plane.queries import LATEST_HEARTBEAT_SQL
    prefix = f"bot:{fleet_name}/".lower()
    out: dict[str, tuple[datetime, str]] = {}
    for row in conn.execute(LATEST_HEARTBEAT_SQL):
        alias = str(row["alias"] or "")
        if not alias.lower().startswith(prefix):
            continue
        try:
            ts = datetime.fromisoformat(str(row["ingested_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        raw = row["value"]
        try:
            state = (json.loads(raw) if isinstance(raw, str) else raw or {}).get("state")
        except (ValueError, AttributeError):
            state = None
        out[alias[len(prefix):].lower()] = (ts, state if isinstance(state, str) else "UNKNOWN")
    return out


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
    from .plane.db import open_ro
    from .utilization import compute_bot_utilization, fleet_heartbeat_series, load_fleet_state

    state_data = load_fleet_state(paths)
    bots_state = state_data.get("bots", {})
    tmux_sessions = _check_tmux_sessions(fleet, paths)
    is_linux = platform.system() == "Linux"
    now = datetime.now(timezone.utc)

    # The plane, opened ONCE: the newest heartbeat per bot (heartbeat + pane
    # state) and the heartbeat series (utilization). Unreachable is not
    # empty — every bot then carries the reason and its columns say unknown.
    heartbeats: dict[str, tuple[datetime, str]] = {}
    series: dict[str, list[tuple[datetime, str]]] = {}
    conn, plane_unreachable = open_ro(paths.root)
    if conn is not None:
        try:
            heartbeats = _latest_heartbeats(conn, fleet.name)
            series = fleet_heartbeat_series(conn, fleet.name, now)
        except Exception as exc:                 # a schema the reader cannot use: say so, never blank
            plane_unreachable = f"the plane could not answer: {exc}"
        finally:
            conn.close()
    plane_unreachable = plane_unreachable or ""

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

        # service supervision — key off the BOT_SERVICE label the installer
        # names the unit after (com.<fleet>.<bot>), not the bare bot id.
        label = _read_service_label(bot_dir)
        if is_linux:
            bs.service_active, bs.service_sub = _check_systemd_service(bot_id, label)
        elif label:
            bs.service_active, bs.service_sub = _check_launchd_service(bot_id, label)

        # the plane: heartbeat + pane state, then utilization over the series
        bs.plane_unreachable = plane_unreachable
        if not plane_unreachable:
            bs.last_heartbeat, bs.pane_state = heartbeats.get(bot_id.lower(), (None, ""))
            util = compute_bot_utilization(
                bot_id, series.get(bot_id.lower(), []), bots_state.get(bot_id, {}), now=now
            )
            bs.busy_pct_24h = util.busy_pct_24h
            bs.current_task_age_secs = util.current_task_age_secs
            bs.idle_since = util.idle_since

        results.append(bs)

    return results


# -- Rendering --------------------------------------------------------------


def _health_indicator(bs: BotStatus) -> str:
    """Single-char health: o healthy, ~ stale, ! blocked, x down, ? not known."""
    if not bs.tmux_alive:
        return _red("x")
    if not bs.service_active:
        # Same precedence as _service_display: a confirmed-active service is
        # never undetermined, so the sentinel is only consulted once we know
        # the unit was not reported active.
        return _yellow("?") if bs.service_undetermined else _red("x")
    if bs.plane_unreachable:
        # up by tmux and the service, but the recorded half is unreadable:
        # NOT known healthy (a plane outage read as an all-green fleet is the
        # founding gap of #1361)
        return _yellow("?")
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
    if bs.plane_unreachable:
        return _yellow("unknown")
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
    """Four states, none mistakable for another at a glance, colour or not.

    green up / dim -- not enrolled / yellow ? not known / red down.
    """
    if bs.service_active:
        return _green("up")
    if bs.service_sub == "not-found":
        return _dim("--")
    if bs.service_undetermined:
        return _yellow("?")
    return _red("down")


def _tmux_display(bs: BotStatus) -> str:
    if bs.tmux_alive:
        if bs.plane_unreachable:
            return "up?"        # alive, pane verdict unknown (the plane is unreachable)
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
    if bs.plane_unreachable:
        return _yellow("?")
    if bs.busy_pct_24h is None:
        return _dim("--")
    pct = f"{bs.busy_pct_24h:.0f}%"
    if bs.busy_pct_24h >= 70:
        return _green(pct)
    if bs.busy_pct_24h >= 30:
        return pct
    return _dim(pct)


def _idle_since_display(bs: BotStatus, now: datetime) -> str:
    """Relative idle duration."""
    if bs.plane_unreachable:
        return _yellow("?")
    if bs.idle_since is None:
        return _dim("--")
    age = (now - bs.idle_since.astimezone(timezone.utc)).total_seconds()
    if age < 0:
        return _dim("--")
    return _fmt_duration(int(age))


def _task_age_display(bs: BotStatus) -> str:
    """Current task age."""
    if bs.plane_unreachable:
        return _yellow("?")
    if bs.current_task_age_secs is None:
        return _dim("--")
    s = _fmt_duration(bs.current_task_age_secs)
    if bs.current_task_age_secs > 7200:
        return _yellow(s)
    return s


def format_table(statuses: list[BotStatus], fleet_name: str) -> str:
    """Format the status table as a string."""
    now = datetime.now(timezone.utc)
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
            f"{_pad(_idle_since_display(bs, now), 8)}  "
            f"{_pad(_task_age_display(bs), 8)}  "
            f"{activity_display}"
        )
        lines.append(row)

    # Summary
    up_count = sum(1 for bs in statuses if bs.tmux_alive and bs.service_active)
    total = len(statuses)
    working = sum(1 for bs in statuses if bs.state == "working")
    blocked = sum(1 for bs in statuses if bs.state == "blocked")
    undetermined = sum(1 for bs in statuses if bs.service_undetermined)

    lines.append("")
    why = next((bs.plane_unreachable for bs in statuses if bs.plane_unreachable), "")
    if why:
        lines.append(_yellow(f"  heartbeat, pane state and utilization: unknown — the plane is"
                             f" unreachable ({why}); restore state/plane/plane.db or name the"
                             " right root"))
        lines.append("")
    summary_parts = [f"{up_count}/{total} up"]
    if undetermined:
        # Without this the shortfall in "N/M up" reads as N-M bots being DOWN.
        summary_parts.append(_yellow(f"{undetermined} undetermined"))
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
    lines.append(f"  Heartbeat:  {_heartbeat_display(bs)}"
                 + (f" (plane unreachable: {bs.plane_unreachable})" if bs.plane_unreachable else ""))

    lines.append(f"  Busy 24h:   {_busy_pct_display(bs)}")
    now = datetime.now(timezone.utc)
    lines.append(f"  Idle since: {_idle_since_display(bs, now)}")
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
                # Explicit: the obvious scripted read is `if not
                # service_active`, which is the same collapse this fixes.
                "service_undetermined": bs.service_undetermined,
                "tmux_alive": bs.tmux_alive,
                "last_heartbeat": (
                    bs.last_heartbeat.isoformat() if bs.last_heartbeat else None
                ),
                "pane_state": bs.pane_state or None,
                # the reason heartbeat/pane/utilization are unknown, else null
                "plane_unreachable": bs.plane_unreachable or None,
                "busy_pct_24h": bs.busy_pct_24h,
                "idle_since": (bs.idle_since.isoformat() if bs.idle_since else None),
                "current_task_age_secs": bs.current_task_age_secs,
                "current_task": bs.current_task,
                "last_completed": bs.last_completed,
            }
        )
    return json.dumps({"fleet": fleet_name, "bots": bots}, indent=2) + "\n"
