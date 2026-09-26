"""Python-wrapped bash test for lib/briefing-trigger.sh (#627 P3).

The composed per-(bot,slot) briefing timer runs
``briefing-trigger.sh <fleet> <bot> <slot>``; it must deliver ``/briefing <slot>``
to the bot's own session via the slash-aware dispatch.sh, defer
(``briefing_deferred``) when the bot is busy or its session is absent and retry
at its next idle check inside the window, emit the right fleet event in each
case, and send exactly one ``briefing_missed`` FLEET NOTICE for a slot that is
missed (#1826).

Like test_dispatch_slash.py, this copies the real script next to stub helpers so
it exercises the real control flow without a live tmux server. The event type
and the dispatched payload are captured to files the assertions read.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TRIGGER = REPO / "lib" / "briefing-trigger.sh"
LIB_COMMON = REPO / "lib" / "lib-common.sh"

# Stub lib-common: every helper briefing-trigger.sh sources, reduced to a
# controllable no-tmux shim. Return codes are driven by env so each test steers
# the busy / session-alive / dispatch-result branches.
STUB_LIB_COMMON = """\
#!/bin/bash
install_error_trap() { :; }
resolve_bots_dir() { printf '%s' "$FAKE_BOTS_DIR"; }
setup_log_dir() { mkdir -p "$(dirname "$1")" 2>/dev/null || true; }
ts_iso() { printf '%s' "2026-07-16T00:00:00Z"; }
tmux_socket_for_bot() { printf '%s' "fakesock"; }
check_tmux_session() { return "${STUB_SESSION_RC:-0}"; }
# $BUSY_ONCE: busy at the first check, which deletes it, and idle after.
bot_is_busy() { rm "${BUSY_ONCE:-}" 2>/dev/null && return 0; return "${STUB_BUSY_RC:-1}"; }
emit_fleet_event() { printf '%s\\n' "$1" >> "$EVENTS_CAPTURE"; }
emit_fleet_notice() { printf '%s\\t%s\\t%s\\n' "$1" "$2" "$3" >> "$NOTICES_CAPTURE"; }
# chunk P fold: briefing-trigger.sh now sources these three from lib-common —
# _read_wire_out (called unconditionally; the real one no-ops on an empty path),
# and safe_mktemp / _wire_frag (reached only when PLANE_ARMED=1). Stubbed so the
# stub stays "every helper the script sources," matching the real contract.
_read_wire_out() { PLANE_WIRE_SHA256=""; PLANE_WIRE_BYTES=""; }
safe_mktemp() { mktemp; }
_wire_frag() { :; }
"""

# Stub dispatch.sh: capture <session>\\t<message>, exit with the steered code.
STUB_DISPATCH = """\
#!/bin/bash
printf '%s\\t%s' "$1" "$2" > "$DISPATCH_CAPTURE"
exit "${STUB_DISPATCH_RC:-0}"
"""


def _run(
    tmp_path: Path, *, env_extra: dict, bot_dir: bool = True, skill: bool = True
) -> tuple[int, str, str]:
    libdir = tmp_path / "lib"
    libdir.mkdir(exist_ok=True)
    # The skill check is the REAL predicate, lifted from lib-common.sh.
    real = re.search(
        r"^session_command_status\(\) \{.*?^\}\n", LIB_COMMON.read_text(), re.S | re.M
    )
    (libdir / "lib-common.sh").write_text(STUB_LIB_COMMON + real.group(0))
    dispatch = libdir / "dispatch.sh"
    dispatch.write_text(STUB_DISPATCH)
    dispatch.chmod(0o755)
    (libdir / "briefing-trigger.sh").write_text(TRIGGER.read_text())

    bots_dir = tmp_path / "bots"
    bots_dir.mkdir(exist_ok=True)
    if bot_dir:
        (bots_dir / "kev").mkdir(exist_ok=True)
        if skill:
            # The link generate composes for bots.<bot>.skills: [briefing].
            (bots_dir / "kev" / ".claude" / "skills" / "briefing").mkdir(
                parents=True, exist_ok=True
            )

    env = {
        **os.environ,
        "FAKE_BOTS_DIR": str(bots_dir),
        "DISPATCH_CAPTURE": str(tmp_path / "dispatch_capture"),
        "EVENTS_CAPTURE": str(tmp_path / "events_capture"),
        "NOTICES_CAPTURE": str(tmp_path / "notices_capture"),
        "BRIEFING_TRIGGER_LOG": str(tmp_path / "trigger.log"),
        **env_extra,
    }
    proc = subprocess.run(
        ["bash", str(libdir / "briefing-trigger.sh"), "test-fleet", "kev", "morning"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,  # a retry loop that never ends fails here instead of hanging
    )
    return proc.returncode, proc.stdout, proc.stderr


def _events(tmp_path: Path) -> list[str]:
    f = tmp_path / "events_capture"
    return f.read_text().split() if f.exists() else []


def _notices(tmp_path: Path) -> list[list[str]]:
    """Each emit_fleet_notice call as [bots_dir, event_type, message]."""
    f = tmp_path / "notices_capture"
    return [ln.split("\t") for ln in f.read_text().splitlines()] if f.exists() else []


def _assert_one_missed_notice(tmp_path: Path, reason: str) -> None:
    # The fleet's bots dir is what routes the notice to the fleet's manager.
    assert _notices(tmp_path) == [
        [str(tmp_path / "bots"), "briefing_missed", f"kev morning ({reason})"]
    ]


def _dispatched(tmp_path: Path) -> str:
    f = tmp_path / "dispatch_capture"
    return f.read_text() if f.exists() else ""


def test_dispatches_slash_briefing_when_idle(tmp_path):
    rc, _out, err = _run(
        tmp_path, env_extra={"STUB_SESSION_RC": "0", "STUB_BUSY_RC": "1"}
    )
    assert rc == 0, err
    session, message = _dispatched(tmp_path).split("\t")
    assert session == "kev"
    assert message == "/briefing morning"  # bare slash — dispatch.sh keeps it bare
    assert _events(tmp_path) == ["briefing_dispatched"]
    assert _notices(tmp_path) == []


def test_a_deferred_slot_is_sent_at_the_bots_next_idle_check(tmp_path):
    busy_once = tmp_path / "busy_once"
    busy_once.touch()
    rc, _out, err = _run(
        tmp_path,
        env_extra={
            "BUSY_ONCE": str(busy_once),
            "BRIEFING_RETRY_WINDOW_S": "30",
            "BRIEFING_RETRY_POLL_S": "1",
        },
    )
    assert rc == 0, err
    assert _dispatched(tmp_path) == "kev\t/briefing morning"
    assert _events(tmp_path) == ["briefing_deferred", "briefing_dispatched"]
    assert _notices(tmp_path) == []


@pytest.mark.parametrize(
    "env, reason",
    [({"STUB_BUSY_RC": "0"}, "bot_busy"), ({"STUB_SESSION_RC": "1"}, "session_absent")],
)
def test_a_slot_deferred_through_the_whole_window_is_noticed_once(tmp_path, env, reason):
    rc, _out, err = _run(
        tmp_path,
        env_extra={
            **env,
            "BRIEFING_RETRY_WINDOW_S": "2",
            "BRIEFING_RETRY_POLL_S": "1",
        },
    )
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""  # never sent into a busy or absent pane
    assert _events(tmp_path) == ["briefing_deferred", "briefing_failed"]
    _assert_one_missed_notice(tmp_path, reason)


def test_a_failed_dispatch_is_noticed_once(tmp_path):
    rc, _out, _err = _run(
        tmp_path,
        env_extra={
            "STUB_SESSION_RC": "0",
            "STUB_BUSY_RC": "1",
            "STUB_DISPATCH_RC": "1",
        },
    )
    assert rc != 0
    assert _events(tmp_path) == ["briefing_failed"]
    _assert_one_missed_notice(tmp_path, "dispatch_failed")


def test_a_missing_bot_dir_is_noticed_once(tmp_path):
    rc, _out, err = _run(tmp_path, env_extra={}, bot_dir=False)
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""
    _assert_one_missed_notice(tmp_path, "bot_dir_absent")


def test_refuses_and_fails_loud_when_the_briefing_skill_is_not_composed(tmp_path):
    # #1819: see the check in briefing-trigger.sh.
    rc, _out, err = _run(
        tmp_path, env_extra={"STUB_SESSION_RC": "0", "STUB_BUSY_RC": "1"}, skill=False
    )
    assert rc != 0
    assert _dispatched(tmp_path) == ""
    assert _events(tmp_path) == ["briefing_failed"]
    assert "no briefing skill composed" in err
