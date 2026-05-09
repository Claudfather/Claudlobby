"""Tests for composer.py — scaffold_env_files merge, access.json reconcile, settings.local tools, bot.conf model strategy."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent


from claudlobby.config import (
    BotConfig,
    FleetConfig,
    TelegramConfig,
    ToolsConfig,
    load_fleet,
)
from claudlobby.composer import (
    compose_access_json,
    compose_settings_local,
    _reconcile_access_json,
    scaffold_env_files,
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
        (root / "fleet.yaml").write_text(
            dedent("""\
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
        """)
        )

        # Library dirs
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")

        (root / "library" / "mcp").mkdir(parents=True)
        (root / "library" / "mcp" / "github.json").write_text(
            json.dumps(
                {
                    "github": {"command": "gh", "args": ["mcp"]},
                    "_env_contract": {
                        "GITHUB_PAT": {"description": "GitHub PAT", "tier": "fleet"},
                    },
                }
            )
        )
        (root / "library" / "mcp" / "shopify.json").write_text(
            json.dumps(
                {
                    "shopify": {
                        "command": "npx",
                        "args": ["-y", "@ajackus/shopify-mcp-server"],
                    },
                    "_env_contract": {
                        "SHOPIFY_ACCESS_TOKEN": {
                            "description": "Shopify token",
                            "tier": "fleet",
                        },
                        "SHOPIFY_STORE_DOMAIN": {
                            "description": "Shopify domain",
                            "tier": "fleet",
                        },
                    },
                }
            )
        )

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
        env_path.write_text(
            dedent("""\
            # Fleet environment for: test-fleet
            export GITHUB_PAT="ghp_realtoken123"
        """)
        )

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
        existing = {
            "dmPolicy": "allowlist",
            "allowFrom": ["12345"],
            "groups": {},
            "pending": {},
        }
        access_path.write_text(json.dumps(existing))

        _reconcile_access_json(access_path, fresh, bot, fleet, log=lambda m: None)

        result = json.loads(access_path.read_text())
        assert result["allowFrom"].count("12345") == 1


class TestComposeSettingsLocal:
    """compose_settings_local generates permissions from sibling isolation + tool rules."""

    def _make_paths_with_runtime(self, tmp_path: Path) -> Paths:
        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "runtime" / "bots").mkdir(parents=True)
        return Paths(root=root, fleet_dir=root)

    def _make_fleet_with_bots(self, *bot_ids):
        bots = {}
        for bid in bot_ids:
            bots[bid] = BotConfig(
                bot_id=bid,
                name=bid,
                expertise=["eng"],
                telegram=TelegramConfig(handle=f"{bid}_bot"),
            )
        return FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            bots=bots,
        )

    def test_no_tools_no_siblings(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(bot_id="solo", name="solo", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"solo": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert "permissions" not in result

    def test_sibling_isolation_only(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        fleet = self._make_fleet_with_bots("bot-a", "bot-b")
        result = compose_settings_local(fleet.bots["bot-a"], fleet, paths)
        assert "permissions" in result
        deny = result["permissions"]["deny"]
        assert len(deny) == 1
        assert "Read(" in deny[0] and "bot-b" in deny[0]

    def test_tool_deny_generates_patterns(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="reviewer",
            name="reviewer",
            expertise=["code-review"],
            tools=ToolsConfig(deny=["Write", "Edit", "NotebookEdit"]),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"reviewer": bot})
        result = compose_settings_local(bot, fleet, paths)
        deny = result["permissions"]["deny"]
        assert "Write(**)" in deny
        assert "Edit(**)" in deny
        assert "NotebookEdit(**)" in deny

    def test_tool_allow_generates_patterns(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="reader",
            name="reader",
            expertise=["eng"],
            tools=ToolsConfig(allow=["Read", "Grep", "Glob"]),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"reader": bot})
        result = compose_settings_local(bot, fleet, paths)
        allow = result["permissions"]["allow"]
        assert "Read(**)" in allow
        assert "Grep(**)" in allow
        assert "Glob(**)" in allow

    def test_tool_deny_and_allow_combined(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="lead",
            name="lead",
            expertise=["orchestration"],
            tools=ToolsConfig(deny=["Write", "Edit"], allow=["Agent", "Bash"]),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"lead": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert "Write(**)" in result["permissions"]["deny"]
        assert "Agent(**)" in result["permissions"]["allow"]

    def test_tool_deny_merged_with_sibling_isolation(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        bot_a = BotConfig(
            bot_id="bot-a",
            name="bot-a",
            expertise=["code-review"],
            tools=ToolsConfig(deny=["Write"]),
            telegram=TelegramConfig(handle="a_bot"),
        )
        bot_b = BotConfig(
            bot_id="bot-b",
            name="bot-b",
            expertise=["eng"],
            telegram=TelegramConfig(handle="b_bot"),
        )
        fleet = FleetConfig(
            name="t",
            service_prefix="p",
            bots={"bot-a": bot_a, "bot-b": bot_b},
        )
        result = compose_settings_local(bot_a, fleet, paths)
        deny = result["permissions"]["deny"]
        # Should have both sibling isolation and tool deny
        assert any("Read(" in d and "bot-b" in d for d in deny)
        assert "Write(**)" in deny


class TestComposeBotConfModelStrategy:
    """compose_bot_conf emits MODEL_STRATEGY_* env vars when model_strategy is set."""

    def _compose(self, tmp_path, model_strategy=None, model=None):
        from claudlobby.composer import compose_bot_conf

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            model=model,
            model_strategy=model_strategy,
            telegram=TelegramConfig(handle="w_bot"),
        )
        fleet = FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            telegram_group_chat_id="-100999",
        )
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)
        (root / "runtime" / "bots" / "worker").mkdir(parents=True, exist_ok=True)
        (root / "lib").mkdir(exist_ok=True)
        paths = Paths(root=root, fleet_dir=root)
        return compose_bot_conf(bot, fleet, paths)

    def test_no_model_strategy_no_vars(self, tmp_path):
        conf = self._compose(tmp_path)
        assert "MODEL_STRATEGY" not in conf

    def test_full_model_strategy(self, tmp_path):
        from claudlobby.config import ModelStrategyConfig

        ms = ModelStrategyConfig(
            base="sonnet",
            escalate_to="opus",
            escalate_when=">5 files or architecture decisions",
            compact_when=">50% context",
        )
        conf = self._compose(tmp_path, model_strategy=ms)
        assert 'export MODEL_STRATEGY_BASE="sonnet"' in conf
        assert 'export MODEL_STRATEGY_ESCALATE_TO="opus"' in conf
        assert "MODEL_STRATEGY_ESCALATE_WHEN=" in conf
        assert ">5 files or architecture decisions" in conf
        assert "MODEL_STRATEGY_COMPACT_WHEN=" in conf
        assert ">50% context" in conf

    def test_partial_model_strategy(self, tmp_path):
        from claudlobby.config import ModelStrategyConfig

        ms = ModelStrategyConfig(base="sonnet", escalate_to="opus")
        conf = self._compose(tmp_path, model_strategy=ms)
        assert 'export MODEL_STRATEGY_BASE="sonnet"' in conf
        assert 'export MODEL_STRATEGY_ESCALATE_TO="opus"' in conf
        assert "MODEL_STRATEGY_ESCALATE_WHEN" not in conf
        assert "MODEL_STRATEGY_COMPACT_WHEN" not in conf

    def test_subagent_model_preferences(self, tmp_path):
        from claudlobby.config import ModelStrategyConfig

        ms = ModelStrategyConfig(
            base="sonnet",
            raw={"explore": "haiku", "plan": "sonnet", "general": "sonnet"},
        )
        conf = self._compose(tmp_path, model_strategy=ms)
        assert 'export MODEL_STRATEGY_EXPLORE="haiku"' in conf
        assert 'export MODEL_STRATEGY_PLAN="sonnet"' in conf
        assert 'export MODEL_STRATEGY_GENERAL="sonnet"' in conf

    def test_model_flag_still_in_claude_flags(self, tmp_path):
        conf = self._compose(tmp_path, model="opus")
        assert "--model opus" in conf

    def test_model_strategy_section_header(self, tmp_path):
        from claudlobby.config import ModelStrategyConfig

        ms = ModelStrategyConfig(base="sonnet")
        conf = self._compose(tmp_path, model_strategy=ms)
        assert "# Model strategy" in conf
