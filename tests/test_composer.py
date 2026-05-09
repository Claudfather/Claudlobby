"""Tests for composer.py — scaffold_env_files, access.json, and settings generation."""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from claudlobby.config import (
    BotConfig, FleetConfig, SandboxConfig, TelegramConfig,
    _coerce_sandbox, _merge_sandbox, load_fleet,
)
from claudlobby.composer import (
    compose_access_json, _reconcile_access_json,
    compose_settings_local, scaffold_env_files,
)
from claudlobby.paths import Paths


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root)


class TestScaffoldEnvMerge:
    """scaffold_env_files preserves existing values and appends new stubs."""

    def _setup_fleet(self, tmp_path: Path) -> tuple[Path, Paths]:
        """Minimal fleet layout with MCP env contract."""
        root = tmp_path / "claudlobby"
        root.mkdir()

        # Fleet.yaml with one bot using github MCP (needs GITHUB_PAT)
        (root / "fleet.yaml").write_text(dedent("""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                worker:
                  expertise: [eng]
                  mcp: [github, shopify]
                  telegram:
                    handle: w_bot
                    token_env: TG_TOKEN_W
        """))

        # Library dirs
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")

        (root / "library" / "mcp").mkdir(parents=True)
        (root / "library" / "mcp" / "github.json").write_text(json.dumps({
            "github": {"command": "gh", "args": ["mcp"]},
            "_env_contract": {
                "GITHUB_PAT": {"description": "GitHub PAT", "tier": "fleet"},
            },
        }))
        (root / "library" / "mcp" / "shopify.json").write_text(json.dumps({
            "shopify": {"command": "npx", "args": ["-y", "@ajackus/shopify-mcp-server"]},
            "_env_contract": {
                "SHOPIFY_ACCESS_TOKEN": {"description": "Shopify token", "tier": "fleet"},
                "SHOPIFY_STORE_DOMAIN": {"description": "Shopify domain", "tier": "fleet"},
            },
        }))

        # Runtime dir (scaffold_env_files runs after compose_bot which creates these)
        (root / "runtime" / "bots" / "worker").mkdir(parents=True)

        paths = _make_paths(root)
        return root, paths

    def test_creates_new_env_file(self, tmp_path):
        root, paths = self._setup_fleet(tmp_path)
        fleet = load_fleet(root / "fleet.yaml")

        scaffold_env_files(fleet, paths, log=lambda m: None)

        env_path = root / ".env"
        assert env_path.is_file()
        content = env_path.read_text()
        assert "GITHUB_PAT" in content
        assert "SHOPIFY_ACCESS_TOKEN" in content

    def test_preserves_existing_values_and_appends_new(self, tmp_path):
        root, paths = self._setup_fleet(tmp_path)
        fleet = load_fleet(root / "fleet.yaml")

        # Pre-populate with one var already set
        env_path = root / ".env"
        env_path.write_text(dedent("""\
            # Fleet environment for: test-fleet
            export GITHUB_PAT="ghp_realtoken123"
        """))

        messages: list[str] = []
        scaffold_env_files(fleet, paths, log=messages.append)

        content = env_path.read_text()
        # Existing value preserved verbatim
        assert 'export GITHUB_PAT="ghp_realtoken123"' in content
        # New vars appended as stubs
        assert "export SHOPIFY_ACCESS_TOKEN=" in content
        assert "export SHOPIFY_STORE_DOMAIN=" in content

        # Log message mentions what was added
        assert any("SHOPIFY_ACCESS_TOKEN" in m for m in messages)

    def test_idempotent_no_duplicates(self, tmp_path):
        root, paths = self._setup_fleet(tmp_path)
        fleet = load_fleet(root / "fleet.yaml")

        # Run scaffold twice
        scaffold_env_files(fleet, paths, log=lambda m: None)
        first_content = (root / ".env").read_text()

        scaffold_env_files(fleet, paths, log=lambda m: None)
        second_content = (root / ".env").read_text()

        assert first_content == second_content

    def test_no_op_logs_up_to_date(self, tmp_path):
        """When all vars already present, log 'up to date' instead of silence."""
        root, paths = self._setup_fleet(tmp_path)
        fleet = load_fleet(root / "fleet.yaml")

        # First run — creates the file
        scaffold_env_files(fleet, paths, log=lambda m: None)

        # Second run — should log "up to date"
        messages: list[str] = []
        scaffold_env_files(fleet, paths, log=messages.append)

        assert any("up to date" in m for m in messages)


# ── Access JSON tests ────────────────────────────────────────────────


def _make_bot(handle="test_bot", require_mention=True, chat_id=None):
    return BotConfig(
        bot_id="test",
        name="test",
        expertise=["eng"],
        telegram=TelegramConfig(
            handle=handle,
            require_mention=require_mention,
            chat_id=chat_id,
        ),
    )


def _make_fleet(group_chat_id="-1001234567890", human_id="12345"):
    return FleetConfig(
        name="test-fleet",
        service_prefix="com.test",
        telegram_group_chat_id=group_chat_id,
        human_telegram_id=human_id,
    )


class TestComposeAccessJson:
    """compose_access_json generates correct structure from fleet.yaml."""

    def test_basic_structure(self):
        bot = _make_bot(require_mention=True)
        fleet = _make_fleet()
        result = compose_access_json(bot, fleet)

        assert result is not None
        assert result["dmPolicy"] == "allowlist"
        assert result["allowFrom"] == ["12345"]
        assert "-1001234567890" in result["groups"]
        assert result["groups"]["-1001234567890"]["requireMention"] is True
        assert result["pending"] == {}

    def test_returns_none_without_handle(self):
        bot = _make_bot(handle=None)
        fleet = _make_fleet()
        assert compose_access_json(bot, fleet) is None

    def test_returns_none_without_chat_id(self):
        bot = _make_bot()
        fleet = _make_fleet(group_chat_id=None)
        assert compose_access_json(bot, fleet) is None

    def test_bot_chat_id_overrides_fleet(self):
        bot = _make_bot(chat_id="-999")
        fleet = _make_fleet(group_chat_id="-1001234567890")
        result = compose_access_json(bot, fleet)
        assert "-999" in result["groups"]
        assert "-1001234567890" not in result["groups"]

    def test_no_human_id_gives_empty_allowfrom(self):
        bot = _make_bot()
        fleet = _make_fleet(human_id=None)
        result = compose_access_json(bot, fleet)
        assert result["allowFrom"] == []


class TestReconcileAccessJson:
    """_reconcile_access_json preserves runtime state while updating fleet fields."""

    def test_preserves_pending_and_extra_groups(self, tmp_path):
        bot = _make_bot(require_mention=True)
        fleet = _make_fleet()
        fresh = compose_access_json(bot, fleet)

        # Pre-seed with runtime state
        access_path = tmp_path / "access.json"
        existing = {
            "dmPolicy": "allowlist",
            "allowFrom": ["12345"],
            "groups": {
                "-1001234567890": {"requireMention": False, "allowFrom": []},
                "-999999": {"requireMention": False, "allowFrom": ["67890"]},
            },
            "pending": {"abc123": {"user": "someone"}},
        }
        access_path.write_text(json.dumps(existing))

        _reconcile_access_json(access_path, fresh, bot, fleet, log=lambda m: None)

        result = json.loads(access_path.read_text())
        # Fleet field updated
        assert result["groups"]["-1001234567890"]["requireMention"] is True
        # Runtime state preserved
        assert "-999999" in result["groups"]
        assert result["groups"]["-999999"]["allowFrom"] == ["67890"]
        assert result["pending"] == {"abc123": {"user": "someone"}}

    def test_propagates_dm_policy(self, tmp_path):
        bot = _make_bot()
        fleet = _make_fleet()
        fresh = compose_access_json(bot, fleet)

        access_path = tmp_path / "access.json"
        existing = {"dmPolicy": "open", "allowFrom": [], "groups": {}, "pending": {}}
        access_path.write_text(json.dumps(existing))

        _reconcile_access_json(access_path, fresh, bot, fleet, log=lambda m: None)

        result = json.loads(access_path.read_text())
        assert result["dmPolicy"] == "allowlist"

    def test_non_dict_json_leaves_file_unchanged(self, tmp_path):
        bot = _make_bot()
        fleet = _make_fleet()
        fresh = compose_access_json(bot, fleet)

        access_path = tmp_path / "access.json"
        access_path.write_text("[]")

        messages = []
        _reconcile_access_json(access_path, fresh, bot, fleet, log=messages.append)

        assert access_path.read_text() == "[]"
        assert any("not a JSON object" in m for m in messages)

    def test_malformed_json_leaves_file_unchanged(self, tmp_path):
        bot = _make_bot()
        fleet = _make_fleet()
        fresh = compose_access_json(bot, fleet)

        access_path = tmp_path / "access.json"
        access_path.write_text("{invalid json")

        messages = []
        _reconcile_access_json(access_path, fresh, bot, fleet, log=messages.append)

        assert access_path.read_text() == "{invalid json"
        assert any("unreadable" in m for m in messages)

    def test_adds_human_id_without_duplicating(self, tmp_path):
        bot = _make_bot()
        fleet = _make_fleet(human_id="12345")
        fresh = compose_access_json(bot, fleet)

        access_path = tmp_path / "access.json"
        existing = {"dmPolicy": "allowlist", "allowFrom": ["12345"], "groups": {}, "pending": {}}
        access_path.write_text(json.dumps(existing))

        _reconcile_access_json(access_path, fresh, bot, fleet, log=lambda m: None)

        result = json.loads(access_path.read_text())
        assert result["allowFrom"].count("12345") == 1


# ── Sandbox / auto_allow_bash cascade tests ──────────────────────────


class TestAutoAllowBashCascade:
    """auto_allow_bash: None cascades from fleet defaults; explicit values win."""

    def _make_fleet(self, tmp_path, defaults_sandbox=None, bot_sandbox=None) -> tuple[FleetConfig, Paths]:
        root = tmp_path / "claudlobby"
        root.mkdir()

        defaults = {}
        if defaults_sandbox is not None:
            defaults["sandbox"] = defaults_sandbox

        bot_raw = {"expertise": ["eng"], "telegram": {"handle": "t_bot"}}
        if bot_sandbox is not None:
            bot_raw["sandbox"] = bot_sandbox

        fleet_yaml = {"fleet": {
            "name": "test", "service_prefix": "t",
            "defaults": defaults,
            "bots": {"worker": bot_raw},
        }}
        (root / "fleet.yaml").write_text(
            __import__("yaml").dump(fleet_yaml, default_flow_style=False)
        )
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
        (root / "runtime" / "bots" / "worker").mkdir(parents=True)

        paths = Paths(root=root, fleet_dir=root)
        fleet = load_fleet(root / "fleet.yaml")
        return fleet, paths

    def test_default_none_no_sandbox_key(self, tmp_path):
        """Neither fleet nor bot sets auto_allow_bash → no autoAllowBashIfSandboxed."""
        fleet, paths = self._make_fleet(tmp_path)
        bot = fleet.bots["worker"]
        assert bot.sandbox.auto_allow_bash is None

        settings = compose_settings_local(bot, fleet, paths)
        assert "sandbox" not in settings

    def test_fleet_default_cascades_to_bot(self, tmp_path):
        """Fleet default auto_allow_bash: true cascades to bot with no sandbox config."""
        fleet, paths = self._make_fleet(
            tmp_path, defaults_sandbox={"auto_allow_bash": True}
        )
        bot = fleet.bots["worker"]
        assert bot.sandbox.auto_allow_bash is True

        settings = compose_settings_local(bot, fleet, paths)
        assert settings["sandbox"]["autoAllowBashIfSandboxed"] is True

    def test_bot_override_false_wins(self, tmp_path):
        """Bot explicitly sets auto_allow_bash: false, overrides fleet default true."""
        fleet, paths = self._make_fleet(
            tmp_path,
            defaults_sandbox={"auto_allow_bash": True},
            bot_sandbox={"auto_allow_bash": False},
        )
        bot = fleet.bots["worker"]
        assert bot.sandbox.auto_allow_bash is False

        settings = compose_settings_local(bot, fleet, paths)
        # False means no autoAllowBashIfSandboxed key
        assert "sandbox" not in settings

    def test_bot_override_true_no_fleet_default(self, tmp_path):
        """Bot sets auto_allow_bash: true with no fleet default."""
        fleet, paths = self._make_fleet(
            tmp_path, bot_sandbox={"auto_allow_bash": True}
        )
        bot = fleet.bots["worker"]
        assert bot.sandbox.auto_allow_bash is True

        settings = compose_settings_local(bot, fleet, paths)
        assert settings["sandbox"]["autoAllowBashIfSandboxed"] is True

    def test_coerce_sandbox_preserves_none(self):
        """_coerce_sandbox with no auto_allow_bash key → None, not False."""
        result = _coerce_sandbox({"network_allowed_domains": ["example.com"]})
        assert result.auto_allow_bash is None

    def test_merge_sandbox_cascade(self):
        """_merge_sandbox: override None falls through to default; explicit wins."""
        default = SandboxConfig(auto_allow_bash=True)
        override_none = SandboxConfig(auto_allow_bash=None)
        override_false = SandboxConfig(auto_allow_bash=False)

        # None falls through
        merged = _merge_sandbox(default, override_none)
        assert merged.auto_allow_bash is True

        # Explicit False wins
        merged = _merge_sandbox(default, override_false)
        assert merged.auto_allow_bash is False
