"""Python-wrapped bash test for lib/briefing-trigger.sh (#627 P3).

The composed per-(bot,slot) briefing timer runs
``briefing-trigger.sh <fleet> <bot> <slot>``; it must deliver ``/briefing <slot>``
to the bot's own session via the slash-aware dispatch.sh, skip-with-log
(``briefing_deferred``) when the bot is busy or its session is absent, and emit
the right fleet event in each case.

Like test_dispatch_slash.py, this copies the real script next to stub helpers so
it exercises the real control flow without a live tmux server. The event type
and the dispatched payload are captured to files the assertions read.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
TRIGGER = REPO / "lib" / "briefing-trigger.sh"

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
bot_is_busy() { return "${STUB_BUSY_RC:-1}"; }
emit_fleet_event() { printf '%s\\n' "$1" >> "$EVENTS_CAPTURE"; }
"""

# Stub dispatch.sh: capture <session>\\t<message>, exit with the steered code.
STUB_DISPATCH = """\
#!/bin/bash
printf '%s\\t%s' "$1" "$2" > "$DISPATCH_CAPTURE"
exit "${STUB_DISPATCH_RC:-0}"
"""


def _run(tmp_path: Path, *, env_extra: dict) -> tuple[int, str, str]:
    libdir = tmp_path / "lib"
    libdir.mkdir(exist_ok=True)
    (libdir / "lib-common.sh").write_text(STUB_LIB_COMMON)
    dispatch = libdir / "dispatch.sh"
    dispatch.write_text(STUB_DISPATCH)
    dispatch.chmod(0o755)
    (libdir / "briefing-trigger.sh").write_text(TRIGGER.read_text())

    bots_dir = tmp_path / "bots"
    (bots_dir / "kev").mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "FAKE_BOTS_DIR": str(bots_dir),
        "DISPATCH_CAPTURE": str(tmp_path / "dispatch_capture"),
        "EVENTS_CAPTURE": str(tmp_path / "events_capture"),
        "BRIEFING_TRIGGER_LOG": str(tmp_path / "trigger.log"),
        **env_extra,
    }
    proc = subprocess.run(
        ["bash", str(libdir / "briefing-trigger.sh"), "test-fleet", "kev", "morning"],
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _event(tmp_path: Path) -> str:
    f = tmp_path / "events_capture"
    return f.read_text().strip() if f.exists() else ""


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
    assert _event(tmp_path) == "briefing_dispatched"


def test_defers_when_busy(tmp_path):
    rc, _out, err = _run(
        tmp_path, env_extra={"STUB_SESSION_RC": "0", "STUB_BUSY_RC": "0"}
    )
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""  # never dispatched
    assert _event(tmp_path) == "briefing_deferred"


def test_defers_when_session_absent(tmp_path):
    rc, _out, err = _run(tmp_path, env_extra={"STUB_SESSION_RC": "1"})
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""
    assert _event(tmp_path) == "briefing_deferred"


def test_failed_dispatch_emits_briefing_failed(tmp_path):
    rc, _out, _err = _run(
        tmp_path,
        env_extra={
            "STUB_SESSION_RC": "0",
            "STUB_BUSY_RC": "1",
            "STUB_DISPATCH_RC": "1",
        },
    )
    assert rc != 0
    assert _event(tmp_path) == "briefing_failed"
