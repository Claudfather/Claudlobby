"""resolve_alert_target resolves the alert chat and its SENDER as one pair (#1771).

The defect: the env supplied the chat (a fleet timer's composed
TELEGRAM_GROUP_CHAT_ID) and the token came from whichever bot a scan found
first. On one fleet that bot was not in the fleet chat, so every fleet alert
failed with `chat not found` for two months while a token-only check called
it healthy. The pair now comes from ONE source, and a chat with no sender is
REFUSED — loudly, naming what to set — never paired with a guessed token.

Driven through the real lib-common in a constructed env, so no ambient chat
id or token from a bot session can leak in. Chat ids are obvious fakes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.conftest import (
    TG_STUB,
    _write_exec,
    constructed_env,
    plane_emit_env,
    read_fleet_events,
)

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"

CHAT_A = "-1001111111111"
CHAT_B = "-1002222222222"
CHAT_ESC = "-1003333333333"


def _bot(bots_dir: Path, name: str, chat: str | None, state_dir: bool = True) -> Path:
    d = bots_dir / name
    d.mkdir(parents=True)
    lines = [f'export TELEGRAM_BOT_HANDLE="{name}"']
    if chat:
        lines.append(f'export TELEGRAM_GROUP_CHAT_ID="{chat}"')
    if state_dir:
        lines.append(
            f'export TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-{name}"'
        )
    (d / "bot.conf").write_text("\n".join(lines) + "\n")
    return d


def _resolve(tmp_path: Path, bots_dir: Path, scope: str = "fleet", **env) -> dict:
    script = (
        f'. "{LIB}/lib-common.sh"; resolve_alert_target "{bots_dir}" {scope}; '
        'printf "chat=%s\\nstate=%s\\nsrc=%s\\nrefusal=%s\\n" '
        '"$_alert_chat_id" "$_alert_state_dir" "$_alert_target_src" "$_alert_refusal"'
    )
    r = subprocess.run(
        ["bash", "-c", script],
        env=constructed_env(
            HOME=tmp_path / "home", CLAUDLOBBY_ROOT=tmp_path / "root", **env
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return dict(line.split("=", 1) for line in r.stdout.splitlines())


def _home_state(tmp_path: Path, bot: str) -> str:
    return str(tmp_path / "home" / ".claude" / "channels" / f"telegram-{bot}")


# --- the required case: an env chat no bot is in --------------------------------


def test_an_env_chat_no_bot_is_in_is_refused_not_paired_with_a_scanned_token(tmp_path):
    bots = tmp_path / "root" / "runtime" / "bots"
    _bot(bots, "alpha", CHAT_B)  # a live bot, in a DIFFERENT chat
    got = _resolve(tmp_path, bots, TELEGRAM_GROUP_CHAT_ID=CHAT_A)
    assert got["chat"] == "" and got["state"] == "", got
    assert got["src"] == "refused"
    assert "TELEGRAM_GROUP_CHAT_ID" in got["refusal"]
    assert "not guessed" in got["refusal"]


def test_the_refusal_sends_nothing_and_the_row_says_why(tmp_path):
    root = tmp_path / "root"
    (root / "lib").mkdir(parents=True)
    _write_exec(root / "lib" / "tg-post.sh", TG_STUB)
    bots = root / "runtime" / "bots"
    _bot(bots, "alpha", CHAT_B)
    capture = tmp_path / "tg-capture"
    env = constructed_env(
        HOME=tmp_path / "home",
        CLAUDLOBBY_ROOT=root,
        TG_CAPTURE=capture,
        TELEGRAM_GROUP_CHAT_ID=CHAT_A,
        FLEET_EVENT_EMIT_TIMEOUT_S="120",
        **plane_emit_env(),
    )
    driver = (
        f'. "{LIB}/lib-common.sh"; emit_failure_alert "{bots}" probe_alert "a probe"'
    )
    r = subprocess.run(
        ["bash", "-c", driver], env=env, capture_output=True, text=True, timeout=300
    )
    assert r.returncode == 0, r.stderr
    assert not capture.exists(), "the refused pair must send NOTHING"
    rows = [json.loads(line) for line in read_fleet_events(root).splitlines()]
    failed = [row["data"] for row in rows if row["type"] == "alert_delivery_failed"]
    assert len(failed) == 1, rows
    assert failed[0]["exit"] == 2 and failed[0]["target"] == "refused", failed[0]
    assert (
        "TELEGRAM_GROUP_CHAT_ID from the environment has no sender"
        in failed[0]["detail"]
    )


# --- the env chat's partners ----------------------------------------------------


def test_an_env_chat_pairs_with_the_bot_whose_own_chat_it_is(tmp_path):
    bots = tmp_path / "root" / "runtime" / "bots"
    _bot(bots, "alpha", CHAT_B)  # first in scan order, but not in the chat
    _bot(bots, "beta", CHAT_A)
    got = _resolve(tmp_path, bots, TELEGRAM_GROUP_CHAT_ID=CHAT_A)
    assert got["chat"] == CHAT_A
    assert got["state"] == _home_state(tmp_path, "beta"), got
    assert got["src"] == "env:TELEGRAM_GROUP_CHAT_ID+bot:beta"
    assert got["refusal"] == ""


def test_a_moved_bots_leftover_dir_does_not_send_its_old_fleets_alerts(tmp_path):
    # move-bot keeps the source runtime dir unless --cleanup-source. A leftover that
    # sorts first would pair the fleet chat with a bot that may have left the group.
    bots = tmp_path / "root" / "runtime" / "bots"
    _bot(bots, "alpha", CHAT_A)  # moved to another fleet; its old dir remains
    _bot(bots, "beta", CHAT_A)
    (tmp_path / "root" / "fleet.yaml").write_text("fleet:\n  bots:\n    beta:\n")
    got = _resolve(tmp_path, bots, TELEGRAM_GROUP_CHAT_ID=CHAT_A)
    assert got["src"] == "env:TELEGRAM_GROUP_CHAT_ID+bot:beta", got


def test_a_state_dir_set_beside_the_env_chat_is_the_pair(tmp_path):
    bots = tmp_path / "root" / "runtime" / "bots"
    _bot(bots, "alpha", CHAT_B)
    got = _resolve(
        tmp_path, bots, TELEGRAM_GROUP_CHAT_ID=CHAT_A, TELEGRAM_STATE_DIR="/sender/dir"
    )
    assert (got["chat"], got["state"]) == (CHAT_A, "/sender/dir")
    assert got["src"] == "env:TELEGRAM_GROUP_CHAT_ID+TELEGRAM_STATE_DIR"


def test_the_equality_search_never_crosses_fleets(tmp_path):
    # A bot in ANOTHER fleet whose chat matches must not become the sender: that
    # would page this fleet's alert into another fleet's group.
    root = tmp_path / "root"
    mine = root / "local" / "f1" / "runtime" / "bots"
    _bot(mine, "alpha", CHAT_B)
    _bot(root / "local" / "f2" / "runtime" / "bots", "beta", CHAT_A)
    got = _resolve(tmp_path, mine, scope="any", TELEGRAM_GROUP_CHAT_ID=CHAT_A)
    assert got["src"] == "refused", got


# --- the escalation chat: only its declared partner ----------------------------


def test_the_escalation_chat_without_its_partner_is_refused_naming_it(tmp_path):
    bots = tmp_path / "root" / "runtime" / "bots"
    # Neither a bot whose own chat IS the escalation chat nor a TELEGRAM_STATE_DIR
    # in the env may stand in for the declared partner.
    _bot(bots, "alpha", CHAT_ESC)
    got = _resolve(
        tmp_path,
        bots,
        FLEET_PULSE_ESCALATION_CHAT_ID=CHAT_ESC,
        TELEGRAM_GROUP_CHAT_ID=CHAT_ESC,
        TELEGRAM_STATE_DIR="/some/bot/dir",
    )
    assert got["chat"] == "" and got["state"] == "", got
    assert got["src"] == "refused"
    assert "set FLEET_PULSE_ESCALATION_STATE_DIR" in got["refusal"]
    assert "fleet_pulse.escalation_state_dir" in got["refusal"]


def test_the_escalation_chat_with_its_partner_is_the_pair(tmp_path):
    bots = tmp_path / "root" / "runtime" / "bots"
    _bot(bots, "alpha", CHAT_B)
    got = _resolve(
        tmp_path,
        bots,
        FLEET_PULSE_ESCALATION_CHAT_ID=CHAT_ESC,
        FLEET_PULSE_ESCALATION_STATE_DIR="/escalation/sender",
    )
    assert (got["chat"], got["state"]) == (CHAT_ESC, "/escalation/sender")
    assert (
        got["src"]
        == "env:FLEET_PULSE_ESCALATION_CHAT_ID+FLEET_PULSE_ESCALATION_STATE_DIR"
    )


# --- no env chat: one bot supplies both ----------------------------------------


def test_no_env_chat_takes_both_halves_from_one_scanned_bot(tmp_path):
    bots = tmp_path / "root" / "runtime" / "bots"
    _bot(bots, "alpha", CHAT_B)
    got = _resolve(tmp_path, bots)
    assert (got["chat"], got["state"]) == (CHAT_B, _home_state(tmp_path, "alpha"))
    assert got["src"] == "scan:bot:alpha"


# --- the token rides only the session's own pair --------------------------------

# Records which token tg-post was handed, then succeeds.
TOKEN_STUB = (
    '#!/bin/bash\nprintf "%s\\n" "${TELEGRAM_BOT_TOKEN:-<empty>}" >> "$TG_CAPTURE"\n'
)


def _token_seen(tmp_path: Path, **env) -> str:
    root = tmp_path / "root"
    (root / "lib").mkdir(parents=True, exist_ok=True)
    _write_exec(root / "lib" / "tg-post.sh", TOKEN_STUB)
    bots = root / "runtime" / "bots"
    if not bots.exists():
        _bot(bots, "alpha", CHAT_B)
    capture = tmp_path / "tg-capture"
    full = constructed_env(
        HOME=tmp_path / "home",
        CLAUDLOBBY_ROOT=root,
        TG_CAPTURE=capture,
        TELEGRAM_BOT_TOKEN="ambient-session-token",
        PLANE_EMIT_DISABLED="1",
        **env,
    )
    driver = (
        f'. "{LIB}/lib-common.sh"; emit_failure_alert "{bots}" probe_alert "a probe"'
    )
    r = subprocess.run(
        ["bash", "-c", driver], env=full, capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 0, r.stderr
    return capture.read_text().strip()


def test_a_scanned_pair_never_rides_an_ambient_token(tmp_path):
    # The pair came from bot alpha: the token must be alpha's (tg-post reads it
    # from alpha's state dir), never the session's own.
    assert _token_seen(tmp_path) == "<empty>"


def test_the_sessions_own_env_pair_keeps_its_token(tmp_path):
    seen = _token_seen(
        tmp_path, TELEGRAM_GROUP_CHAT_ID=CHAT_B, TELEGRAM_STATE_DIR="/session/dir"
    )
    assert seen == "ambient-session-token"
