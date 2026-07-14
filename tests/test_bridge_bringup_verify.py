"""Tests for `bridge_bringup_verify` (lib-common.sh) — #453 Tier-1, PR4 (Phase 3b/3c).

start-bot.sh calls this once post-boot to prove the Telegram bridge came up. It
NEVER bounces (keepalive owns the heal ladder — Fork F1=b); it polls bridge_state,
then on a terminal non-up state logs / marks / escalates so a silently-dark bridge
is loud at bring-up. `bridge_state` and `emit_failure_alert` are stubbed so these
pin the state->action mapping and the data/.bridge-down marker lifecycle
deterministically, without a live bridge or a real escalation.
"""

import os
import subprocess
from pathlib import Path

LIB_COMMON = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"


def _verify(bot_dir: Path, *, state: str, timeout: int = 1):
    """Stub bridge_state->`state` and emit_failure_alert->append-to-alerts.log,
    then run `bridge_bringup_verify <bot_dir> <bots_dir> <timeout>`.

    Returns (verdict, marker_exists, alert_count)."""
    alerts = bot_dir.parent / "alerts.log"
    script = (
        '. "$1"\n'
        # bridge_state stub: ignore the dir, print the fixed state, succeed only on up.
        'bridge_state() { printf "%s" "$STUB_STATE"; [ "$STUB_STATE" = up ] && return 0 || return 1; }\n'
        # emit_failure_alert stub: record one line per escalation (capture the reason).
        'emit_failure_alert() { printf "%s\\n" "$3" >> "$STUB_ALERTS"; }\n'
        'bridge_bringup_verify "$2" "$3" "$4"\n'
    )
    env = {**os.environ, "STUB_STATE": state, "STUB_ALERTS": str(alerts)}
    proc = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "_",
            str(LIB_COMMON),
            str(bot_dir),
            str(bot_dir.parent),
            str(timeout),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    marker = bot_dir / "data" / ".bridge-down"
    alert_count = len(alerts.read_text().splitlines()) if alerts.exists() else 0
    return proc.stdout.strip(), marker.exists(), alert_count


def _mk_bot(tmp_path):
    bot = tmp_path / "bots" / "b1"
    bot.mkdir(parents=True)
    return bot


def test_up_returns_ready_no_alert(tmp_path):
    bot = _mk_bot(tmp_path)
    verdict, marker, alerts = _verify(bot, state="up")
    assert verdict == "ready"
    assert not marker
    assert alerts == 0


def test_up_clears_stale_marker(tmp_path):
    """A leftover .bridge-down from a prior dark bring-up is cleared unconditionally
    once the bridge reads up — the clear-on-up contract."""
    bot = _mk_bot(tmp_path)
    (bot / "data").mkdir()
    (bot / "data" / ".bridge-down").write_text("")
    verdict, marker, alerts = _verify(bot, state="up")
    assert verdict == "ready"
    assert not marker  # cleared
    assert alerts == 0


def test_no_bridge_marks_and_escalates(tmp_path):
    bot = _mk_bot(tmp_path)
    verdict, marker, alerts = _verify(bot, state="no_bridge", timeout=1)
    assert verdict == "missing:no_bridge"
    assert marker  # durable marker dropped for keepalive's later heal
    assert alerts == 1  # escalated once (tmux-first via emit_failure_alert)


def test_no_token_escalates_without_marker(tmp_path):
    """no_token is a config issue (token unset) — escalate once, but no marker
    churn (bouncing/healing can't fix a missing token)."""
    bot = _mk_bot(tmp_path)
    verdict, marker, alerts = _verify(bot, state="no_token", timeout=1)
    assert verdict == "missing:no_token"
    assert not marker
    assert alerts == 1


def test_no_token_canary_marker_exempts(tmp_path):
    """#608: a declared throwaway/canary (EXPECT_NO_TOKEN=1 in bot.conf) has no
    token by design — the no_token bring-up alert would be pure noise, so it is
    suppressed and the verdict is expected:no_token, not missing:no_token."""
    bot = _mk_bot(tmp_path)
    (bot / "bot.conf").write_text("export EXPECT_NO_TOKEN=1\n")
    verdict, marker, alerts = _verify(bot, state="no_token", timeout=1)
    assert verdict == "expected:no_token"
    assert not marker
    assert alerts == 0  # the whole point — no fleet alert for an intentional throwaway


def test_no_token_marker_must_be_one(tmp_path):
    """The marker is exact: EXPECT_NO_TOKEN=0 (or any non-1 value) is a real bot,
    so a missing token still escalates — a stray 0 cannot silence a genuine fault."""
    bot = _mk_bot(tmp_path)
    (bot / "bot.conf").write_text("export EXPECT_NO_TOKEN=0\n")
    verdict, marker, alerts = _verify(bot, state="no_token", timeout=1)
    assert verdict == "missing:no_token"
    assert alerts == 1


def test_unknown_is_silent(tmp_path):
    """unknown = ownership unprovable (EACCES / non-Linux) — never actionable."""
    bot = _mk_bot(tmp_path)
    verdict, marker, alerts = _verify(bot, state="unknown", timeout=1)
    assert verdict == "unknown"
    assert not marker
    assert alerts == 0


def test_no_handle_is_silent(tmp_path):
    """A bot with no Telegram handle is not a channel bot — nothing to verify."""
    bot = _mk_bot(tmp_path)
    verdict, marker, alerts = _verify(bot, state="no_handle")
    assert verdict == "no_handle"
    assert not marker
    assert alerts == 0
