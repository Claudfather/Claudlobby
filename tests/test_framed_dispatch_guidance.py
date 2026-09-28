"""Every composed bot carries the verify-then-trust check for a dispatch framed
as pasted text (#1876): 9 of the estate's 21 bots composed neither protocol that
held it, although any pane can receive a framed dispatch.

#1946: the guidance also names the id EXTRACTION rule -- everything between
`plane:` and the closing bracket, `msg_` included -- because a receiver who
took only the part after `msg_` (the hex, prefix stripped) passed a shape the
door can never match, and it silently read as "not received yet" rather than
as a wrong input."""
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
        # #1946: msg_ is part of the id, not a separator to strip.
        assert "the id is everything between" in text, bot.name
        assert "never the hex alone" in text, bot.name
