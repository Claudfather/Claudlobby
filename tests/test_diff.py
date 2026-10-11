"""Tests for claudlobby/diff.py — rendered drift detection."""

from __future__ import annotations

import json

from claudlobby.config import load_fleet
from claudlobby.diff import diff_bot, diff_fleet_timers
from claudlobby.composer import compose_bot
from tests.package_fixtures import source_package
from claudlobby.paths import Paths


# ---------------------------------------------------------------------------
# diff_bot
# ---------------------------------------------------------------------------


class TestDiffBot:
    def test_unknown_bot(self, fleet_dir):
        paths = Paths(root=fleet_dir, package=source_package())
        fleet, _md = load_fleet(paths.fleet_yaml)
        result = diff_bot("nonexistent", fleet, paths)
        assert "not in fleet.yaml" in result

    def test_missing_runtime_dir(self, fleet_dir):
        paths = Paths(root=fleet_dir, package=source_package())
        fleet, _md = load_fleet(paths.fleet_yaml)
        result = diff_bot("lead", fleet, paths)
        assert "does not exist" in result

    def test_no_drift_after_generate(self, fleet_dir):
        paths = Paths(root=fleet_dir, package=source_package())
        fleet, _md = load_fleet(paths.fleet_yaml)
        compose_bot(fleet.bots["lead"], fleet, paths)
        result = diff_bot("lead", fleet, paths)
        assert "no drift" in result

    def test_claude_md_drift_detected(self, fleet_dir):
        paths = Paths(root=fleet_dir, package=source_package())
        fleet, _md = load_fleet(paths.fleet_yaml)
        compose_bot(fleet.bots["lead"], fleet, paths)
        # Modify the generated CLAUDE.md
        md_path = paths.bot_runtime("lead") / "CLAUDE.md"
        md_path.write_text(md_path.read_text() + "\n# Hand-edited section\n")
        result = diff_bot("lead", fleet, paths)
        assert "CLAUDE.md drift" in result
        assert "Hand-edited section" in result

    def test_bot_conf_drift_detected(self, fleet_dir):
        """bot.conf is a generated file; a stale one (missing an env line the
        composer now emits — e.g. a new fleet.yaml observability field) must
        show as drift. Without this, `diff` reads "no drift" while runtime
        bot.conf is out of sync — the false signal that hid the #591
        bridge_heal deploy."""
        paths = Paths(root=fleet_dir, package=source_package())
        fleet, _md = load_fleet(paths.fleet_yaml)
        compose_bot(fleet.bots["lead"], fleet, paths)
        conf_path = paths.bot_runtime("lead") / "bot.conf"
        # A stale line the composer would not produce → drift the tool must surface.
        conf_path.write_text(conf_path.read_text() + "\nexport STALE_DRIFT_MARKER=1\n")
        result = diff_bot("lead", fleet, paths)
        assert "bot.conf drift" in result
        assert "STALE_DRIFT_MARKER" in result

    def test_mcp_json_drift_detected(self, fleet_dir, monkeypatch):
        # Add github mcp to lead bot
        from textwrap import dedent

        fleet_yaml = fleet_dir / "fleet.yaml"
        fleet_yaml.write_text(
            dedent("""\
            fleet:
              manager: lead
              name: test-fleet
              service_prefix: com.test
              telegram_group_chat_id: "-100999"
              accounts:
                default: ~/.claude
              defaults:
                model: opus
                guardrails: [no-push-main]
                protocols: [report-back]
              bots:
                lead:
                  expertise: [orchestration]
                  mcp: [github]
                  telegram:
                    handle: lead_bot
                    token_env: TELEGRAM_TOKEN_LEAD
        """)
        )
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        paths = Paths(root=fleet_dir, package=source_package())
        fleet, _md = load_fleet(paths.fleet_yaml)
        compose_bot(fleet.bots["lead"], fleet, paths)

        # Tamper with .mcp.json
        mcp_path = paths.bot_runtime("lead") / ".mcp.json"
        mcp_data = json.loads(mcp_path.read_text())
        mcp_data["mcpServers"]["extra"] = {"command": "echo"}
        mcp_path.write_text(json.dumps(mcp_data))

        result = diff_bot("lead", fleet, paths)
        assert ".mcp.json drift" in result


# ---------------------------------------------------------------------------
# diff_fleet_timers — generate-vs-diff Paths-surface invariant (#441)
# ---------------------------------------------------------------------------


class TestDiffFleetTimers:
    def test_passes_real_paths_not_partial_shadow(self, fleet_dir, monkeypatch):
        """Regression for #441: diff_fleet_timers must hand compose_fleet_timers the
        SAME real Paths object that generate uses — not a 2-attribute shadow.

        A partial shadow exposes only root/runtime_fleet, so any paths.* access the
        timer path grows later AttributeErrors under `claudlobby diff` while
        `claudlobby generate` (real Paths) keeps working — a silent divergence.
        """
        paths = Paths(root=fleet_dir, package=source_package())
        fleet, merged = load_fleet(paths.fleet_yaml)
        # diff_fleet_timers early-returns unless the actual timers dir exists.
        (paths.runtime_fleet / "timers").mkdir(parents=True, exist_ok=True)

        captured: dict = {}

        def fake_compose_fleet_timers(fleet_, paths_, merged_, output_dir=None):
            captured["paths"] = paths_

        # diff_fleet_timers imports compose_fleet_timers from .composer at call time,
        # so patching the composer attribute is picked up.
        monkeypatch.setattr(
            "claudlobby.composer.compose_fleet_timers", fake_compose_fleet_timers
        )

        diff_fleet_timers(fleet, paths, merged)

        assert isinstance(captured.get("paths"), Paths), (
            f"diff_fleet_timers passed a {type(captured.get('paths')).__name__} instead of "
            "a real Paths; any paths.* access beyond root/runtime_fleet AttributeErrors in "
            "diff but not generate"
        )


def test_named_github_binding_document_and_gitconfig_drift(fleet_dir):
    import hashlib
    from claudlobby.config import ProjectConfig
    from claudlobby.github_connections import GithubAssignment, GithubAttachment, GithubConnection

    paths = Paths(root=fleet_dir, package=source_package())
    fleet, _md = load_fleet(paths.fleet_yaml)
    fleet.github_connections_enabled = True
    fleet.projects['service'] = ProjectConfig(key='service', title='Service', repos=['example/service'])
    fleet.github_connections['team'] = GithubConnection(
        connection_id='team', name='Team', mode='independent', app_id=101, installation_id=201,
        permissions=(('contents', 'read'),), credential_refs=(('private_key_path_env', 'TEAM_KEY_PATH'),),
    )
    fleet.github_attachments['attached'] = GithubAttachment(attachment_id='attached', connection='team')
    bot = fleet.bots['lead']
    bot.github_assignments['service'] = GithubAssignment(
        assignment_id='service', attachment='attached', project='service',
    )
    compose_bot(bot, fleet, paths)
    bot_dir = paths.bot_runtime('lead')
    assert 'no drift' in diff_bot('lead', fleet, paths)
    binding_path = bot_dir / 'github-bindings.json'
    document = binding_path.read_text()
    digest = hashlib.sha256(document.encode()).hexdigest()
    assert 'CLAUDLOBBY_GITHUB_BINDING_SHA256=' + digest in (bot_dir / 'bot.conf').read_text()
    servers = json.loads((bot_dir / '.mcp.json').read_text())['mcpServers']
    github_servers = [server for name, server in servers.items() if name.startswith('github-app-')]
    assert len(github_servers) == 1
    assert github_servers[0]['env']['CLAUDLOBBY_GITHUB_BINDING_SHA256'] == digest
    changed = json.loads(document)
    original_revision = changed['bindings'][0]['revision']
    changed['bindings'][0]['permissions']['contents'] = 'write'
    binding_path.write_text(json.dumps(changed))
    assert changed['bindings'][0]['revision'] == original_revision
    assert 'github-bindings.json drift' in diff_bot('lead', fleet, paths, metadata_only=True)
    binding_path.write_text(document)
    gitconfig = bot_dir / '.gitconfig'
    gitconfig.write_text(gitconfig.read_text() + '\n[credential]\n\thelper = operator-helper\n')
    result = diff_bot('lead', fleet, paths, metadata_only=True)
    assert '.gitconfig drift' in result
    assert 'github-bindings.json drift' not in result
