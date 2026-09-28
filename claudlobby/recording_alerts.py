"""Best-effort, recording-independent alert for an ordinary message write failure.

The public message operation owns when to call this adapter.  This module does
not inspect Plane, record a message, retry one, or discover an authoring fleet.
The selected native package owns target pairing, debounce and the carriers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import os
import re
import stat
import subprocess
import sys
import time
from typing import Literal, Mapping
from uuid import UUID

from .context import Context
from .message_transport import TransportDestination, _StartedFailure, _run
from .resources import PackageResources


ChannelStatus = Literal["submitted", "carrier_accepted", "suppressed", "failed", "unconfigured"]
_TIER_KEYS = frozenset({"FLEET_PULSE_ESCALATION_CHAT_ID", "FLEET_PULSE_ESCALATION_STATE_DIR",
                        "TELEGRAM_GROUP_CHAT_ID", "TELEGRAM_STATE_DIR", "TELEGRAM_BOT_TOKEN",
                        "FLEET_PULSE_RENOTIFY_AFTER_S", "FLEET_PULSE_REARM_WINDOW_S"})
_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"


@dataclass(frozen=True)
class ChannelOutcome:
    status: ChannelStatus
    debounce_available: bool | None  # None means the bounded child never proved availability.
    reason: str | None = None


@dataclass(frozen=True)
class RecordingAlertOutcome:
    manager: ChannelOutcome
    telegram: ChannelOutcome


@dataclass(frozen=True)
class RecordingAlertClear:
    manager: bool
    telegram: bool


# The only stdout is a deliberately small protocol line.  Native output is
# contained because tg-post can echo carrier errors (and sometimes secrets).
# A failed persistence probe never blocks the alert: the selected carrier is
# attempted once without a debounce marker, and the result reports that no
# durable suppression window was available. Python holds the episode lock.
_NOTIFY = r'''
set -u
umask 077
. "$1" >/dev/null 2>&1 || exit 4
state="$2"; fleet="$3"; channel="$4"; bots="$5"; socket="$6"; session="$7"
renotify="$8"; tg_post="$9"
message=''; IFS= read -r -d '' message || :
_send_manager() { bot_tmux_send "$socket" "=$session" "$1" >/dev/null 2>&1; }
_send_telegram() {
    TELEGRAM_GROUP_CHAT_ID="$_alert_chat_id" TELEGRAM_STATE_DIR="$_alert_state_dir" \
        TELEGRAM_BOT_TOKEN="$_alert_token" "$tg_post" "$1" >/dev/null 2>&1
}
recipient=''; sender=''
if [ "$channel" = manager ]; then
    instance=$(bot_tmux "$socket" display-message -p -t "=$session" \
        '#{session_created}-#{pane_pid}' 2>/dev/null) || instance=''
    if [ -n "$instance" ]; then
        recipient="${socket}:${session}:${instance}"
        sender=_send_manager
    else
        printf 'recording-alert-v1\tfailed\t?\n'
        exit 0
    fi
else
    # Only explicit complete pairs reach the resolver.  Its fallback scans
    # mutable bot.conf/fleet.yaml, which is not an active Context input.
    if { [ -n "${FLEET_PULSE_ESCALATION_CHAT_ID:-}" ] &&
         [ -n "${FLEET_PULSE_ESCALATION_STATE_DIR:-}" ]; } ||
       { [ -z "${FLEET_PULSE_ESCALATION_CHAT_ID:-}" ] &&
         [ -n "${TELEGRAM_GROUP_CHAT_ID:-}" ] &&
         [ -n "${TELEGRAM_STATE_DIR:-}" ]; }; then
        resolve_alert_target "$bots" fleet >/dev/null 2>&1
        if [ -n "$_alert_chat_id" ] && [ -n "$_alert_state_dir" ]; then
            recipient=$(sha256_prefixed "${_alert_chat_id}|${_alert_state_dir}" 2>/dev/null) || recipient=''
            if [ -n "$recipient" ]; then sender=_send_telegram; fi
        fi
    fi
fi
if [ -z "$sender" ]; then
    printf 'recording-alert-v1\tunconfigured\t?\n'
    exit 0
fi
send_rc=1
_notify() {
    if "$sender" "$1"; then send_rc=0; return 0; fi
    send_rc=1; return 1
}
available=0
if [ "${ALERT_DEBOUNCE_DISABLED:-0}" != 1 ] && mkdir -p "$state" 2>/dev/null &&
   probe=$(mktemp "$state/.probe.XXXXXXXX" 2>/dev/null); then
    rm -f "$probe"; available=1
    debounce_notify "$state" "$fleet" "recording_degraded_${channel}" _notify \
        "$message" "$recipient" "$renotify" >/dev/null 2>&1 || :
    marker="$state/${fleet}.recording_degraded_${channel}"
    if [ "${_DEBOUNCE_FIRED:-0}" = 1 ] && [ "$send_rc" -eq 0 ] && [ ! -f "$marker" ]; then
        available=0
    fi
    if [ "${_DEBOUNCE_FIRED:-0}" = 0 ]; then
        verdict=suppressed
    elif [ "$send_rc" -eq 0 ]; then
        if [ "$channel" = manager ]; then verdict=submitted; else verdict=carrier_accepted; fi
    else
        verdict=failed
    fi
else
    # Lock/state unavailable: no unsynchronized marker read or write.
    if _notify "$message"; then
        if [ "$channel" = manager ]; then verdict=submitted; else verdict=carrier_accepted; fi
    else
        verdict=failed
    fi
fi
printf 'recording-alert-v1\t%s\t%s\n' "$verdict" "$available"
'''

_CLEAR = r'''
set -u
. "$1" >/dev/null 2>&1 || exit 4
state="$2"; fleet="$3"; channel="$4"
if debounce_clear "$state" "$fleet" "recording_degraded_${channel}" >/dev/null 2>&1; then
    printf 'recording-alert-clear-v1\t0\n'
else
    printf 'recording-alert-clear-v1\t1\n'
fi
'''


def _validate(context: Context, package: PackageResources, manager: TransportDestination) -> None:
    if not isinstance(context, Context) or not isinstance(package, PackageResources):
        raise ValueError("active Context and selected PackageResources required")
    if not isinstance(manager, TransportDestination):
        raise ValueError("frozen manager destination required")
    if context.paths.seed or context.paths.root != manager.root or context.fleet.name != manager.fleet:
        raise ValueError("manager destination differs from active fleet context")
    expected = context.fleet.manager
    if manager.session != expected or manager.socket != f"{context.fleet.service_prefix}.{expected}":
        raise ValueError("manager destination differs from active fleet manager")
    if context.paths.package != package:
        raise ValueError("selected package differs from active context")


def _environment(context: Context, manager: TransportDestination,
                 trusted_tiers: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(trusted_tiers, Mapping) or any(
        key not in _TIER_KEYS or not isinstance(value, str) or "\0" in value
        for key, value in trusted_tiers.items()
    ):
        raise ValueError("alert tier values must be explicitly resolved and allowlisted")
    env = {"PATH": _PATH, "LC_ALL": "C", "CLAUDLOBBY_ROOT": str(manager.root),
           "FLEET_NAME": manager.fleet, "TMUX_TMPDIR": str(manager.tmux_tmpdir),
           "TMPDIR": str(manager.tmux_tmpdir), "HOME": str(manager.root / "state/recording-alert-home"),
           "PLANE_EMIT_DISABLED": "1", "PLANE_MSG_ID": "", "PANE_SEND_VERIFY_TICKS": "0"}
    env.update(trusted_tiers)
    return env


def _acquire_episode_lock(state, fleet: str, channel: str, deadline: float) -> tuple[int | None, str]:
    """Hold one kernel lock for the entire native decision and send.

    An unavailable lock means persistence is untrustworthy and the caller may
    attempt a single unmarked alert. Contention expiry is different: another
    writer may already be sending, so the caller must send nothing.
    """
    fd = None
    try:
        if state.parent.is_symlink() or state.is_symlink():
            raise OSError("redirected alert state")
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not state.is_dir():
            raise OSError("alert state is not a directory")
        path = state / f"{fleet}.recording_degraded_{channel}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise OSError("alert lock is not owned private state")
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd, "acquired"
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    os.close(fd)
                    return None, "contended"
                time.sleep(min(0.025, remaining))
    except OSError:
        if fd is not None:
            os.close(fd)
        return None, "unavailable"


def _run_channel(command: list[str], *, env: dict[str, str], timeout: float,
                 payload: bytes, runner) -> ChannelOutcome:
    try:
        result = (runner or _run)(command, input=payload, env=env, timeout=timeout)
    except (OSError, subprocess.SubprocessError, _StartedFailure):
        return ChannelOutcome("failed", None)
    if result.returncode != 0:
        return ChannelOutcome("failed", None)
    match = re.fullmatch(rb"recording-alert-v1\t"
                         rb"(submitted|carrier_accepted|suppressed|failed|unconfigured)\t([01?])\n?",
                         result.stdout)
    if match is None:
        return ChannelOutcome("failed", None)
    available = {b"1": True, b"0": False, b"?": None}[match.group(2)]
    return ChannelOutcome(match.group(1).decode(), available)


def notify_recording_degraded(
    context: Context, package: PackageResources, manager: TransportDestination, *,
    request_id: str, component: str, at: datetime, trusted_tiers: Mapping[str, str],
    timeout_per_channel: float = 8, runner=None,
) -> RecordingAlertOutcome:
    """Page once per fleet/channel episode; caller keeps the ordinary result.

    ``trusted_tiers`` contains only values already resolved by the operation
    owner for this active fleet.  No ambient bot identity, token, chat or shell
    startup setting crosses into the native process.  Neither channel depends
    on the other's result; each gets its own bounded process-group lifetime.
    """
    _validate(context, package, manager)
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("canonical request UUID required")
    if not isinstance(component, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", component):
        raise ValueError("literal component required")
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("aware alert time required")
    if isinstance(timeout_per_channel, bool) or not 0 < timeout_per_channel <= 30:
        raise ValueError("channel timeout must be in (0, 30]")
    env = _environment(context, manager, trusted_tiers)
    raw_renotify = env.get("FLEET_PULSE_RENOTIFY_AFTER_S", "21600")
    raw_rearm = env.get("FLEET_PULSE_REARM_WINDOW_S", "1800")
    if any(not re.fullmatch(r"[0-9]{1,10}", value) or int(value) > 2_147_483_647
           for value in (raw_renotify, raw_rearm)):
        raise ValueError("renotify interval must be nonnegative seconds")
    def disclose(channel: str, outcome: ChannelOutcome) -> None:
        if outcome.status in ("failed", "unconfigured") or not outcome.debounce_available:
            print(f"recording-alert: root={context.paths.root} fleet={context.fleet.name} "
                  f"time={at.astimezone(timezone.utc).isoformat(timespec='seconds')} "
                  f"component={component} request={request_id} channel={channel} "
                  f"status={outcome.status} debounce_available="
                  f"{int(outcome.debounce_available) if outcome.debounce_available is not None else 'unknown'}"
                  f" reason={outcome.reason or 'none'}",
                  file=sys.stderr)
    native = package.native / "lib-common.sh"
    if not native.is_file():
        failed = ChannelOutcome("failed", None)
        disclose("manager", failed)
        disclose("telegram", failed)
        return RecordingAlertOutcome(failed, failed)
    component_label = component.replace("_", " ").replace("-", " ")
    message = (f"FLEET ALERT: {component_label} persistence could not be confirmed; "
               f"root={context.paths.root} fleet={context.fleet.name} "
               f"time={at.astimezone(timezone.utc).isoformat(timespec='seconds')} "
               f"component={component} request={request_id}. Inspect the request before retrying.")
    state = manager.root / "state/recording-alerts"
    base = ["/bin/bash", "--noprofile", "--norc", "-c", _NOTIFY, "recording-alert",
            str(native), str(state), context.fleet.name]
    outcomes = []
    for channel in ("manager", "telegram"):
        deadline = time.monotonic() + timeout_per_channel
        fd, lock_state = _acquire_episode_lock(state, context.fleet.name, channel, deadline)
        if lock_state == "contended":
            outcome = ChannelOutcome("failed", None, "lock_timeout")
            disclose(channel, outcome)
            outcomes.append(outcome)
            continue
        command = [*base, channel, str(context.paths.runtime_bots), manager.socket,
                   manager.session, raw_renotify, str(package.native / "tg-post.sh")]
        channel_env = dict(env)
        if lock_state == "unavailable":
            channel_env["ALERT_DEBOUNCE_DISABLED"] = "1"
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = ChannelOutcome("failed", None, "deadline")
            else:
                outcome = _run_channel(command, env=channel_env, timeout=remaining,
                                       payload=message.encode() + b"\0", runner=runner)
                if lock_state == "unavailable":
                    outcome = ChannelOutcome(outcome.status, False, "state_unavailable")
        finally:
            if fd is not None:
                os.close(fd)
        disclose(channel, outcome)
        outcomes.append(outcome)
    return RecordingAlertOutcome(*outcomes)


def clear_recording_degraded(context: Context, package: PackageResources,
                             manager: TransportDestination, *, request_id: str,
                             component: str, at: datetime, timeout: float = 5,
                             runner=None) -> RecordingAlertClear:
    """Clear both markers only after the caller confirms an ordinary write."""
    _validate(context, package, manager)
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("canonical request UUID required")
    if not isinstance(component, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", component):
        raise ValueError("literal component required")
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("aware alert time required")
    def disclose(channel: str, reason: str):
        print(f"recording-alert-clear: root={context.paths.root} fleet={context.fleet.name} "
              f"time={at.astimezone(timezone.utc).isoformat(timespec='seconds')} "
              f"component={component} request={request_id} channel={channel} "
              f"reason={reason}", file=sys.stderr)
    native = package.native / "lib-common.sh"
    if not native.is_file() or isinstance(timeout, bool) or not 0 < timeout <= 30:
        disclose("manager", "native_unavailable")
        disclose("telegram", "native_unavailable")
        return RecordingAlertClear(False, False)
    state = manager.root / "state/recording-alerts"
    env = _environment(context, manager, {})
    cleared = []
    for channel in ("manager", "telegram"):
        deadline = time.monotonic() + timeout
        fd, lock_state = _acquire_episode_lock(state, context.fleet.name, channel, deadline)
        if fd is None:
            disclose(channel, "lock_timeout" if lock_state == "contended" else "state_unavailable")
            cleared.append(False)
            continue
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                success = False
            else:
                command = ["/bin/bash", "--noprofile", "--norc", "-c", _CLEAR,
                           "recording-alert-clear", str(native), str(state),
                           context.fleet.name, channel]
                try:
                    result = (runner or _run)(command, input=b"", env=env, timeout=remaining)
                except (OSError, subprocess.SubprocessError, _StartedFailure):
                    success = False
                else:
                    success = (result.returncode == 0 and
                               re.fullmatch(rb"recording-alert-clear-v1\t0\n?", result.stdout) is not None)
        finally:
            os.close(fd)
        if not success:
            disclose(channel, "clear_failed")
        cleared.append(success)
    return RecordingAlertClear(*cleared)
