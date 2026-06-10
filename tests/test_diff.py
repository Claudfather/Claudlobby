"""Tests for claudlobby/diff.py — drift detection and promote guidance."""
from __future__ import annotations

import json
from pathlib import Path

from claudlobby.config import load_fleet
from claudlobby.diff import diff_bot, promote_bot
from claudlobby.composer import compose_bot
from claudlobby.paths import Paths


# ---------------------------------------------------------------------------
# diff_bot
# ---------------------------------------------------------------------------


class TestDiffBot:
    def test_unknown_bot(self, fleet_dir):
        paths = Paths(root=fleet_dir)
        fleet, _md = load_fleet(paths.fleet_yaml)
        result = diff_bot("nonexistent", fleet, paths)
        assert "not in fleet.yaml" in result

    def test_missing_runtime_dir(self, fleet_dir):
        paths = Paths(root=fleet_dir)
        fleet, _md = load_fleet(paths.fleet_yaml)
        result = diff_bot("lead", fleet, paths)
        assert "does not exist" in result

    def test_no_drift_after_generate(self, fleet_dir):
        paths = Paths(root=fleet_dir)
        fleet, _md = load_fleet(paths.fleet_yaml)
        compose_bot(fleet.bots["lead"], fleet, paths)
        result = diff_bot("lead", fleet, paths)
        assert "no drift" in result

    def test_claude_md_drift_detected(self, fleet_dir):
        paths = Paths(root=fleet_dir)
        fleet, _md = load_fleet(paths.fleet_yaml)
        compose_bot(fleet.bots["lead"], fleet, paths)
        # Modify the generated CLAUDE.md
        md_path = paths.bot_runtime("lead") / "CLAUDE.md"
        md_path.write_text(md_path.read_text() + "\n# Hand-edited section\n")
        result = diff_bot("lead", fleet, paths)
        assert "CLAUDE.md drift" in result
        assert "Hand-edited section" in result

    def test_mcp_json_drift_detected(self, fleet_dir, monkeypatch):
        # Add github mcp to lead bot
        from textwrap import dedent
        fleet_yaml = fleet_dir / "fleet.yaml"
        fleet_yaml.write_text(dedent("""\
            fleet:
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
        """))
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        paths = Paths(root=fleet_dir)
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
# promote_bot
# ---------------------------------------------------------------------------


class TestPromoteBot:
    def test_unknown_bot(self, fleet_dir):
        paths = Paths(root=fleet_dir)
        fleet, _md = load_fleet(paths.fleet_yaml)
        result = promote_bot("nonexistent", fleet, paths)
        assert "not in fleet.yaml" in result

    def test_promote_output_structure(self, fleet_dir):
        paths = Paths(root=fleet_dir)
        fleet, _md = load_fleet(paths.fleet_yaml)
        result = promote_bot("lead", fleet, paths)
        assert "Promote workflow" in result
        assert "Review drift" in result
        assert "claudlobby diff lead" in result
        assert "Expertise content" in result
        assert "orchestration.md" in result
        assert "claudlobby generate" in result

    def test_promote_no_voice(self, fleet_dir):
        paths = Paths(root=fleet_dir)
        fleet, _md = load_fleet(paths.fleet_yaml)
        result = promote_bot("lead", fleet, paths)
        assert "create a voices/" in result

    def test_promote_with_voice(self, fleet_dir):
        from textwrap import dedent
        fleet_yaml = fleet_dir / "fleet.yaml"
        fleet_yaml.write_text(dedent("""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              telegram_group_chat_id: "-100999"
              accounts:
                default: ~/.claude
              defaults:
                model: opus
              bots:
                lead:
                  expertise: [orchestration]
                  voice: voices/erlich.md
                  telegram:
                    handle: lead_bot
                    token_env: TELEGRAM_TOKEN_LEAD
        """))
        paths = Paths(root=fleet_dir)
        fleet, _md = load_fleet(paths.fleet_yaml)
        result = promote_bot("lead", fleet, paths)
        assert "erlich.md" in result
