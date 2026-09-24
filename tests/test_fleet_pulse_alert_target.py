"""fleet-pulse and the alert PAIR (#1771, part B) -- reviewer-supplied for #1782.

The PR's tests pin the resolver (`test_alert_target_pair.py`) and `_emit_fleet_signal`, but nothing drives
the REAL fleet-pulse sweep through the three things the PR says it adds there:

  * a REFUSED escalation target is loud: a WARNING that names the variable, and a plane event
    (`alert_target_refused`), while nothing is sent to the refused chat;
  * that page is debounced (once, then daily) and CLEARS the moment the pair resolves, so a later
    refusal pages again;
  * an ambient TELEGRAM_BOT_TOKEN rides only the session's OWN env pair -- never a pair the resolver took
    from a scanned bot (the PR says all three of fleet-pulse's sends follow this rule).

Same rig as `test_fleet_pulse_escalated.py`: the real sweep, a throwaway plane, one stub (`tg-post.sh`).
Fake ids only.
"""

from __future__ import annotations

import json

from tests.conftest import read_fleet_events
from tests.test_fleet_pulse_escalated import (
    _act,
    _pulse,
    _pulse_lib,
    _scene,
    needs_tmux,
)

CHAT_ESC = "-1003333333333"
CHAT_BOT = "-1002222222222"
FAST = {"FLEET_EVENT_EMIT_TIMEOUT_S": "120"}

# every test here runs the real sweep, which needs tmux
pytestmark = needs_tmux


def _events(root, kind):
    rows = [json.loads(line) for line in read_fleet_events(root).splitlines() if line]
    return [r for r in rows if r["type"] == kind]


def _marker(root):
    return root / "state" / "pulse" / "fleet.alert_target_refused"


# --- a refused escalation target is loud, debounced, and clears --------------------


def test_a_refused_escalation_chat_is_loud_debounced_and_clears(tmp_path):
    root, paths, wi, asg = _scene(tmp_path)
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)
    refused = {
        "FLEET_PULSE_ESCALATION_CHAT_ID": CHAT_ESC,
        "FLEET_PULSE_ESCALATION_STATE_DIR": "",
        **FAST,
    }

    r = _pulse(root, libdir, **refused)
    assert r.returncode == 0, r.stderr[-2000:]
    # loud, and it names the fix
    assert "escalation Telegram target REFUSED" in r.stderr, r.stderr[-1500:]
    assert "FLEET_PULSE_ESCALATION_STATE_DIR" in r.stderr
    # nothing is sent at all: the refused target is not attempted, and no other page rides in its place
    assert not capture.exists(), capture.read_text()
    # ...and the refusal reaches the plane, once
    assert len(_events(root, "alert_target_refused")) == 1
    assert _marker(root).exists()

    # debounced: the same refusal on the next sweep is not announced again
    _pulse(root, libdir, **refused)
    assert len(_events(root, "alert_target_refused")) == 1, (
        "the refusal page was not debounced"
    )

    # it clears the moment the pair resolves...
    resolved = {
        "FLEET_PULSE_ESCALATION_CHAT_ID": CHAT_ESC,
        "FLEET_PULSE_ESCALATION_STATE_DIR": str(root / "sender"),
        **FAST,
    }
    _pulse(root, libdir, **resolved)
    assert not _marker(root).exists(), "the refusal marker survived a resolved pair"

    # ...so a later refusal pages again
    _pulse(root, libdir, **refused)
    assert len(_events(root, "alert_target_refused")) == 2


# --- the ambient token rides only the session's own env pair -----------------------


def _token_scene(tmp_path, *, env_pair):
    root, paths, wi, asg = _scene(tmp_path)
    # a bot with its own chat and channel dir: the resolver's scan source
    (paths.runtime_bots / "w1" / "bot.conf").write_text(
        f'export TELEGRAM_GROUP_CHAT_ID="{CHAT_BOT}"\n'
        'export TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-w1"\n'
        "TMUX_SOCKET=esc-none-w1\n"
    )
    _act(
        root,
        wi,
        asg,
        "escalated",
        "2026-09-02T10:00:00Z",
        by="mgr",
        question="ship it?",
    )
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)
    # the stub records the token it was handed, then succeeds
    (libdir / "tg-post.sh").write_text(
        f'#!/bin/bash\nprintf "%s|%s\\n" "${{TELEGRAM_BOT_TOKEN:-<empty>}}" "$TELEGRAM_GROUP_CHAT_ID" >> "{capture}"\n'
    )
    extra = {
        "FLEET_PULSE_ESCALATION_CHAT_ID": "",
        "FLEET_PULSE_ESCALATION_STATE_DIR": "",
        "TELEGRAM_BOT_TOKEN": "ambient-session-token",
        **FAST,
    }
    if env_pair:
        extra.update(
            TELEGRAM_GROUP_CHAT_ID=CHAT_BOT,
            TELEGRAM_STATE_DIR=str(root / "session-dir"),
        )
    r = _pulse(root, libdir, **extra)
    assert r.returncode == 0, r.stderr[-2000:]
    return (
        [line for line in capture.read_text().splitlines() if line]
        if capture.exists()
        else []
    )


def test_a_scanned_pair_never_carries_an_ambient_token_in_fleet_pulse(tmp_path):
    # the pair came from bot w1 (no env chat): its token is read from w1's channel dir by tg-post,
    # so the session's own token must NOT be handed over
    sends = _token_scene(tmp_path, env_pair=False)
    assert sends, "the escalation page was never attempted"
    assert all(s.startswith("<empty>|") for s in sends), sends


def test_the_sessions_own_env_pair_keeps_its_token_in_fleet_pulse(tmp_path):
    sends = _token_scene(tmp_path, env_pair=True)
    assert sends, "the escalation page was never attempted"
    assert all(s.startswith("ambient-session-token|") for s in sends), sends
