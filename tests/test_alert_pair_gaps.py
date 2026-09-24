"""Pins for the parts of #1771 part B that no test kills -- reviewer-supplied for #1782.

The PR's tests kill all nine of its own mutants. These six are the claims its body and docs make that survive
deliberate mutation of the code behind them (each was run: green on the head, red under its mutation). Built on the
PR's own helpers; fake ids and tokens only.
"""

from __future__ import annotations

from claudlobby.config import FleetConfig, _coerce_fleet_pulse
from tests.test_alert_target_pair import CHAT_A, _bot, _resolve
from tests.test_creds_check_telegram import (
    REVOKED_TOKEN,
    VALID_TOKEN,
    WRONGBOT_TOKEN,
    _fleet,
    _run,
)
from tests.test_system_defaults import TestComposeFleetTimers as _Compose

AMBIENT = "999999:ambientAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


# --- the resolver's strictness ------------------------------------------------------


def test_the_own_chat_match_is_exact_never_a_prefix(tmp_path):
    # mutation: the bot's chat merely STARTS WITH the env chat. A chat id that is a prefix of another is a
    # different chat; pairing them is the wrong-pair defect this PR exists to remove.
    bots = tmp_path / "root" / "runtime" / "bots"
    _bot(bots, "alpha", CHAT_A)
    for env_chat in (CHAT_A[:-1], CHAT_A + "0"):
        got = _resolve(tmp_path, bots, TELEGRAM_GROUP_CHAT_ID=env_chat)
        assert got["src"] == "refused", (env_chat, got)


def test_a_bot_in_the_chat_with_no_channel_dir_is_not_a_sender(tmp_path):
    # mutation: a bot in the chat but declaring no TELEGRAM_STATE_DIR still pairs, with an EMPTY sender, which
    # tg-post then resolves to its dead default dir: the same split, one layer down.
    bots = tmp_path / "root" / "runtime" / "bots"
    _bot(bots, "alpha", CHAT_A, state_dir=False)
    got = _resolve(tmp_path, bots, TELEGRAM_GROUP_CHAT_ID=CHAT_A)
    assert got["src"] == "refused" and got["state"] == "", got
    _bot(bots, "beta", CHAT_A)  # a later bot in the same chat that DOES have one
    got = _resolve(tmp_path, bots, TELEGRAM_GROUP_CHAT_ID=CHAT_A)
    assert got["src"] == "env:TELEGRAM_GROUP_CHAT_ID+bot:beta", got


# --- what the schema doc promises the composer and config do -------------------------


def test_escalation_state_dir_expands_the_home_forms_the_docs_promise(
    tmp_path, monkeypatch
):
    # mutation: `$HOME/` is left literal. A unit's Environment= does not expand it, so the sender dir would be
    # a path that does not exist.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    want = str(tmp_path / "home" / ".claude" / "channels" / "telegram-x")
    for raw in ("~/.claude/channels/telegram-x", "$HOME/.claude/channels/telegram-x"):
        fp = _coerce_fleet_pulse(
            {"escalation_chat_id": "-1", "escalation_state_dir": raw}
        )
        assert fp.escalation_state_dir == want, raw


def test_the_fleet_sender_is_lexically_first_not_declaration_first(
    tmp_path, monkeypatch
):
    # mutation: `sorted(fleet.bots)` becomes dict order. The docs say lexical (it is the order the runtime scan
    # walks), so a fleet keeps the sender it has today.
    from claudlobby.composer import fleet_alert_sender_state_dir

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fleet = FleetConfig(
        name="f",
        service_prefix="com.t",
        telegram_group_chat_id="-1009999999999",
        bots={  # declared zed first
            "zed": _Compose._channel_bot("zed"),
            "alpha": _Compose._channel_bot("alpha"),
        },
    )
    assert fleet_alert_sender_state_dir(fleet) == str(
        tmp_path / "home" / ".claude" / "channels" / "telegram-alpha_bot"
    )


# --- creds-check ----------------------------------------------------------------------


def test_creds_check_drops_an_ambient_token_on_a_scanned_pair(tmp_path):
    # mutation: the ambient token is kept when the pair came from a scanned bot. It is a bot session's own token
    # and would re-split the pair: the alert would go out as some other bot.
    f = _fleet(
        tmp_path,
        real_tgpost=True,
        roster=[
            ("abot", "bot_one_bot", "T_ABOT_TOKEN", VALID_TOKEN),  # in the chat, live
            (
                "bbot",
                "b_bot",
                "T_BBOT_TOKEN",
                REVOKED_TOKEN,
            ),  # in the chat, revoked: its FAIL is what is sent
        ],
    )
    del f["env"]["TELEGRAM_GROUP_CHAT_ID"]  # no env chat: the pair is a scanned bot's
    f["env"]["TELEGRAM_BOT_TOKEN"] = AMBIENT
    _run(f)
    send_log = f["bindir"] / "send.log"
    sends = send_log.read_text() if send_log.exists() else ""
    assert sends, "the revoked bot's FAIL was never sent"
    assert AMBIENT not in sends, "an ambient token carried the alert"
    assert VALID_TOKEN in sends, "the live bot in the chat did not carry it"


def test_a_failed_getchat_is_raised_through_emit_failure_alert(tmp_path):
    # mutation: the failure is recorded but only the ladder's own message goes out, and that message rides the
    # very pair that just failed. The PR says it is also raised through emit_failure_alert.
    f = _fleet(
        tmp_path, roster=[("abot", "some_other_bot", "T_ABOT_TOKEN", WRONGBOT_TOKEN)]
    )
    _run(f)
    posts = f["tg_log"].read_text() if f["tg_log"].exists() else ""
    assert "alert_pair_unreachable" in posts, posts
