"""Every composed bot carries the verify-then-trust check for framed dispatches.

Nine of the estate's 21 bots composed neither protocol that held it (#1876).
The guidance also retains the whole minted msg_ ID when extracting the trailer
from a pasted prompt (#1946).
"""
from tests.conftest import install_real_template, load_test_fleet, make_paths

from claudlobby.composer import compose_claude_md

CHECK = 'claudlobby --json message receipt MESSAGE_ID --destination "$FLEET_NAME/$BOT_ID" --wait 30'


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
        assert CHECK in text, bot.name
        assert "data.message_id" in text and "data.sender.alias" in text, bot.name
        assert "data.destination.alias" in text and "data.integrity_verdict" in text, bot.name
        assert "unexpected sender or destination" in text, bot.name
        assert "never accepts an assignment" in text, bot.name
        assert "plane-lookup.py" not in text, bot.name
        assert "between `plane:` and `⟧`" in text, bot.name
        assert "never the hex alone" in text, bot.name
