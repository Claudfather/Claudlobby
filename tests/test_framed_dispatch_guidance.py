"""Every composed bot carries the verify-then-trust check for framed dispatches.

Nine of the estate's 21 bots composed neither protocol that held it (#1876).
The guidance also retains the whole minted msg_ ID when extracting the trailer
from a pasted prompt (#1946).
"""
from tests.conftest import install_real_template, load_test_fleet, make_paths

from claudlobby.composer import compose_claude_md

def test_every_composed_bot_carries_the_verify_then_trust_check(fleet_dir):
    # The fixture's library holds neither the dispatch nor the worker-lifecycle
    # protocol, so a bot can only have the check if the template gives it.
    install_real_template(fleet_dir)
    fleet = load_test_fleet(fleet_dir)
    paths = make_paths(fleet_dir)
    assert len(fleet.bots) >= 2
    for bot in fleet.bots.values():
        text = compose_claude_md(bot, fleet, paths)
        assert "## Dispatches framed as pasted text" in text, bot.name
        check = ("claudlobby --json message receipt MESSAGE_ID --destination "
                 f"{fleet.name}/{bot.bot_id} --wait 30")
        assert check in text, bot.name
        assert "$FLEET_NAME/$BOT_ID" not in text, bot.name
        assert "data.message_id" in text and "data.sender.alias" in text, bot.name
        assert "data.destination.alias" in text and "data.integrity_verdict" in text, bot.name
        assert "unexpected sender or destination" in text, bot.name
        assert "never accepts an assignment" in text, bot.name
        assert "plane-lookup.py" not in text, bot.name
        assert "between `plane:` and `⟧`" in text, bot.name
        assert "never the hex alone" in text, bot.name


def test_every_composed_bot_is_told_what_the_set_h_prefix_is(fleet_dir):
    # lib/dispatch.sh puts `set +H; ` in front of every non-slash-command
    # message, and receivers flagged it as unexplained: only the dispatch
    # protocol said what it was, and only managers compose that. The note
    # sits in the pasted-text section every bot carries.
    install_real_template(fleet_dir)
    fleet = load_test_fleet(fleet_dir)
    paths = make_paths(fleet_dir)
    assert len(fleet.bots) >= 2
    for bot in fleet.bots.values():
        text = compose_claude_md(bot, fleet, paths)
        section = text.split("## Dispatches framed as pasted text", 1)[1].split("\n## ", 1)[0]
        assert "can also start with `set +H; `" in section, bot.name
        assert "such as a file path" in section, bot.name
        assert "there is nothing to run" in section, bot.name
