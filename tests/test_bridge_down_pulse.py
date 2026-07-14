"""Tests for the `bridge_down_state` lib-common.sh helper (#453 Tier-1, PR3).

Verifies the down-state classification (no_bridge/no_token actionable; others
not) and the post-(re)start grace window. See the helper's own doc comment in
lib-common.sh for the full state-collapse and grace semantics.
"""

import os
import subprocess
import time
from pathlib import Path

LIB_COMMON = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"


def _bridge_down_state(bot_dir: Path, home: Path, grace: int = 300) -> tuple[str, int]:
    """Source lib-common.sh and run `bridge_down_state`; return (stdout, rc).

    HOME is pinned to an isolated dir so source_env_tiered cannot read the real
    ~/.env (hermetic; no real secrets, no cross-test bleed) — mirrors
    test_bridge_state.py.
    """
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; bridge_down_state "$2" "$3"',
            "_",
            str(LIB_COMMON),
            str(bot_dir),
            str(grace),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    return proc.stdout.strip(), proc.returncode


def _write_bot_conf(
    bot_dir, *, handle="b1", state_dir=None, token_env=None, expect_no_token=False
):
    """Minimal bot.conf. The token VALUE is never written here — bot.conf only
    names the var via TELEGRAM_TOKEN_ENV_NAME (as composer does)."""
    bot_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"export CLAUDLOBBY_ROOT={bot_dir.parent}",
        "export FLEET_NAME=test-fleet",
        f'BOT_DIR="{bot_dir}"',
    ]
    if handle is not None:
        lines.append(f"export TELEGRAM_BOT_HANDLE={handle}")
    if state_dir is not None:
        lines.append(f'TELEGRAM_STATE_DIR="{state_dir}"')
    if token_env is not None:
        lines.append(f"export TELEGRAM_TOKEN_ENV_NAME={token_env}")
    if expect_no_token:
        lines.append("export EXPECT_NO_TOKEN=1")
    (bot_dir / "bot.conf").write_text("\n".join(lines) + "\n")


def _set_spawn(bot_dir: Path, age_seconds: float):
    """Create data/.spawn with an mtime `age_seconds` in the past."""
    data = bot_dir / "data"
    data.mkdir(parents=True, exist_ok=True)
    marker = data / ".spawn"
    marker.touch()
    t = time.time() - age_seconds
    os.utime(marker, (t, t))


# --- down-state classification (no live bridge / tmux needed) ----------------


def test_no_bridge_is_actionable_bridge_down(tmp_path):
    """Token resolves but no live poller (no bot.pid) -> alert as no_bridge."""
    sd = tmp_path / "state"
    sd.mkdir()  # exists, but no bot.pid -> bridge down
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(bot, handle="b1", state_dir=sd, token_env="B1_TG_TOKEN")
    (bot / ".env").write_text("B1_TG_TOKEN=8888888:AAAAAAAAAAAAAAAAAAAA\n")
    out, rc = _bridge_down_state(bot, tmp_path)
    assert out == "no_bridge"
    assert rc == 0


def test_non_down_state_is_not_actionable(tmp_path):
    """A bot with no Telegram handle is not a channel bot — never bridge_down.
    Represents the default (non-actionable) branch shared by up/no_handle/unknown."""
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(bot, handle=None)
    out, rc = _bridge_down_state(bot, tmp_path)
    assert out == ""
    assert rc != 0


# --- post-(re)start grace window ---------------------------------------------


def test_within_grace_suppresses_alert(tmp_path):
    """Bridge is down, but the bot (re)started 5s ago with a 300s grace ->
    suppressed (the helper short-circuits before classifying)."""
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(
        bot, handle="b1", state_dir=tmp_path / "state", token_env="B1_TG_TOKEN"
    )
    _set_spawn(bot, age_seconds=5)
    out, rc = _bridge_down_state(bot, tmp_path, grace=300)
    assert out == ""
    assert rc != 0


def test_past_grace_alerts(tmp_path):
    """A .spawn marker aged well past the grace window no longer suppresses."""
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(
        bot, handle="b1", state_dir=tmp_path / "state", token_env="B1_TG_TOKEN"
    )
    _set_spawn(bot, age_seconds=1000)
    out, rc = _bridge_down_state(bot, tmp_path, grace=300)
    assert out == "no_token"
    assert rc == 0


def test_no_token_canary_marker_not_actionable(tmp_path):
    """#608: a declared throwaway (EXPECT_NO_TOKEN=1) has no token by design, so
    fleet-pulse must NOT count it as bridge_down — no alert. Same setup as
    test_past_grace_alerts (past grace, so the grace window is not what suppresses)
    except for the marker: it is the marker, not timing, that exempts it."""
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(
        bot,
        handle="b1",
        state_dir=tmp_path / "state",
        token_env="B1_TG_TOKEN",
        expect_no_token=True,
    )
    _set_spawn(bot, age_seconds=1000)
    out, rc = _bridge_down_state(bot, tmp_path, grace=300)
    assert out == ""
    assert rc != 0
