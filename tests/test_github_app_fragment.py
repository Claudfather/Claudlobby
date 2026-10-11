"""App-auth P2 (#1272): library/mcp/github-app.json — the REAL shipped fragment.

These pins run against the repo's own library (Paths at REPO_ROOT), not a
synthetic fixture: the contract under test is the file a fleet equips.
"""

import json
from pathlib import Path

from claudlobby.composer import compose_mcp_json, compose_settings_local
from claudlobby.config import BotConfig, FleetConfig, McpEntry
from claudlobby.mcp_resolve import required_vars

from tests.conftest import make_paths

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
    return make_paths(REPO_ROOT)


class TestFragmentContract:
    def test_fragment_parses(self):
        json.loads(FRAGMENT.read_text())

    def test_contract_is_exactly_the_three_app_vars(self):
        vars_ = list(required_vars(_bot(), _paths()))
        names = {v.canonical_name for v in vars_}
        # Exactly the three — and therefore NOT GITHUB_PAT: no optional:
        # field exists in the contract schema, so declaring it here would be
        # a creds-reconcile shape-1 FAIL for every pure-App fleet;
        # github.json owns that declaration.
        assert names == APP_VARS

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
        assert "${CLAUDLOBBY_NATIVE_DIR}/github-app-mcp-wrapper.py" in joined
        assert str(REPO_ROOT) not in joined, "anchor must not be baked at compose time"

    def test_composed_server_name_is_the_entry_name(self):
        # Tools are mcp__github-app__* (entry-name derivation) — no collision
        # with github.json's mcp__github__* even when both are equipped.
        mcp = compose_mcp_json(_bot(), _paths())
        assert list(mcp["mcpServers"].keys()) == ["github-app"]

    def test_permission_grants_use_the_github_app_prefix(self):
        bot = _bot()
        fleet = FleetConfig(manager="worker", name="t", service_prefix="p", bots={"worker": bot})
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
        wrapper_src = (REPO_ROOT / "claudlobby/_runtime_scripts" / "github-app-mcp-wrapper.py").read_text()
        assert len(pinned) == 1
        assert pinned[0] in wrapper_src

    def test_tools_contract_matches_github_json(self):
        # Same coupling as the package pin, symmetric: same pinned server →
        # same tool universe. A tool added to github.json's contract alone
        # would leave App-identity bots without the composed allow-grant —
        # surfacing as a runtime permission prompt with nothing red at
        # compose or test time.
        github = json.loads((REPO_ROOT / "library" / "mcp" / "github.json").read_text())
        app = json.loads(FRAGMENT.read_text())
        assert (
            app["_permissions_contract"]["tools"]
            == github["_permissions_contract"]["tools"]
        )

    def test_path_audit_accepts_the_referencing_bot(self):
        # The L1 deny-by-default source guard, run for real against a bot that
        # EQUIPS the fragment (spec gap flagged on #1283: the /bin/sh + braced-
        # anchor choices exist to satisfy this gate, so the record must show
        # the gate passing, not assert it by design).
        from claudlobby import path_audit

        bot = _bot()
        fleet = FleetConfig(manager="worker", name="t", service_prefix="p", bots={"worker": bot})
        findings = path_audit.audit_bot_sources(bot, fleet, paths=_paths())
        assert findings == [], [str(f) for f in findings]

    def test_both_github_fragments_compose_side_by_side(self):
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            mcp=[McpEntry(name="github"), McpEntry(name="github-app")],
        )
        mcp = compose_mcp_json(bot, _paths())
        assert set(mcp["mcpServers"]) == {"github", "github-app"}


class TestNamedConnectionComposition:
    def test_separate_repository_instances_preserve_distinct_permissions(self, monkeypatch):
        from claudlobby import composer
        rows = [
            {'assignment_id': 'read', 'repo': 'example/one', 'revision': 'rev-read', 'connection_id': 'first'},
            {'assignment_id': 'write', 'repo': 'example/two', 'revision': 'rev-write', 'connection_id': 'second'},
        ]
        monkeypatch.setattr(composer, '_named_github_rows', lambda *args: rows)
        original_fragment = composer._load_mcp_fragment
        monkeypatch.setattr(composer, '_load_mcp_fragment', lambda name, paths:
                            {'github-metrics': {'command': 'metrics-cli'}} if name == 'github-metrics'
                            else original_fragment(name, paths))
        bot = _bot()
        fleet = FleetConfig(manager='worker', name='t', service_prefix='p', bots={'worker': bot},
                            github_connections_enabled=True)
        bot.mcp.extend([McpEntry(name='github'), McpEntry(name='github-metrics')])
        mcp = composer.compose_mcp_json(bot, _paths(), fleet=fleet)
        assert composer.mcp_launch_plan(bot, _paths(), fleet=fleet)[0] == mcp
        assert mcp['mcpServers']['github-metrics'] == {'command': 'metrics-cli'}
        servers = {name: value for name, value in mcp['mcpServers'].items() if name != 'github-metrics'}
        assert len(servers) == 2
        assert {s['env']['CLAUDLOBBY_GITHUB_REPO'] for s in servers.values()} == {'example/one', 'example/two'}
        assert {s['env']['CLAUDLOBBY_GITHUB_REVISION'] for s in servers.values()} == {'rev-read', 'rev-write'}
        assert all('GITHUB_PAT' not in s['env'] for s in servers.values())
        assert all(name.startswith('github-app-') for name in servers)
        settings = composer.compose_settings_local(bot, fleet, _paths())
        assert settings['enabledMcpjsonServers'] == sorted(mcp['mcpServers'])
        assert all(any(grant.startswith('mcp__' + name + '__')
                       for grant in settings['permissions']['allow']) for name in servers)

    def test_named_fleet_unassigned_bot_has_deny_all_consumers(self, tmp_path):
        from claudlobby import composer
        bot = BotConfig(bot_id='worker', name='worker', expertise=['software-engineering'],
                        mcp=[McpEntry(name='github')], git_credentials={'example': 'GITHUB_PAT'})
        fleet = FleetConfig(manager='worker', name='t', service_prefix='p',
                            bots={'worker': bot}, github_connections_enabled=True)
        assert composer.compose_mcp_json(bot, _paths(), fleet=fleet)['mcpServers'] == {}
        assert composer.collect_env_contracts(fleet, _paths()) == []
        files = composer.render_bot_files(bot, fleet, make_paths(tmp_path), boot_delay_s=0, cascade={})
        config = json.loads(files['github-bindings.json'].content)
        assert config['bindings'] == []
        assert json.loads(files['.mcp.json'].content)['mcpServers'] == {}
        assert 'CLAUDLOBBY_GITHUB_STRICT=1' in files['bot.conf'].content
        assert 'github-binding-runtime.py' in files['tools/gh'].content
        assert 'GITHUB_PAT' not in files['.gitconfig'].content
        assert 'auth git-credential' not in files['.gitconfig'].content
        assert 'cache --timeout' not in files['.gitconfig'].content
