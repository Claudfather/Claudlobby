"""usage-limit-hook.sh (#996): the bot's own record of a usage-limit stop.

The StopFailure payload is the shape Claude Code 2.1.292 sent a hook on a live
limit (captured in a hermetic run against a stand-in API; identifiers faked).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "claudlobby/_runtime_scripts/usage-limit-hook.sh"
LINE = "You've hit your session limit · resets 10:45am (America/New_York)"


def _payload(event: str, **extra) -> str:
    p = {
        "session_id": "00000000-0000-4000-8000-000000000000",
        "transcript_path": "/tmp/harness/transcript.jsonl",
        "cwd": "/tmp/harness",
        "hook_event_name": event,
        **extra,
    }
    return json.dumps(p)


def _run(bot: Path | None, payload: str) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "PLANE_EMIT_DISABLED": "1"}
    if bot is not None:
        env["BOT_DIR"] = str(bot)
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


@pytest.fixture
def bot(tmp_path):
    (tmp_path / "data").mkdir()
    return tmp_path


def test_a_rate_limit_stop_writes_the_record(bot):
    r = _run(
        bot,
        _payload(
            "StopFailure",
            error="rate_limit",
            last_assistant_message=LINE
            + "\n/usage-credits to finish what you're working on.",
        ),
    )
    assert (r.returncode, r.stdout) == (0, "")
    epoch, line = (bot / "data/.usage-limit").read_text().splitlines()
    assert epoch.isdigit() and line == LINE


def test_another_failure_leaves_the_record_as_it_is(bot):
    marker = bot / "data/.usage-limit"
    marker.write_text("1\n" + LINE + "\n")
    r = _run(
        bot,
        _payload(
            "StopFailure", error="server_error", last_assistant_message="API Error: 529"
        ),
    )
    assert (r.returncode, r.stdout) == (0, "")
    assert marker.read_text() == "1\n" + LINE + "\n"


def test_a_turn_that_ends_normally_clears_the_record(bot):
    marker = bot / "data/.usage-limit"
    marker.write_text("1\n" + LINE + "\n")
    r = _run(bot, _payload("Stop", stop_hook_active=False))
    assert (r.returncode, r.stdout) == (0, "")
    assert not marker.exists()


@pytest.mark.parametrize("payload", ["", "not json", "[]", _payload("SessionStart")])
def test_anything_else_writes_nothing(bot, payload):
    r = _run(bot, payload)
    assert (r.returncode, r.stdout) == (0, "")
    assert list((bot / "data").iterdir()) == []


def test_without_bot_dir_it_writes_nowhere(tmp_path):
    r = subprocess.run(
        ["bash", str(HOOK)],
        input=_payload("StopFailure", error="rate_limit", last_assistant_message=LINE),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        timeout=30,
    )
    assert (r.returncode, r.stdout) == (0, "")
    assert list(tmp_path.iterdir()) == []


def test_it_is_composed_for_stopfailure_and_stop():
    import yaml

    hooks = yaml.safe_load((REPO / "claudlobby/system.yaml").read_text())["defaults"][
        "hooks"
    ]
    for event in ("StopFailure", "Stop"):
        commands = [h["command"] for h in hooks[event]]
        assert "$CLAUDLOBBY_NATIVE_DIR/usage-limit-hook.sh" in commands, (
            event,
            commands,
        )
