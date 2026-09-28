"""Every composed bot carries the verify-then-trust check for a dispatch framed
as pasted text (#1876): 9 of the estate's 21 bots composed neither protocol that
held it, although any pane can receive a framed dispatch."""
from tests.conftest import install_real_template, load_test_fleet, make_paths

from claudlobby.composer import compose_claude_md

CHECK = '--received <msg_id> --destination "$BOT_ID" --verdict --wait 30'


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
