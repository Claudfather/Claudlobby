"""Tests for config.py — _coerce_bot defaults and fleet loading."""
from __future__ import annotations

import pytest
from pathlib import Path

from claudlobby.config import _coerce_bot, load_fleet, BotConfig


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

    def test_deprecated_persona_warns(self, capsys):
        bot = _coerce_bot("test", {"persona": "old-role"}, {})
        assert bot.expertise == ["old-role"]
        assert "deprecated" in capsys.readouterr().err

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

    def test_lists_dedup(self):
        defaults = {"guardrails": ["a", "b"]}
        raw = {"expertise": ["eng"], "guardrails": ["b", "c"]}
        bot = _coerce_bot("test", raw, defaults)
        assert bot.guardrails == ["a", "b", "c"]


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
