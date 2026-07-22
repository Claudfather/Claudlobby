"""Tests for validator.py — validate() with missing env vars and library refs."""

from __future__ import annotations

from pathlib import Path

import pytest

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

    def test_missing_telegram_token_is_warning(self, fleet_dir, monkeypatch):
        monkeypatch.delenv("TELEGRAM_TOKEN_LEAD", raising=False)
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

    def test_reap_days_zero_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].observability.reap_days = 0
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any(
            "reap_days must be > 0" in w and "lead" in w for w in report.warnings
        )

    def test_reap_days_over_365_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["worker-1"].observability.reap_days = 500
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("reap_days > 365" in w and "worker-1" in w for w in report.warnings)

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


class TestHookCommandValidation:
    """Hook command path existence validation."""

    def _env_patch(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")

    def test_absolute_missing_command_warns(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].hooks = {
            "PreToolUse": [{"command": "/nonexistent/path/hook.sh"}],
        }
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any(
            "hook" in w and "/nonexistent/path/hook.sh" in w for w in report.warnings
        )

    def test_relative_command_not_checked(self, fleet_dir, monkeypatch):
        """Relative commands (like 'log.sh') are not validated — may be on PATH."""
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].hooks = {
            "PostToolUse": [{"command": "log.sh", "matcher": "Bash"}],
        }
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not any("hook" in w and "log.sh" in w for w in report.warnings)

    def test_existing_absolute_command_no_warn(self, fleet_dir, monkeypatch):
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        # /bin/true exists on all Unix systems
        fleet.bots["lead"].hooks = {
            "PreToolUse": [{"command": "/bin/true"}],
        }
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not any("hook" in w and "/bin/true" in w for w in report.warnings)

    def test_prompt_type_hooks_skip_command_check(self, fleet_dir, monkeypatch):
        """Hooks with type: prompt don't have file-based commands."""
        self._env_patch(monkeypatch)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].hooks = {
            "PreToolUse": [{"type": "prompt", "prompt": "Is this safe?"}],
        }
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not any("hook" in w and "not found" in w for w in report.warnings)


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
