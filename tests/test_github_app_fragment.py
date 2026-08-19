"""App-auth P2 (#1272): library/mcp/github-app.json — the REAL shipped fragment.

These pins run against the repo's own library (Paths at REPO_ROOT), not a
synthetic fixture: the contract under test is the file a fleet equips.
"""

import json
from pathlib import Path

from claudlobby.composer import compose_mcp_json, compose_settings_local
from claudlobby.config import BotConfig, FleetConfig, McpEntry
from claudlobby.mcp_resolve import required_vars
from claudlobby.paths import Paths

REPO_ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = REPO_ROOT / "library" / "mcp" / "github-app.json"

APP_VARS = {
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
}


def _bot():
    return BotConfig(
        bot_id="worker",
        name="worker",
        expertise=["eng"],
        mcp=[McpEntry(name="github-app")],
    )


def _paths():
    return Paths(root=REPO_ROOT, fleet_dir=REPO_ROOT)


class TestFragmentContract:
    def test_fragment_parses(self):
        json.loads(FRAGMENT.read_text())

    def test_contract_is_exactly_the_three_app_vars(self):
        vars_ = list(required_vars(_bot(), _paths()))
        names = {v.canonical_name for v in vars_}
        assert names == APP_VARS
        # NOT GITHUB_PAT: no optional: field exists in the contract schema, so
        # declaring it here would make it a creds-reconcile shape-1 FAIL for
        # every pure-App fleet; github.json owns that declaration.
        assert "GITHUB_PAT" not in names

    def test_merged_1226_schema_fields(self):
        # secret: true on all three (the documented authenticate-without test:
        # the integration cannot authenticate without any of them), fleet
        # tier, and NO source — the vars are the mint INPUTS, human-supplied;
        # the mint:github-app resolver arm stays reserved (F9).
        for v in required_vars(_bot(), _paths()):
            assert v.secret is True, v.canonical_name
            assert v.default_tier == "fleet", v.canonical_name
            assert v.source is None, v.canonical_name

    def test_braced_anchor_survives_into_composed_mcp_json(self):
        # ${CLAUDLOBBY_ROOT} must be BRACED: the unbraced form dodges the
        # placeholder walk (mcp_resolve _VAR_RE) and would silently falsify
        # the audited-path claim. It must reach .mcp.json verbatim for
        # runtime expansion — never baked to an absolute path.
        mcp = compose_mcp_json(_bot(), _paths())
        server = mcp["mcpServers"]["github-app"]
        assert server["command"] == "/bin/sh"
        joined = " ".join(server["args"])
        assert "${CLAUDLOBBY_ROOT}/lib/github-app-mcp-wrapper.py" in joined
        assert str(REPO_ROOT) not in joined, "anchor must not be baked at compose time"

    def test_composed_server_name_is_the_entry_name(self):
        # Tools are mcp__github-app__* (entry-name derivation) — no collision
        # with github.json's mcp__github__* even when both are equipped.
        mcp = compose_mcp_json(_bot(), _paths())
        assert list(mcp["mcpServers"].keys()) == ["github-app"]

    def test_permission_grants_use_the_github_app_prefix(self):
        bot = _bot()
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        mcp = compose_mcp_json(bot, _paths())
        settings = compose_settings_local(
            bot, fleet, _paths(), list(mcp["mcpServers"].keys())
        )
        allow = settings["permissions"]["allow"]
        assert any(a.startswith("mcp__github-app__") for a in allow), allow
        assert settings["enabledMcpjsonServers"] == ["github-app"]

    def test_package_pin_matches_github_json(self):
        # The wrapper's respawn-transparency evidence is package-specific, so
        # the two fragments must move together.
        github = json.loads((REPO_ROOT / "library" / "mcp" / "github.json").read_text())
        pinned = [a for a in github["github"]["args"] if a.startswith("@modelcontextprotocol")]
        wrapper_src = (REPO_ROOT / "lib" / "github-app-mcp-wrapper.py").read_text()
        assert len(pinned) == 1
        assert pinned[0] in wrapper_src

    def test_both_github_fragments_compose_side_by_side(self):
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            mcp=[McpEntry(name="github"), McpEntry(name="github-app")],
        )
        mcp = compose_mcp_json(bot, _paths())
        assert set(mcp["mcpServers"]) == {"github", "github-app"}
