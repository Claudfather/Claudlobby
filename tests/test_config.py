"""Tests for config.py — _coerce_bot defaults and fleet loading."""

from __future__ import annotations

import logging

import pytest

from claudlobby.config import (
    DEFAULT_MARKETPLACES,
    DEFAULT_PLUGINS,
    AutonomousRunnerBypass,
    AutonomousRunnerConfig,
    AutonomousRunnerPicker,
    _coerce_bot,
    _coerce_plugins,
    load_fleet,
)


class TestCoercePlugins:
    def test_defaults_applied_when_no_plugins_section(self):
        result = _coerce_plugins(None)
        assert result.required == DEFAULT_PLUGINS
        assert result.marketplaces == DEFAULT_MARKETPLACES
        assert result.include_defaults is True

    def test_defaults_applied_when_empty(self):
        result = _coerce_plugins({})
        assert result.required == DEFAULT_PLUGINS
        assert result.marketplaces == DEFAULT_MARKETPLACES

    def test_additional_merges_with_defaults(self):
        result = _coerce_plugins({"additional": ["telegram@claude-plugins-official"]})
        assert result.required == [
            "claudna@Claudfather",
            "telegram@claude-plugins-official",
        ]
        assert "Claudfather" in result.marketplaces

    def test_additional_dedup_with_defaults(self):
        result = _coerce_plugins({"additional": ["claudna@Claudfather"]})
        assert result.required == ["claudna@Claudfather"]
        assert result.required.count("claudna@Claudfather") == 1

    def test_include_defaults_false(self):
        result = _coerce_plugins({"include_defaults": False})
        assert result.required == []
        assert result.marketplaces == {}
        assert result.include_defaults is False

    def test_include_defaults_false_with_additional(self):
        result = _coerce_plugins(
            {
                "include_defaults": False,
                "additional": ["telegram@claude-plugins-official"],
            }
        )
        assert result.required == ["telegram@claude-plugins-official"]
        assert result.marketplaces == {}

    def test_custom_marketplace_merges_with_defaults(self):
        result = _coerce_plugins(
            {
                "marketplaces": {
                    "MyMarket": {"source": {"source": "github", "repo": "Org/Repo"}},
                },
            }
        )
        assert "Claudfather" in result.marketplaces
        assert "MyMarket" in result.marketplaces

    def test_custom_marketplace_overrides_default(self):
        custom = {"source": {"source": "github", "repo": "Fork/clauDNA"}}
        result = _coerce_plugins(
            {
                "marketplaces": {"Claudfather": custom},
            }
        )
        assert result.marketplaces["Claudfather"] == custom

    def test_deprecated_required_key_treated_as_additional(self, caplog):
        result = _coerce_plugins({"required": ["telegram@claude-plugins-official"]})
        assert "telegram@claude-plugins-official" in result.required
        assert "claudna@Claudfather" in result.required  # defaults still present
        assert "deprecated" in caplog.text


class TestCoerceBot:
    def test_minimal_bot(self):
        bot = _coerce_bot("test", {"expertise": ["software-engineering"]}, {})
        assert bot.name == "test"
        assert bot.expertise == ["software-engineering"]
        assert bot.account == "default"
        assert bot.model is None
        assert bot.remote_control is True
        assert bot.dangerously_skip_permissions is True
        assert bot.prompt_suggestions is False
        assert bot.channels == ["plugin:telegram@claude-plugins-official"]
        assert bot.mcp == []
        assert bot.skills == []
        assert bot.guardrails == []
        assert bot.bench is False

    def test_bench_from_bot(self):
        bot = _coerce_bot("test", {"expertise": ["eng"], "bench": True}, {})
        assert bot.bench is True

    def test_bench_from_defaults(self):
        bot = _coerce_bot("test", {"expertise": ["eng"]}, {"bench": True})
        assert bot.bench is True

    def test_bench_bot_overrides_defaults(self):
        bot = _coerce_bot(
            "test", {"expertise": ["eng"], "bench": False}, {"bench": True}
        )
        assert bot.bench is False

    def test_defaults_merge(self):
        defaults = {
            "model": "opus",
            "guardrails": ["no-push-main"],
            "protocols": ["report-back"],
        }
        bot = _coerce_bot("test", {"expertise": ["eng"]}, defaults)
        assert bot.model == "opus"
        assert bot.guardrails == ["no-push-main"]
        assert bot.protocols == ["report-back"]

    def test_bot_overrides_defaults(self):
        defaults = {"model": "sonnet"}
        raw = {"expertise": ["eng"], "model": "opus"}
        bot = _coerce_bot("test", raw, defaults)
        assert bot.model == "opus"

    def test_expertise_from_list(self):
        bot = _coerce_bot("test", {"expertise": ["a", "b"]}, {})
        assert bot.expertise == ["a", "b"]

    def test_expertise_from_string(self):
        bot = _coerce_bot("test", {"expertise": "solo"}, {})
        assert bot.expertise == ["solo"]

    def test_missing_expertise_raises(self):
        with pytest.raises(ValueError, match="missing required field 'expertise'"):
            _coerce_bot("test", {}, {})

    def test_deprecated_persona_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="claudlobby.config"):
            bot = _coerce_bot("test", {"persona": "old-role"}, {})
        assert bot.expertise == ["old-role"]
        assert "deprecated" in caplog.text

    def test_telegram_config(self):
        raw = {
            "expertise": ["eng"],
            "telegram": {
                "handle": "my_bot",
                "token_env": "MY_TOKEN",
                "require_mention": False,
                "chat_id": "-100123",
            },
        }
        bot = _coerce_bot("test", raw, {})
        assert bot.telegram.handle == "my_bot"
        assert bot.telegram.token_env == "MY_TOKEN"
        assert bot.telegram.require_mention is False
        assert bot.telegram.chat_id == "-100123"

    def test_telegram_defaults_merge(self):
        defaults = {"telegram": {"token_env": "FLEET_TOKEN", "require_mention": True}}
        raw = {
            "expertise": ["eng"],
            "telegram": {"handle": "my_bot", "require_mention": False},
        }
        bot = _coerce_bot("test", raw, defaults)
        assert bot.telegram.handle == "my_bot"
        assert bot.telegram.token_env == "FLEET_TOKEN"  # inherited from defaults
        assert bot.telegram.require_mention is False  # bot override wins

    def test_telegram_defaults_only(self):
        defaults = {"telegram": {"token_env": "FLEET_TOKEN"}}
        raw = {"expertise": ["eng"], "telegram": {"handle": "bot1"}}
        bot = _coerce_bot("test", raw, defaults)
        assert bot.telegram.token_env == "FLEET_TOKEN"
        assert bot.telegram.handle == "bot1"

    def test_lists_dedup(self):
        defaults = {"guardrails": ["a", "b"]}
        raw = {"expertise": ["eng"], "guardrails": ["b", "c"]}
        bot = _coerce_bot("test", raw, defaults)
        assert bot.guardrails == ["a", "b", "c"]

    def test_permissions_from_bot(self):
        raw = {"expertise": ["eng"], "permissions": ["read-only-db", "no-delete"]}
        bot = _coerce_bot("test", raw, {})
        assert bot.permissions == ["read-only-db", "no-delete"]

    def test_permissions_merge_with_defaults(self):
        defaults = {"permissions": ["read-only-db"]}
        raw = {"expertise": ["eng"], "permissions": ["no-delete"]}
        bot = _coerce_bot("test", raw, defaults)
        assert bot.permissions == ["read-only-db", "no-delete"]

    def test_permissions_default_empty(self):
        bot = _coerce_bot("test", {"expertise": ["eng"]}, {})
        assert bot.permissions == []

    def test_bot_id_defaults_to_key(self):
        bot = _coerce_bot("greg", {"expertise": ["eng"]}, {})
        assert bot.bot_id == "greg"
        assert bot.name == "greg"

    def test_bot_id_with_custom_name(self):
        bot = _coerce_bot("greg", {"expertise": ["eng"], "name": "Greg"}, {})
        assert bot.bot_id == "greg"
        assert bot.name == "Greg"

    def test_reports_to(self):
        bot = _coerce_bot("worker", {"expertise": ["eng"], "reports_to": "lead"}, {})
        assert bot.reports_to == "lead"

    def test_manages(self):
        bot = _coerce_bot("lead", {"expertise": ["eng"], "manages": ["w1", "w2"]}, {})
        assert bot.manages == ["w1", "w2"]

    def test_manages_from_string(self):
        bot = _coerce_bot("lead", {"expertise": ["eng"], "manages": "w1"}, {})
        assert bot.manages == ["w1"]

    def test_reports_to_and_manages_default_none(self):
        bot = _coerce_bot("test", {"expertise": ["eng"]}, {})
        assert bot.reports_to is None
        assert bot.manages is None

    def test_tools_default_empty(self):
        bot = _coerce_bot("test", {"expertise": ["eng"]}, {})
        assert bot.tools.deny == []
        assert bot.tools.allow == []

    def test_tools_deny_list(self):
        raw = {"expertise": ["eng"], "tools": {"deny": ["Write", "Edit"]}}
        bot = _coerce_bot("test", raw, {})
        assert bot.tools.deny == ["Write", "Edit"]
        assert bot.tools.allow == []

    def test_tools_allow_list(self):
        raw = {"expertise": ["eng"], "tools": {"allow": ["Read", "Grep"]}}
        bot = _coerce_bot("test", raw, {})
        assert bot.tools.allow == ["Read", "Grep"]

    def test_tools_deny_and_allow(self):
        raw = {"expertise": ["eng"], "tools": {"deny": ["Write"], "allow": ["Read"]}}
        bot = _coerce_bot("test", raw, {})
        assert bot.tools.deny == ["Write"]
        assert bot.tools.allow == ["Read"]

    def test_tools_merge_with_defaults(self):
        defaults = {"tools": {"deny": ["Write"]}}
        raw = {"expertise": ["eng"], "tools": {"deny": ["Edit"]}}
        bot = _coerce_bot("test", raw, defaults)
        assert bot.tools.deny == ["Write", "Edit"]

    def test_tools_merge_dedup(self):
        defaults = {"tools": {"deny": ["Write", "Edit"]}}
        raw = {"expertise": ["eng"], "tools": {"deny": ["Edit", "NotebookEdit"]}}
        bot = _coerce_bot("test", raw, defaults)
        assert bot.tools.deny == ["Write", "Edit", "NotebookEdit"]

    def test_model_strategy_full(self):
        raw = {
            "expertise": ["eng"],
            "model_strategy": {
                "base": "sonnet",
                "escalate_to": "opus",
                "escalate_when": ">5 files",
                "compact_when": ">50% context",
                "explore": "haiku",
                "plan": "sonnet",
            },
        }
        bot = _coerce_bot("test", raw, {})
        assert bot.model_strategy is not None
        assert bot.model_strategy.base == "sonnet"
        assert bot.model_strategy.escalate_to == "opus"
        assert bot.model_strategy.escalate_when == ">5 files"
        assert bot.model_strategy.compact_when == ">50% context"
        assert bot.model_strategy.raw["explore"] == "haiku"
        assert bot.model_strategy.raw["plan"] == "sonnet"

    def test_model_strategy_none_by_default(self):
        bot = _coerce_bot("test", {"expertise": ["eng"]}, {})
        assert bot.model_strategy is None

    def test_model_strategy_from_defaults(self):
        defaults = {
            "model_strategy": {"base": "sonnet", "escalate_to": "opus"},
        }
        bot = _coerce_bot("test", {"expertise": ["eng"]}, defaults)
        assert bot.model_strategy is not None
        assert bot.model_strategy.base == "sonnet"
        assert bot.model_strategy.escalate_to == "opus"


class TestLoadFleet:
    def test_load_minimal(self, fleet_dir):
        fleet = load_fleet(fleet_dir / "fleet.yaml")
        assert fleet.name == "test-fleet"
        assert len(fleet.bots) == 2
        assert "lead" in fleet.bots
        assert "worker-1" in fleet.bots

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_fleet(tmp_path / "nonexistent.yaml")

    def test_bad_yaml_structure(self, tmp_path):
        (tmp_path / "bad.yaml").write_text("not_fleet:\n  key: val\n")
        with pytest.raises(ValueError, match="'fleet' missing"):
            load_fleet(tmp_path / "bad.yaml")


class TestAutonomousRunnerConfig:
    def test_default_none_when_omitted(self):
        bot = _coerce_bot("test", {"expertise": ["eng"]}, {})
        assert bot.autonomous_runner is None

    def test_minimal_dataclass_construction(self):
        cfg = AutonomousRunnerConfig(
            skill="/claudna:tech-debt", cadence="1h", target_repo="org/repo"
        )
        assert cfg.skill == "/claudna:tech-debt"
        assert cfg.args == ""
        assert cfg.picker is None
        assert cfg.bypass is None
        assert cfg.pre_hooks == []
        assert cfg.post_hooks == []
        assert cfg.on_outcome == {}

    def test_full_dataclass_construction(self):
        cfg = AutonomousRunnerConfig(
            skill="/claudna:implement-plan",
            cadence="2h",
            target_repo="artemis-xyz/dbt",
            args="--source github",
            picker=AutonomousRunnerPicker(
                type="github_issues",
                label="claudna-eligible",
                state="open",
                score_by="mission_alignment",
            ),
            bypass=AutonomousRunnerBypass(
                risk_classifier="structural_vs_mechanical",
                block_on=["structural"],
                on_bypass="comment_and_label",
            ),
            pre_hooks=["/claudna:adversarial-review"],
            post_hooks=["/claudna:simplify"],
            on_outcome={"blocked": "report_and_pause"},
        )
        assert cfg.picker.score_by == "mission_alignment"
        assert cfg.bypass.block_on == ["structural"]
        assert cfg.on_outcome["blocked"] == "report_and_pause"

    def test_coerce_minimal_block(self):
        raw = {
            "expertise": ["eng"],
            "autonomous_runner": {
                "skill": "/claudna:tech-debt",
                "cadence": "1h",
                "target_repo": "org/repo",
            },
        }
        bot = _coerce_bot("test", raw, {})
        assert bot.autonomous_runner is not None
        assert bot.autonomous_runner.skill == "/claudna:tech-debt"
        assert bot.autonomous_runner.cadence == "1h"
        assert bot.autonomous_runner.target_repo == "org/repo"
        assert bot.autonomous_runner.args == ""
        assert bot.autonomous_runner.picker is None

    def test_coerce_full_block(self):
        raw = {
            "expertise": ["eng"],
            "autonomous_runner": {
                "skill": "/claudna:implement-plan",
                "cadence": "2h",
                "target_repo": "artemis-xyz/dbt",
                "args": "--source github",
                "picker": {
                    "type": "github_issues",
                    "label": "claudna-eligible",
                    "state": "open",
                    "score_by": "mission_alignment",
                },
                "bypass": {
                    "risk_classifier": "structural_vs_mechanical",
                    "block_on": ["structural"],
                    "on_bypass": "comment_and_label",
                },
                "pre_hooks": ["/claudna:adversarial-review"],
                "post_hooks": ["/claudna:simplify"],
                "on_outcome": {
                    "completed": "report",
                    "blocked": "report_and_pause",
                },
            },
        }
        bot = _coerce_bot("dbt-bot", raw, {})
        ar = bot.autonomous_runner
        assert ar.args == "--source github"
        assert ar.picker.label == "claudna-eligible"
        assert ar.bypass.block_on == ["structural"]
        assert "/claudna:adversarial-review" in ar.pre_hooks
        assert ar.on_outcome["blocked"] == "report_and_pause"

    @pytest.mark.parametrize("missing", ["skill", "cadence", "target_repo"])
    def test_missing_required_field_raises(self, missing):
        block = {
            "skill": "/claudna:tech-debt",
            "cadence": "1h",
            "target_repo": "org/repo",
        }
        del block[missing]
        raw = {"expertise": ["eng"], "autonomous_runner": block}
        with pytest.raises(ValueError, match=f"missing required field '{missing}'"):
            _coerce_bot("test", raw, {})

    def test_non_dict_block_raises(self):
        raw = {"expertise": ["eng"], "autonomous_runner": "not-a-dict"}
        with pytest.raises(ValueError, match="must be a mapping"):
            _coerce_bot("test", raw, {})

    def test_picker_defaults_applied(self):
        raw = {
            "expertise": ["eng"],
            "autonomous_runner": {
                "skill": "/claudna:tech-debt",
                "cadence": "1h",
                "target_repo": "org/repo",
                "picker": {"label": "ok-to-auto"},
            },
        }
        bot = _coerce_bot("test", raw, {})
        assert bot.autonomous_runner.picker.type == "github_issues"
        assert bot.autonomous_runner.picker.state == "open"
        assert bot.autonomous_runner.picker.score_by == "recency"
        assert bot.autonomous_runner.picker.label == "ok-to-auto"


class TestObservabilityConfig:
    def test_defaults(self):
        bot = _coerce_bot("test", {"expertise": ["eng"]}, {})
        assert bot.observability.pulse_interval == 300
        assert bot.observability.reap_days == 7

    def test_from_bot(self):
        raw = {
            "expertise": ["eng"],
            "observability": {"pulse_interval": 60, "reap_days": 14},
        }
        bot = _coerce_bot("test", raw, {})
        assert bot.observability.pulse_interval == 60
        assert bot.observability.reap_days == 14

    def test_from_defaults(self):
        defaults = {"observability": {"pulse_interval": 120, "reap_days": 30}}
        bot = _coerce_bot("test", {"expertise": ["eng"]}, defaults)
        assert bot.observability.pulse_interval == 120
        assert bot.observability.reap_days == 30

    def test_bot_overrides_defaults(self):
        defaults = {"observability": {"pulse_interval": 120, "reap_days": 30}}
        raw = {"expertise": ["eng"], "observability": {"pulse_interval": 60}}
        bot = _coerce_bot("test", raw, defaults)
        assert bot.observability.pulse_interval == 60
        assert bot.observability.reap_days == 30  # inherited from defaults

    def test_partial_bot_inherits_defaults(self):
        defaults = {"observability": {"pulse_interval": 120, "reap_days": 30}}
        raw = {"expertise": ["eng"], "observability": {"reap_days": 3}}
        bot = _coerce_bot("test", raw, defaults)
        assert bot.observability.pulse_interval == 120  # inherited from defaults
        assert bot.observability.reap_days == 3

    def test_bot_explicit_default_value_not_dropped(self):
        """Bot explicitly sets pulse_interval to 300 (the hardcoded default) —
        must not be silently overridden by fleet default of 120."""
        defaults = {"observability": {"pulse_interval": 120}}
        raw = {"expertise": ["eng"], "observability": {"pulse_interval": 300}}
        bot = _coerce_bot("test", raw, defaults)
        assert bot.observability.pulse_interval == 300  # bot wins, not fleet's 120
