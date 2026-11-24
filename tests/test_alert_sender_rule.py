"""One fleet-alert sender rule in both halves (#3127).

lib-common's resolver picks the sender for fleet-pulse, creds-check and every
emit_failure_alert, emit_fleet_notice and notify_currency; the Python rule
(config.fleet_alert_sender) picks it for `fleet notify`, a failed message
write and the validator. Each case composes the fleet's real bot.conf files,
runs the shell resolver on them with the fleet chat a fleet timer carries, and
asks the Python rule the same question: both must name the same bot, the fleet
manager whenever it can send, though its workers sort before it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claudlobby.composer import compose_bot_conf
from claudlobby.config import BotConfig, FleetConfig, TelegramConfig, fleet_alert_sender
from claudlobby.paths import Paths
from tests.conftest import constructed_env
from tests.package_fixtures import git_package

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "claudlobby/_runtime_scripts/lib-common.sh"
CHAT = "-1001111111111"
OTHER = "-1002222222222"
TELEGRAM = ["plugin:telegram@claude-plugins-official"]


def _bot(bot_id: str, kind: str) -> BotConfig:
    if kind == "none":
        return BotConfig(bot_id=bot_id, name=bot_id, expertise=["eng"], channels=[])
    chat = OTHER if kind == "other-chat" else None
    return BotConfig(bot_id=bot_id, name=bot_id, expertise=["eng"], channels=TELEGRAM,
                     telegram=TelegramConfig(chat_id=chat))


# The manager is `zed`; its workers sort before it. Each case: what kind of bot
# each one is, whose channel dir holds the token file, the sender.
CASES = {
    # A name comes before the longer names it starts, in both halves.
    "a-name-before-the-names-it-starts": ({"zed": "none", "abc-d": "in", "abc": "in"},
                                          "abc-d abc", "abc"),
    "workers-sort-first": ({"zed": "in", "able": "in", "baker": "in"}, "zed able baker", "zed"),
    "manager-not-a-channel-bot": ({"zed": "none", "able": "in", "baker": "in"}, "able baker", "able"),
    "manager-in-another-chat": ({"zed": "other-chat", "able": "in", "baker": "in"}, "zed able baker",
                                "able"),
    "manager-without-a-token-file": ({"zed": "in", "able": "in", "baker": "in"}, "able baker", "able"),
    "first-worker-without-a-token-file": ({"zed": "none", "able": "in", "baker": "in"}, "baker",
                                          "baker"),
    "no-token-file-anywhere": ({"zed": "in", "able": "in", "baker": "in"}, "", "zed"),
}


@pytest.mark.parametrize("case", CASES)
def test_the_shell_and_the_python_rule_name_the_same_sender(tmp_path, monkeypatch, case):
    kinds, tokens, expected = CASES[case]
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "claudlobby"
    (root / "lib").mkdir(parents=True)
    fleet = FleetConfig(manager="zed", name="fleet", service_prefix="com.fleet",
                        telegram_group_chat_id=CHAT,
                        bots={bot_id: _bot(bot_id, kind) for bot_id, kind in kinds.items()})
    paths = Paths(root=root, fleet_dir=root, package=git_package(root))
    bots = root / "runtime" / "bots"
    for bot_id in fleet.bots:
        (bots / bot_id).mkdir(parents=True)
        (bots / bot_id / "bot.conf").write_text(compose_bot_conf(fleet.bots[bot_id], fleet, paths))
    (root / "fleet.yaml").write_text(
        "fleet:\n  bots:\n" + "".join(f"    {bot_id}:\n" for bot_id in fleet.bots))
    for bot_id in tokens.split():
        channel = home / f".claude/channels/telegram-{bot_id}"
        channel.mkdir(parents=True)
        (channel / ".env").write_text("TELEGRAM_BOT_TOKEN=8888888:AAAAAAAAAAAAAAAAAAAA\n")

    python = fleet_alert_sender(fleet, CHAT, require_token_file=True)
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; resolve_alert_target "{bots}" fleet; '
                       'printf "%s\\n%s\\n" "$_alert_state_dir" "$_alert_target_src"'],
        env=constructed_env(HOME=home, CLAUDLOBBY_ROOT=root, TELEGRAM_GROUP_CHAT_ID=CHAT),
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    state_dir, src = r.stdout.splitlines()

    assert python is not None and python.bot_id == expected, python
    assert state_dir == python.state_dir == str(home / f".claude/channels/telegram-{expected}")
    role = "manager" if expected == "zed" else "bot"
    assert src == f"env:TELEGRAM_GROUP_CHAT_ID+{role}:{expected}", (src, r.stderr)
    assert (python.manager_why == "") == (expected == "zed")
    assert ("alert-sender" in r.stderr) == (expected != "zed"), r.stderr
