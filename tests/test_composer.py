"""Tests for composer.py — tools, hooks, scaffold_env_files, access.json reconcile, bot.conf model strategy, and expertise permissions."""

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
    _compose_hooks,
    _reconcile_access_json,
    compose_access_json,
    compose_settings_local,
    compose_systemd_unit,
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

        messages = []
        scaffold_env_files(fleet, paths, log=messages.append)
        second_content = (root / ".env").read_text()

        assert first_content == second_content
        assert any("up to date" in m for m in messages)


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
        assert "Read" in allow
        assert "Grep" in allow
        assert "Glob" in allow

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
        assert "Agent" in result["permissions"]["allow"]

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
        assert "export MODEL_STRATEGY_BASE=sonnet" in conf
        assert "export MODEL_STRATEGY_ESCALATE_TO=opus" in conf
        assert "MODEL_STRATEGY_ESCALATE_WHEN=" in conf
        assert ">5 files or architecture decisions" in conf
        assert "MODEL_STRATEGY_COMPACT_WHEN=" in conf
        assert ">50% context" in conf

    def test_partial_model_strategy(self, tmp_path):
        from claudlobby.config import ModelStrategyConfig

        ms = ModelStrategyConfig(base="sonnet", escalate_to="opus")
        conf = self._compose(tmp_path, model_strategy=ms)
        assert "export MODEL_STRATEGY_BASE=sonnet" in conf
        assert "export MODEL_STRATEGY_ESCALATE_TO=opus" in conf
        assert "MODEL_STRATEGY_ESCALATE_WHEN" not in conf
        assert "MODEL_STRATEGY_COMPACT_WHEN" not in conf

    def test_subagent_model_preferences(self, tmp_path):
        from claudlobby.config import ModelStrategyConfig

        ms = ModelStrategyConfig(
            base="sonnet",
            raw={"explore": "haiku", "plan": "sonnet", "general": "sonnet"},
        )
        conf = self._compose(tmp_path, model_strategy=ms)
        assert "export MODEL_STRATEGY_EXPLORE=haiku" in conf
        assert "export MODEL_STRATEGY_PLAN=sonnet" in conf
        assert "export MODEL_STRATEGY_GENERAL=sonnet" in conf

    def test_model_flag_still_in_claude_flags(self, tmp_path):
        conf = self._compose(tmp_path, model="opus")
        assert "--model opus" in conf

    def test_model_strategy_section_header(self, tmp_path):
        from claudlobby.config import ModelStrategyConfig

        ms = ModelStrategyConfig(base="sonnet")
        conf = self._compose(tmp_path, model_strategy=ms)
        assert "# Model strategy" in conf


class TestComposeBotConfExportedVars:
    """bot.conf must export BOT_ID so hook subprocesses (bot-vitals.sh) can read it."""

    def test_bot_id_is_exported(self, tmp_path):
        from claudlobby.composer import compose_bot_conf

        bot = BotConfig(
            bot_id="astrid",
            name="astrid",
            expertise=["eng"],
            telegram=TelegramConfig(handle="w_bot"),
        )
        fleet = FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            telegram_group_chat_id="-100999",
        )
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)
        (root / "runtime" / "bots" / "astrid").mkdir(parents=True, exist_ok=True)
        (root / "lib").mkdir(exist_ok=True)
        paths = Paths(root=root, fleet_dir=root)
        conf = compose_bot_conf(bot, fleet, paths)
        assert "export BOT_ID=astrid" in conf


class TestComposeBotConfServicePrefix:
    """compose_bot_conf derives BOT_SERVICE and SERVICE_PREFIX from fleet config."""

    def test_service_prefix_from_fleet_config(self, tmp_path):
        from claudlobby.composer import compose_bot_conf

        bot = BotConfig(
            bot_id="eng-1",
            name="eng-1",
            expertise=["eng"],
            telegram=TelegramConfig(handle="eng_bot"),
        )
        fleet = FleetConfig(
            name="my-fleet",
            service_prefix="com.myorg.prod",
            telegram_group_chat_id="-100999",
        )
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)
        (root / "runtime" / "bots" / "eng-1").mkdir(parents=True, exist_ok=True)
        (root / "lib").mkdir(exist_ok=True)
        paths = Paths(root=root, fleet_dir=root)
        conf = compose_bot_conf(bot, fleet, paths)
        assert "BOT_SERVICE=com.myorg.prod.eng-1" in conf
        assert "export SERVICE_PREFIX=com.myorg.prod" in conf

    def test_no_hardcoded_example_prefix(self, tmp_path):
        from claudlobby.composer import compose_bot_conf

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle="w_bot"),
        )
        fleet = FleetConfig(
            name="custom",
            service_prefix="io.custom.fleet",
            telegram_group_chat_id="-100999",
        )
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)
        (root / "runtime" / "bots" / "worker").mkdir(parents=True, exist_ok=True)
        (root / "lib").mkdir(exist_ok=True)
        paths = Paths(root=root, fleet_dir=root)
        conf = compose_bot_conf(bot, fleet, paths)
        assert "com.example" not in conf
        assert "com.claudlobby" not in conf
        assert "io.custom.fleet" in conf


class TestComposeHooks:
    """_compose_hooks transforms flat fleet.yaml entries into Claude Code format."""

    def test_empty_hooks(self):
        assert _compose_hooks({}) == {}

    def test_single_command_hook_no_matcher(self):
        hooks = {
            "PreToolUse": [
                {"command": "/usr/local/bin/log.sh"},
            ],
        }
        result = _compose_hooks(hooks)
        assert "PreToolUse" in result
        groups = result["PreToolUse"]
        assert len(groups) == 1
        # No matcher key when matcher is empty
        assert "matcher" not in groups[0]
        assert groups[0]["hooks"] == [
            {"type": "command", "command": "/usr/local/bin/log.sh"}
        ]

    def test_hook_with_matcher(self):
        hooks = {
            "PostToolUse": [
                {"command": "notify.sh", "matcher": "Bash"},
            ],
        }
        result = _compose_hooks(hooks)
        groups = result["PostToolUse"]
        assert len(groups) == 1
        assert groups[0]["matcher"] == "Bash"
        assert groups[0]["hooks"][0]["command"] == "notify.sh"

    def test_groups_by_matcher(self):
        hooks = {
            "PreToolUse": [
                {"command": "log.sh", "matcher": "Bash"},
                {"command": "validate.sh", "matcher": "Bash"},
                {"command": "other.sh", "matcher": "Write|Edit"},
            ],
        }
        result = _compose_hooks(hooks)
        groups = result["PreToolUse"]
        assert len(groups) == 2
        # First group: Bash with 2 hooks
        bash_group = [g for g in groups if g.get("matcher") == "Bash"][0]
        assert len(bash_group["hooks"]) == 2
        # Second group: Write|Edit with 1 hook
        write_group = [g for g in groups if g.get("matcher") == "Write|Edit"][0]
        assert len(write_group["hooks"]) == 1

    def test_defaults_type_to_command(self):
        hooks = {"PreToolUse": [{"command": "script.sh"}]}
        result = _compose_hooks(hooks)
        assert result["PreToolUse"][0]["hooks"][0]["type"] == "command"

    def test_preserves_explicit_type(self):
        hooks = {"PreToolUse": [{"type": "prompt", "prompt": "Is this safe?"}]}
        result = _compose_hooks(hooks)
        hook = result["PreToolUse"][0]["hooks"][0]
        assert hook["type"] == "prompt"
        assert hook["prompt"] == "Is this safe?"

    def test_preserves_extra_fields(self):
        hooks = {
            "PostToolUse": [
                {"command": "log.sh", "timeout": 10, "async": True, "matcher": "Bash"},
            ],
        }
        result = _compose_hooks(hooks)
        hook = result["PostToolUse"][0]["hooks"][0]
        assert hook["timeout"] == 10
        assert hook["async"] is True
        assert "matcher" not in hook  # matcher stays on the group, not the hook

    def test_skips_empty_event_lists(self):
        hooks = {"PreToolUse": [], "PostToolUse": [{"command": "log.sh"}]}
        result = _compose_hooks(hooks)
        assert "PreToolUse" not in result
        assert "PostToolUse" in result


class TestHooksMergeAndSettings:
    """Hooks merge from fleet defaults + bot overrides into settings.local.json."""

    def test_hooks_in_fleet_yaml(self, tmp_path):
        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
        (root / "runtime" / "bots" / "worker").mkdir(parents=True)

        (root / "fleet.yaml").write_text(
            dedent("""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              defaults:
                hooks:
                  PreToolUse:
                    - command: "log-pre.sh"
                  PostToolUse:
                    - command: "log-post.sh"
              bots:
                worker:
                  expertise: [eng]
                  hooks:
                    PostToolUse:
                      - command: "notify.sh"
                        matcher: "Bash"
        """)
        )

        fleet = load_fleet(root / "fleet.yaml")
        bot = fleet.bots["worker"]

        # Verify merge: default PreToolUse + merged PostToolUse
        assert "PreToolUse" in bot.hooks
        assert len(bot.hooks["PreToolUse"]) == 1
        assert bot.hooks["PreToolUse"][0]["command"] == "log-pre.sh"

        assert "PostToolUse" in bot.hooks
        assert len(bot.hooks["PostToolUse"]) == 2  # default + bot override
        assert bot.hooks["PostToolUse"][0]["command"] == "log-post.sh"
        assert bot.hooks["PostToolUse"][1]["command"] == "notify.sh"

    def test_settings_local_includes_hooks(self, tmp_path):
        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "runtime" / "bots" / "worker").mkdir(parents=True)
        paths = _make_paths(root)

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            hooks={
                "PreToolUse": [{"command": "log.sh"}],
                "PostToolUse": [{"command": "check.sh", "matcher": "Bash"}],
            },
        )
        fleet = FleetConfig(name="test", service_prefix="com.test")

        settings = compose_settings_local(bot, fleet, paths)
        assert "hooks" in settings
        assert "PreToolUse" in settings["hooks"]
        assert "PostToolUse" in settings["hooks"]

        # Verify Claude Code format: matcher groups with nested hooks
        pre_groups = settings["hooks"]["PreToolUse"]
        assert len(pre_groups) == 1
        assert pre_groups[0]["hooks"][0]["type"] == "command"
        assert pre_groups[0]["hooks"][0]["command"] == "log.sh"

        post_groups = settings["hooks"]["PostToolUse"]
        assert post_groups[0]["matcher"] == "Bash"

    def test_no_hooks_omits_key(self, tmp_path):
        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "runtime" / "bots" / "worker").mkdir(parents=True)
        paths = _make_paths(root)

        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        fleet = FleetConfig(name="test", service_prefix="com.test")

        settings = compose_settings_local(bot, fleet, paths)
        assert "hooks" not in settings

    def test_bot_only_hooks_no_defaults(self, tmp_path):
        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
        (root / "runtime" / "bots" / "worker").mkdir(parents=True)

        (root / "fleet.yaml").write_text(
            dedent("""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                worker:
                  expertise: [eng]
                  hooks:
                    PreToolUse:
                      - command: "my-hook.sh"
                        matcher: "Write|Edit"
        """)
        )

        fleet = load_fleet(root / "fleet.yaml")
        bot = fleet.bots["worker"]
        assert len(bot.hooks["PreToolUse"]) == 1
        assert bot.hooks["PreToolUse"][0]["matcher"] == "Write|Edit"

    def test_defaults_only_hooks_no_bot_override(self, tmp_path):
        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
        (root / "runtime" / "bots" / "worker").mkdir(parents=True)

        (root / "fleet.yaml").write_text(
            dedent("""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              defaults:
                hooks:
                  PostToolUse:
                    - command: "fleet-log.sh"
              bots:
                worker:
                  expertise: [eng]
        """)
        )

        fleet = load_fleet(root / "fleet.yaml")
        bot = fleet.bots["worker"]
        assert len(bot.hooks["PostToolUse"]) == 1
        assert bot.hooks["PostToolUse"][0]["command"] == "fleet-log.sh"


class TestResolveChannelPermissions:
    """_resolve_channel_permissions auto-derives Telegram plugin tools."""

    def test_returns_telegram_tools_when_handle_set(self):
        from claudlobby.composer import _resolve_channel_permissions

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle="my_bot"),
        )
        result = _resolve_channel_permissions(bot)
        assert "mcp__plugin_telegram_telegram__reply" in result
        assert "mcp__plugin_telegram_telegram__edit_message" in result
        assert "mcp__plugin_telegram_telegram__react" in result
        assert "mcp__plugin_telegram_telegram__download_attachment" in result
        assert len(result) == 4

    def test_returns_empty_when_no_handle(self):
        from claudlobby.composer import _resolve_channel_permissions

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle=None),
        )
        result = _resolve_channel_permissions(bot)
        assert result == []

    def test_returns_empty_when_handle_is_empty_string(self):
        from claudlobby.composer import _resolve_channel_permissions

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle=""),
        )
        result = _resolve_channel_permissions(bot)
        assert result == []


class TestResolveSkillPermissions:
    """_resolve_skill_permissions generates Skill() patterns from bot.skills."""

    def test_generates_both_patterns_per_skill(self):
        from claudlobby.composer import _resolve_skill_permissions

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            skills=["lifecycle", "prs"],
        )
        result = _resolve_skill_permissions(bot)
        assert "Skill(lifecycle)" in result
        assert "Skill(lifecycle:*)" in result
        assert "Skill(prs)" in result
        assert "Skill(prs:*)" in result
        assert len(result) == 4

    def test_empty_skills_returns_empty(self):
        from claudlobby.composer import _resolve_skill_permissions

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            skills=[],
        )
        result = _resolve_skill_permissions(bot)
        assert result == []

    def test_single_skill(self):
        from claudlobby.composer import _resolve_skill_permissions

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            skills=["commit"],
        )
        result = _resolve_skill_permissions(bot)
        assert result == ["Skill(commit)", "Skill(commit:*)"]


class TestChannelSkillInSettingsLocal:
    """compose_settings_local includes channel + skill permissions in allow list."""

    def _make_paths_with_runtime(self, tmp_path: Path) -> Paths:
        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "runtime" / "bots").mkdir(parents=True)
        return Paths(root=root, fleet_dir=root)

    def test_telegram_tools_in_allow(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle="my_bot"),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths)
        allow = result["permissions"]["allow"]
        assert "mcp__plugin_telegram_telegram__reply" in allow
        assert "mcp__plugin_telegram_telegram__edit_message" in allow

    def test_skill_patterns_in_allow(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            skills=["lifecycle", "prs"],
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths)
        allow = result["permissions"]["allow"]
        assert "Skill(lifecycle)" in allow
        assert "Skill(lifecycle:*)" in allow
        assert "Skill(prs)" in allow
        assert "Skill(prs:*)" in allow

    def test_channel_and_skill_combined(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            skills=["commit"],
            telegram=TelegramConfig(handle="my_bot"),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths)
        allow = result["permissions"]["allow"]
        # Channel tools present
        assert "mcp__plugin_telegram_telegram__reply" in allow
        # Skill patterns present
        assert "Skill(commit)" in allow
        assert "Skill(commit:*)" in allow

    def test_explicit_deny_still_wins(self, tmp_path):
        """Explicit tools.deny should appear in deny regardless of auto-derived allow."""
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            skills=["commit"],
            telegram=TelegramConfig(handle="my_bot"),
            tools=ToolsConfig(deny=["Write"]),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert "Write(**)" in result["permissions"]["deny"]
        # Auto-derived still in allow
        assert "mcp__plugin_telegram_telegram__reply" in result["permissions"]["allow"]

    def test_no_telegram_no_skills_no_permissions(self, tmp_path):
        """Bot with no telegram, no skills, no explicit tools → no permissions block."""
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(bot_id="solo", name="solo", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"solo": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert "permissions" not in result

    def test_explicit_allow_merges_with_auto_derived(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            skills=["commit"],
            telegram=TelegramConfig(handle="my_bot"),
            tools=ToolsConfig(allow=["Bash", "Agent"]),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths)
        allow = result["permissions"]["allow"]
        # Auto-derived
        assert "mcp__plugin_telegram_telegram__reply" in allow
        assert "Skill(commit)" in allow
        # Explicit (plain tool names, no glob suffix)
        assert "Bash" in allow
        assert "Agent" in allow
        # Base tools prepended
        assert "Read" in allow
        assert "Grep" in allow
        assert "Glob" in allow


class TestResolveMcpPermissions:
    """_resolve_mcp_permissions emits server-level wildcards from fragment contracts."""

    def _setup_mcp_library(self, tmp_path: Path, fragments: dict[str, dict]) -> Paths:
        """Create a minimal library with MCP fragments."""
        root = tmp_path / "claudlobby"
        root.mkdir()
        mcp_dir = root / "library" / "mcp"
        mcp_dir.mkdir(parents=True)
        (root / "runtime" / "bots").mkdir(parents=True)
        for name, content in fragments.items():
            (mcp_dir / f"{name}.json").write_text(json.dumps(content))
        return Paths(root=root, fleet_dir=root)

    def test_github_and_notion_emit_wildcards(self, tmp_path):
        """When all tools are allowed, emit mcp__<server>__* wildcards instead of per-tool entries."""
        from claudlobby.config import McpEntry
        from claudlobby.composer import _resolve_mcp_permissions

        paths = self._setup_mcp_library(
            tmp_path,
            {
                "github": {
                    "_permissions_contract": {
                        "tools": ["search_code", "get_issue", "create_pull_request"]
                    },
                    "github": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github"],
                    },
                },
                "notion": {
                    "_permissions_contract": {
                        "tools": ["API-post-page", "API-get-block-children"]
                    },
                    "notion": {
                        "command": "npx",
                        "args": ["-y", "@notionhq/notion-mcp-server"],
                    },
                },
            },
        )
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            mcp=[McpEntry(name="github"), McpEntry(name="notion")],
        )
        result = _resolve_mcp_permissions(bot, paths)
        # Wildcards emitted instead of per-tool entries
        assert "mcp__github__*" in result
        assert "mcp__notion__*" in result
        # No individual tool entries
        assert "mcp__github__search_code" not in result
        assert "mcp__notion__API-post-page" not in result
        assert len(result) == 2

    def test_multi_instance_gws_wildcards(self, tmp_path):
        """Multi-instance servers each get their own wildcard entry."""
        from claudlobby.config import McpEntry
        from claudlobby.composer import _resolve_mcp_permissions

        paths = self._setup_mcp_library(
            tmp_path,
            {
                "gws": {
                    "_permissions_contract": {
                        "tools": ["search_gmail_messages", "get_events"]
                    },
                    "gws": {"command": "uvx", "args": ["workspace-mcp"]},
                },
            },
        )
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            mcp=[McpEntry(name="gws", instances=["personal", "work"])],
        )
        result = _resolve_mcp_permissions(bot, paths)
        assert "mcp__gws-personal__*" in result
        assert "mcp__gws-work__*" in result
        # No individual tool entries
        assert "mcp__gws-personal__search_gmail_messages" not in result
        assert "mcp__gws-work__get_events" not in result
        assert len(result) == 2

    def test_fragment_without_contract_skipped(self, tmp_path):
        from claudlobby.config import McpEntry
        from claudlobby.composer import _resolve_mcp_permissions

        paths = self._setup_mcp_library(
            tmp_path,
            {
                "custom": {
                    "custom": {"command": "npx", "args": ["-y", "custom-mcp"]},
                },
            },
        )
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            mcp=[McpEntry(name="custom")],
        )
        result = _resolve_mcp_permissions(bot, paths)
        assert result == []

    def test_empty_tools_list_skipped(self, tmp_path):
        from claudlobby.config import McpEntry
        from claudlobby.composer import _resolve_mcp_permissions

        paths = self._setup_mcp_library(
            tmp_path,
            {
                "shopify": {
                    "_permissions_contract": {"tools": []},
                    "shopify": {"command": "npx", "args": ["-y", "shopify-mcp"]},
                },
            },
        )
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            mcp=[McpEntry(name="shopify")],
        )
        result = _resolve_mcp_permissions(bot, paths)
        assert result == []

    def test_missing_fragment_skipped(self, tmp_path):
        from claudlobby.config import McpEntry
        from claudlobby.composer import _resolve_mcp_permissions

        paths = self._setup_mcp_library(tmp_path, {})
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            mcp=[McpEntry(name="nonexistent")],
        )
        result = _resolve_mcp_permissions(bot, paths)
        assert result == []


class TestMcpPermissionsInSettingsLocal:
    """compose_settings_local includes MCP permissions in allow list."""

    def _setup_mcp_library(self, tmp_path: Path, fragments: dict[str, dict]) -> Paths:
        root = tmp_path / "claudlobby"
        root.mkdir()
        mcp_dir = root / "library" / "mcp"
        mcp_dir.mkdir(parents=True)
        (root / "runtime" / "bots").mkdir(parents=True)
        for name, content in fragments.items():
            (mcp_dir / f"{name}.json").write_text(json.dumps(content))
        return Paths(root=root, fleet_dir=root)

    def test_mcp_permissions_in_allow_list(self, tmp_path):
        """Wildcard is emitted for github MCP in settings.local allow list."""
        from claudlobby.config import McpEntry

        paths = self._setup_mcp_library(
            tmp_path,
            {
                "github": {
                    "_permissions_contract": {
                        "tools": ["search_code", "create_pull_request"]
                    },
                    "github": {"command": "npx", "args": ["-y", "gh-mcp"]},
                },
            },
        )
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            mcp=[McpEntry(name="github")],
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert "permissions" in result
        allow = result["permissions"]["allow"]
        assert "mcp__github__*" in allow
        # Individual tool entries should not appear
        assert "mcp__github__search_code" not in allow
        assert "mcp__github__create_pull_request" not in allow

    def test_mcp_emitted_even_without_explicit_tools_allow(self, tmp_path):
        """MCP permissions should appear even if bot.tools.allow is empty."""
        from claudlobby.config import McpEntry

        paths = self._setup_mcp_library(
            tmp_path,
            {
                "notion": {
                    "_permissions_contract": {"tools": ["API-post-page"]},
                    "notion": {"command": "npx", "args": ["-y", "notion-mcp"]},
                },
            },
        )
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            mcp=[McpEntry(name="notion")],
            tools=ToolsConfig(allow=[], deny=[]),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert "permissions" in result
        assert "mcp__notion__*" in result["permissions"]["allow"]

    def test_explicit_deny_alongside_mcp_permissions(self, tmp_path):
        from claudlobby.config import McpEntry

        paths = self._setup_mcp_library(
            tmp_path,
            {
                "github": {
                    "_permissions_contract": {"tools": ["search_code"]},
                    "github": {"command": "npx", "args": ["-y", "gh-mcp"]},
                },
            },
        )
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            mcp=[McpEntry(name="github")],
            tools=ToolsConfig(deny=["Write", "Edit"]),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths)
        deny = result["permissions"]["deny"]
        allow = result["permissions"]["allow"]
        assert "Write(**)" in deny
        assert "Edit(**)" in deny
        assert "mcp__github__*" in allow

    def test_multi_instance_in_settings_local(self, tmp_path):
        from claudlobby.config import McpEntry

        paths = self._setup_mcp_library(
            tmp_path,
            {
                "gws": {
                    "_permissions_contract": {
                        "tools": ["get_events", "search_gmail_messages"]
                    },
                    "gws": {"command": "uvx", "args": ["workspace-mcp"]},
                },
            },
        )
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            mcp=[McpEntry(name="gws", instances=["personal", "work"])],
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths)
        allow = result["permissions"]["allow"]
        assert "mcp__gws-personal__*" in allow
        assert "mcp__gws-work__*" in allow
        # Individual tool entries should not appear
        assert "mcp__gws-personal__get_events" not in allow
        assert "mcp__gws-work__search_gmail_messages" not in allow


class TestParseExpertisePermissions:
    """parse_expertise_file extracts permissions from YAML frontmatter."""

    def test_extracts_allow_all(self, tmp_path):
        from claudlobby.loader import parse_expertise_file

        f = tmp_path / "eng.md"
        f.write_text(
            "---\npermissions:\n  allow_all: true\n  bash_allow: [git, npm]\n---\n\n# Bot — Engineer\n\nBuild.\n"
        )
        item = parse_expertise_file(f)
        assert item is not None
        assert item.permissions is not None
        assert item.permissions.allow_all is True
        assert item.permissions.bash_allow == ["git", "npm"]
        assert item.permissions.allow == []
        assert item.permissions.deny == []

    def test_extracts_deny_list(self, tmp_path):
        from claudlobby.loader import parse_expertise_file

        f = tmp_path / "reviewer.md"
        f.write_text(
            "---\npermissions:\n  deny: [Write, Edit, NotebookEdit]\n  bash_allow: [git, gh]\n---\n\n# Bot — Reviewer\n\nReview.\n"
        )
        item = parse_expertise_file(f)
        assert item.permissions is not None
        assert item.permissions.deny == ["Write", "Edit", "NotebookEdit"]
        assert item.permissions.bash_allow == ["git", "gh"]
        assert item.permissions.allow_all is False

    def test_extracts_explicit_allow_list(self, tmp_path):
        from claudlobby.loader import parse_expertise_file

        f = tmp_path / "ops.md"
        f.write_text(
            "---\npermissions:\n  allow: [Read, Grep, Glob, Bash]\n  bash_allow: [git, curl]\n---\n\n# Ops\n\nWork.\n"
        )
        item = parse_expertise_file(f)
        assert item.permissions is not None
        assert item.permissions.allow == ["Read", "Grep", "Glob", "Bash"]

    def test_no_frontmatter_gives_none_permissions(self, tmp_path):
        from claudlobby.loader import parse_expertise_file

        f = tmp_path / "plain.md"
        f.write_text("# Bot — Plain\n\nNo frontmatter.\n")
        item = parse_expertise_file(f)
        assert item is not None
        assert item.permissions is None

    def test_frontmatter_without_permissions_gives_none(self, tmp_path):
        from claudlobby.loader import parse_expertise_file

        f = tmp_path / "other.md"
        f.write_text("---\ntitle_label: Other\n---\n\nBody.\n")
        item = parse_expertise_file(f)
        assert item is not None
        assert item.permissions is None

    def test_title_label_still_extracted_with_permissions(self, tmp_path):
        from claudlobby.loader import parse_expertise_file

        f = tmp_path / "eng.md"
        f.write_text(
            "---\npermissions:\n  allow_all: true\n---\n\n# Bot — Engineer\n\nBuild.\n"
        )
        item = parse_expertise_file(f)
        assert item.title_label == "Engineer"
        assert item.permissions is not None
        assert item.permissions.allow_all is True


class TestResolveExpertisePermissions:
    """_resolve_expertise_permissions merges profiles from all expertise files."""

    def _setup_expertise(self, tmp_path, files: dict[str, str]) -> Paths:
        root = tmp_path / "claudlobby"
        root.mkdir()
        exp_dir = root / "library" / "expertise"
        exp_dir.mkdir(parents=True)
        (root / "runtime" / "bots").mkdir(parents=True)
        for name, content in files.items():
            (exp_dir / f"{name}.md").write_text(content)
        return Paths(root=root, fleet_dir=root)

    def test_allow_all_expands_to_full_tool_set(self, tmp_path):
        from claudlobby.composer import _resolve_expertise_permissions

        paths = self._setup_expertise(
            tmp_path,
            {
                "eng": "---\npermissions:\n  allow_all: true\n  bash_allow: [git]\n---\n\n# Engineer\n\nBuild.\n",
            },
        )
        bot = BotConfig(bot_id="w", name="w", expertise=["eng"])
        allow, deny = _resolve_expertise_permissions(bot, paths)
        # All 10 core tools should be in allow
        for tool in [
            "Read",
            "Write",
            "Edit",
            "Bash",
            "Agent",
            "Grep",
            "Glob",
            "WebFetch",
            "WebSearch",
            "NotebookEdit",
        ]:
            assert tool in allow, f"{tool} missing from allow"
        assert "Bash(git *)" in allow
        assert deny == []

    def test_deny_wins_over_allow(self, tmp_path):
        from claudlobby.composer import _resolve_expertise_permissions

        paths = self._setup_expertise(
            tmp_path,
            {
                "eng": "---\npermissions:\n  allow_all: true\n---\n\n# Engineer\n",
                "reviewer": "---\npermissions:\n  deny: [Write, Edit]\n---\n\n# Reviewer\n",
            },
        )
        bot = BotConfig(bot_id="w", name="w", expertise=["eng", "reviewer"])
        allow, deny = _resolve_expertise_permissions(bot, paths)
        # Write and Edit should be in deny, not in allow
        assert "Write(**)" in deny
        assert "Edit(**)" in deny
        assert "Write" not in allow
        assert "Edit" not in allow
        # Other tools still allowed
        assert "Read" in allow
        assert "Bash" in allow

    def test_bash_allow_merged_across_expertise(self, tmp_path):
        from claudlobby.composer import _resolve_expertise_permissions

        paths = self._setup_expertise(
            tmp_path,
            {
                "eng": "---\npermissions:\n  allow_all: true\n  bash_allow: [git, npm]\n---\n\n# Eng\n",
                "ops": "---\npermissions:\n  allow: [Bash]\n  bash_allow: [git, curl, jq]\n---\n\n# Ops\n",
            },
        )
        bot = BotConfig(bot_id="w", name="w", expertise=["eng", "ops"])
        allow, deny = _resolve_expertise_permissions(bot, paths)
        assert "Bash(git *)" in allow
        assert "Bash(npm *)" in allow
        assert "Bash(curl *)" in allow
        assert "Bash(jq *)" in allow

    def test_missing_expertise_file_skipped(self, tmp_path):
        from claudlobby.composer import _resolve_expertise_permissions

        paths = self._setup_expertise(
            tmp_path,
            {
                "eng": "---\npermissions:\n  allow_all: true\n---\n\n# Eng\n",
            },
        )
        bot = BotConfig(bot_id="w", name="w", expertise=["eng", "nonexistent"])
        allow, deny = _resolve_expertise_permissions(bot, paths)
        # Should still work with just the eng expertise
        assert "Read" in allow
        assert "Write" in allow

    def test_no_permissions_in_expertise_skipped(self, tmp_path):
        from claudlobby.composer import _resolve_expertise_permissions

        paths = self._setup_expertise(
            tmp_path,
            {
                "eng": "# Engineer\n\nNo frontmatter.\n",
            },
        )
        bot = BotConfig(bot_id="w", name="w", expertise=["eng"])
        allow, deny = _resolve_expertise_permissions(bot, paths)
        assert allow == []
        assert deny == []

    def test_explicit_allow_without_allow_all(self, tmp_path):
        from claudlobby.composer import _resolve_expertise_permissions

        paths = self._setup_expertise(
            tmp_path,
            {
                "ops": "---\npermissions:\n  allow: [Read, Grep, Glob, Bash, WebFetch, WebSearch]\n  bash_allow: [git, curl]\n---\n\n# Ops\n",
            },
        )
        bot = BotConfig(bot_id="w", name="w", expertise=["ops"])
        allow, deny = _resolve_expertise_permissions(bot, paths)
        assert "Read" in allow
        assert "Bash" in allow
        assert "WebFetch" in allow
        # Write/Edit not in allow (not allow_all, not in explicit list)
        assert "Write" not in allow
        assert "Edit" not in allow
        assert "Bash(git *)" in allow
        assert "Bash(curl *)" in allow


class TestExpertisePermissionsInSettingsLocal:
    """compose_settings_local integrates expertise permissions into the layered output."""

    def _setup_expertise(self, tmp_path, files: dict[str, str]) -> Paths:
        root = tmp_path / "claudlobby"
        root.mkdir()
        exp_dir = root / "library" / "expertise"
        exp_dir.mkdir(parents=True)
        (root / "runtime" / "bots").mkdir(parents=True)
        for name, content in files.items():
            (exp_dir / f"{name}.md").write_text(content)
        return Paths(root=root, fleet_dir=root)

    def test_expertise_allow_in_settings(self, tmp_path):
        paths = self._setup_expertise(
            tmp_path,
            {
                "eng": "---\npermissions:\n  allow_all: true\n  bash_allow: [git, npm]\n---\n\n# Eng\n",
            },
        )
        bot = BotConfig(bot_id="w", name="w", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
        result = compose_settings_local(bot, fleet, paths)
        allow = result["permissions"]["allow"]
        assert "Write" in allow
        assert "Bash" in allow
        assert "Bash(git *)" in allow
        assert "Bash(npm *)" in allow

    def test_expertise_deny_in_settings(self, tmp_path):
        paths = self._setup_expertise(
            tmp_path,
            {
                "reviewer": "---\npermissions:\n  deny: [Write, Edit]\n  bash_allow: [git, gh]\n---\n\n# Reviewer\n",
            },
        )
        bot = BotConfig(bot_id="r", name="r", expertise=["reviewer"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"r": bot})
        result = compose_settings_local(bot, fleet, paths)
        deny = result["permissions"]["deny"]
        allow = result["permissions"]["allow"]
        assert "Write(**)" in deny
        assert "Edit(**)" in deny
        assert "Bash(git *)" in allow
        assert "Bash(gh *)" in allow

    def test_bot_deny_wins_over_expertise_allow(self, tmp_path):
        """Bot-level deny (fleet.yaml) should override expertise allow_all."""
        paths = self._setup_expertise(
            tmp_path,
            {
                "eng": "---\npermissions:\n  allow_all: true\n---\n\n# Eng\n",
            },
        )
        bot = BotConfig(
            bot_id="w",
            name="w",
            expertise=["eng"],
            tools=ToolsConfig(deny=["Write", "Edit"]),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
        result = compose_settings_local(bot, fleet, paths)
        deny = result["permissions"]["deny"]
        allow = result["permissions"]["allow"]
        assert "Write(**)" in deny
        assert "Edit(**)" in deny
        # Bot deny removes from allow
        assert "Write" not in allow
        assert "Edit" not in allow
        # Other tools still present
        assert "Bash" in allow
        assert "Read" in allow

    def test_expertise_plus_mcp_plus_skills_combined(self, tmp_path):
        """Full integration: expertise + MCP contracts + skills + telegram all compose."""
        from claudlobby.config import McpEntry

        root = tmp_path / "claudlobby"
        root.mkdir()
        exp_dir = root / "library" / "expertise"
        exp_dir.mkdir(parents=True)
        mcp_dir = root / "library" / "mcp"
        mcp_dir.mkdir(parents=True)
        (root / "runtime" / "bots").mkdir(parents=True)

        (exp_dir / "eng.md").write_text(
            "---\npermissions:\n  allow_all: true\n  bash_allow: [git]\n---\n\n# Eng\n"
        )
        (mcp_dir / "github.json").write_text(
            json.dumps(
                {
                    "_permissions_contract": {
                        "tools": ["search_code", "create_pull_request"]
                    },
                    "github": {"command": "npx", "args": ["-y", "gh-mcp"]},
                }
            )
        )

        paths = Paths(root=root, fleet_dir=root)
        bot = BotConfig(
            bot_id="w",
            name="w",
            expertise=["eng"],
            mcp=[McpEntry(name="github")],
            skills=["commit"],
            telegram=TelegramConfig(handle="my_bot"),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
        result = compose_settings_local(bot, fleet, paths)
        allow = result["permissions"]["allow"]

        # Layer 2: Expertise
        assert "Write" in allow
        assert "Bash(git *)" in allow
        # Layer 3: MCP — wildcard instead of individual tool entries
        assert "mcp__github__*" in allow
        assert "mcp__github__search_code" not in allow
        assert "mcp__github__create_pull_request" not in allow
        # Layer 4: Channel
        assert "mcp__plugin_telegram_telegram__reply" in allow
        # Layer 5: Skills
        assert "Skill(commit)" in allow
        assert "Skill(commit:*)" in allow
        # Base tools guaranteed
        assert "Read" in allow
        assert "Grep" in allow
        assert "Glob" in allow

    def test_code_review_profile_in_settings_local(self, tmp_path):
        """Code reviewer gets Agent/WebFetch/WebSearch in allow, Write/Edit/NotebookEdit in deny."""
        paths = self._setup_expertise(
            tmp_path,
            {
                "code-review": (
                    "---\npermissions:\n"
                    "  allow: [Agent, WebFetch, WebSearch]\n"
                    "  deny: [Write, Edit, NotebookEdit]\n"
                    "  bash_allow: [git, gh, grep, find, cat, head, tail, diff]\n"
                    "---\n\n# Reviewer\n"
                ),
            },
        )
        bot = BotConfig(bot_id="r", name="r", expertise=["code-review"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"r": bot})
        result = compose_settings_local(bot, fleet, paths)
        allow = result["permissions"]["allow"]
        deny = result["permissions"]["deny"]
        # Reviewer needs subagents and research tools
        assert "Agent" in allow
        assert "WebFetch" in allow
        assert "WebSearch" in allow
        # Reviewer must not write code
        assert "Write(**)" in deny
        assert "Edit(**)" in deny
        assert "NotebookEdit(**)" in deny
        assert "Write" not in allow
        assert "Edit" not in allow
        assert "NotebookEdit" not in allow
        # Bash commands scoped
        assert "Bash(git *)" in allow
        assert "Bash(gh *)" in allow


class TestComposeSystemdUnit:
    """Boot stagger injection in systemd units."""

    def _make(self, tmp_path):
        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        (root / "runtime" / "bots" / "w").mkdir(parents=True)
        bot = BotConfig(bot_id="w", name="w", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
        return bot, fleet, paths

    def test_no_stagger_when_delay_zero(self, tmp_path):
        bot, fleet, paths = self._make(tmp_path)
        unit = compose_systemd_unit(bot, fleet, paths, boot_delay_s=0)
        assert "ExecStartPre" not in unit

    def test_stagger_injected_when_delay_positive(self, tmp_path):
        bot, fleet, paths = self._make(tmp_path)
        unit = compose_systemd_unit(bot, fleet, paths, boot_delay_s=3)
        assert "ExecStartPre=/bin/sleep 3" in unit

    def test_stagger_value_varies(self, tmp_path):
        bot, fleet, paths = self._make(tmp_path)
        unit6 = compose_systemd_unit(bot, fleet, paths, boot_delay_s=6)
        assert "ExecStartPre=/bin/sleep 6" in unit6
        unit0 = compose_systemd_unit(bot, fleet, paths, boot_delay_s=0)
        assert "ExecStartPre" not in unit0


class TestPluginsBotConf:
    """compose_bot_conf emits CLAUDE_CODE_SYNC_PLUGIN_INSTALL when fleet has plugins."""

    def _compose(self, tmp_path, plugins=None):
        from claudlobby.config import PluginsConfig
        from claudlobby.composer import compose_bot_conf

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle="w_bot"),
        )
        fleet = FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            telegram_group_chat_id="-100999",
            plugins=plugins or PluginsConfig(),
        )
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)
        (root / "runtime" / "bots" / "worker").mkdir(parents=True, exist_ok=True)
        (root / "lib").mkdir(exist_ok=True)
        paths = Paths(root=root, fleet_dir=root)
        return compose_bot_conf(bot, fleet, paths)

    def test_bot_conf_has_sync_env_var(self, tmp_path):
        from claudlobby.config import PluginsConfig

        plugins = PluginsConfig(required=["claudna@Claudfather"])
        conf = self._compose(tmp_path, plugins=plugins)
        assert 'export CLAUDE_CODE_SYNC_PLUGIN_INSTALL="1"' in conf

    def test_bot_conf_no_sync_without_plugins(self, tmp_path):
        conf = self._compose(tmp_path)
        assert "CLAUDE_CODE_SYNC_PLUGIN_INSTALL" not in conf

    def test_bot_conf_emits_fleet_plugins_required(self, tmp_path):
        from claudlobby.config import PluginsConfig

        plugins = PluginsConfig(
            required=["claudna@Claudfather", "telegram@claude-plugins-official"]
        )
        conf = self._compose(tmp_path, plugins=plugins)
        assert (
            "export FLEET_PLUGINS_REQUIRED='claudna@Claudfather telegram@claude-plugins-official'"
            in conf
        )

    def test_bot_conf_no_fleet_plugins_required_without_plugins(self, tmp_path):
        conf = self._compose(tmp_path)
        assert "FLEET_PLUGINS_REQUIRED" not in conf

    def test_bot_conf_emits_fleet_plugins_marketplaces(self, tmp_path):
        from claudlobby.config import PluginsConfig

        plugins = PluginsConfig(
            required=["claudna@Claudfather"],
            marketplaces={
                "Claudfather": {
                    "source": {"source": "github", "repo": "Claudfather/clauDNA"}
                }
            },
        )
        conf = self._compose(tmp_path, plugins=plugins)
        assert (
            "export FLEET_PLUGINS_MARKETPLACES=Claudfather=github:Claudfather/clauDNA"
            in conf
        )

    def test_bot_conf_no_marketplaces_when_empty(self, tmp_path):
        from claudlobby.config import PluginsConfig

        plugins = PluginsConfig(required=["claudna@Claudfather"])
        conf = self._compose(tmp_path, plugins=plugins)
        assert "FLEET_PLUGINS_MARKETPLACES" not in conf


class TestPluginsSettingsLocal:
    """compose_settings_local emits enabledPlugins + extraKnownMarketplaces."""

    def _make_paths_with_runtime(self, tmp_path: Path) -> Paths:
        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "runtime" / "bots").mkdir(parents=True)
        return Paths(root=root, fleet_dir=root)

    def test_settings_local_has_enabled_plugins(self, tmp_path):
        from claudlobby.config import PluginsConfig

        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        plugins = PluginsConfig(
            required=["claudna@Claudfather", "telegram@claude-plugins-official"]
        )
        fleet = FleetConfig(
            name="t", service_prefix="p", bots={"worker": bot}, plugins=plugins
        )
        result = compose_settings_local(bot, fleet, paths)
        assert "enabledPlugins" in result
        assert result["enabledPlugins"] == {
            "claudna@Claudfather": True,
            "telegram@claude-plugins-official": True,
        }

    def test_settings_local_has_extra_marketplaces(self, tmp_path):
        from claudlobby.config import PluginsConfig

        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        plugins = PluginsConfig(
            marketplaces={
                "Claudfather": {
                    "source": {"source": "github", "repo": "Claudfather/clauDNA"}
                },
            },
            required=["claudna@Claudfather"],
        )
        fleet = FleetConfig(
            name="t", service_prefix="p", bots={"worker": bot}, plugins=plugins
        )
        result = compose_settings_local(bot, fleet, paths)
        assert "extraKnownMarketplaces" in result
        assert result["extraKnownMarketplaces"] == {
            "Claudfather": {
                "source": {"source": "github", "repo": "Claudfather/clauDNA"}
            }
        }

    def test_settings_local_no_marketplaces_without_decl(self, tmp_path):
        from claudlobby.config import PluginsConfig

        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        plugins = PluginsConfig(required=["telegram@claude-plugins-official"])
        fleet = FleetConfig(
            name="t", service_prefix="p", bots={"worker": bot}, plugins=plugins
        )
        result = compose_settings_local(bot, fleet, paths)
        assert "enabledPlugins" in result
        assert "extraKnownMarketplaces" not in result


class TestComposeBotEventsDir:
    """compose_bot creates data/events/ directory."""

    def test_events_dir_created(self, tmp_path):
        from claudlobby.composer import compose_bot

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
        (root / "templates").mkdir()
        (root / "templates" / "claude.md.j2").write_text(
            "# {{ bot.name }}\n\n{{ expertise_body }}\n"
        )
        (root / "runtime" / "bots").mkdir(parents=True)
        (root / "lib").mkdir()
        (root / "voices").mkdir()

        paths = Paths(root=root, fleet_dir=root)
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})

        bot_dir = compose_bot(bot, fleet, paths, log=lambda m: None)

        assert (bot_dir / "data").is_dir()
        assert (bot_dir / "data" / "events").is_dir()


class TestComposeBotConfObservability:
    """compose_bot_conf emits OBSERVABILITY_* env vars."""

    def _compose(self, tmp_path, observability=None):
        from claudlobby.composer import compose_bot_conf
        from claudlobby.config import ObservabilityConfig

        obs = observability or ObservabilityConfig()
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle="w_bot"),
            observability=obs,
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

    def test_default_observability_values(self, tmp_path):
        conf = self._compose(tmp_path)
        assert "export OBSERVABILITY_PULSE_INTERVAL=300" in conf
        assert "export OBSERVABILITY_REAP_DAYS=7" in conf

    def test_custom_observability_values(self, tmp_path):
        from claudlobby.config import ObservabilityConfig

        obs = ObservabilityConfig(pulse_interval=60, reap_days=14)
        conf = self._compose(tmp_path, observability=obs)
        assert "export OBSERVABILITY_PULSE_INTERVAL=60" in conf
        assert "export OBSERVABILITY_REAP_DAYS=14" in conf

    def test_observability_section_header(self, tmp_path):
        conf = self._compose(tmp_path)
        assert "# Observability" in conf


class TestComposePermissions:
    """compose_claude_md renders library/permissions/ into CLAUDE.md."""

    def _setup(self, tmp_path, permissions_files: dict[str, str]):
        from claudlobby.composer import compose_claude_md

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
        perm_dir = root / "library" / "permissions"
        perm_dir.mkdir(parents=True)
        for name, content in permissions_files.items():
            (perm_dir / f"{name}.md").write_text(content)
        (root / "templates").mkdir()
        import shutil

        shutil.copy(
            Path(__file__).parent.parent / "templates" / "claude.md.j2",
            root / "templates" / "claude.md.j2",
        )
        (root / "runtime" / "bots").mkdir(parents=True)
        (root / "voices").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        return compose_claude_md, paths

    def test_permissions_rendered_in_claude_md(self, tmp_path):
        compose_claude_md, paths = self._setup(
            tmp_path,
            {
                "read-only-db": "---\ntitle: Read-only database access\ndescription: Restrict to SELECT only\n---\n\n# Read-only database access\n\nNever run INSERT, UPDATE, or DELETE.\n",
            },
        )
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            permissions=["read-only-db"],
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_claude_md(bot, fleet, paths)
        assert "## Permissions" in result
        assert "### Read-only database access" in result
        assert "Never run INSERT, UPDATE, or DELETE." in result

    def test_empty_permissions_omits_section(self, tmp_path):
        compose_claude_md, paths = self._setup(tmp_path, {})
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_claude_md(bot, fleet, paths)
        assert "## Permissions" not in result

    def test_multiple_permissions_rendered(self, tmp_path):
        compose_claude_md, paths = self._setup(
            tmp_path,
            {
                "no-delete": "---\ntitle: No destructive writes\ndescription: No DELETE or DROP\n---\n\n# No destructive writes\n\nNever DELETE or DROP.\n",
                "prod-readonly": "---\ntitle: Production read-only\ndescription: No writes to prod\n---\n\n# Production read-only\n\nProd is read-only.\n",
            },
        )
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            permissions=["no-delete", "prod-readonly"],
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_claude_md(bot, fleet, paths)
        assert "### No destructive writes" in result
        assert "### Production read-only" in result


class TestJinja2Sandbox:
    """Jinja2 sandbox blocks SSTI payloads in startup_prompt and template rendering."""

    def test_render_startup_prompt_allows_documented_variables(self):
        from claudlobby.composer import _render_startup_prompt

        bot = BotConfig(
            bot_id="worker",
            name="Worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle="w_bot"),
        )
        fleet = FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            telegram_group_chat_id="-100999",
        )
        prompt = "Hello {{ bot_name }} on {{ fleet_name }}, id={{ bot_id }}"
        result = _render_startup_prompt(prompt, bot, fleet)
        assert result == "Hello Worker on test-fleet, id=worker"

    def test_render_startup_prompt_blocks_class_traversal(self):
        import pytest
        from jinja2.sandbox import SecurityError
        from claudlobby.composer import _render_startup_prompt

        bot = BotConfig(bot_id="evil", name="evil", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p")
        prompt = "{{ ''.__class__.__mro__[1].__subclasses__() }}"
        with pytest.raises(SecurityError):
            _render_startup_prompt(prompt, bot, fleet)

    def test_render_startup_prompt_blocks_subclass_walk(self):
        import pytest
        from jinja2.sandbox import SecurityError
        from claudlobby.composer import _render_startup_prompt

        bot = BotConfig(bot_id="evil", name="evil", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p")
        # Attempt to walk from str -> object -> subclasses to find Popen
        prompt = "{{ ''.__class__.__mro__[1].__subclasses__()[100] }}"
        with pytest.raises(SecurityError):
            _render_startup_prompt(prompt, bot, fleet)

    def test_build_jinja_env_is_sandboxed(self, tmp_path):
        from jinja2.sandbox import SandboxedEnvironment
        from claudlobby.composer import _build_jinja_env

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "templates").mkdir()
        (root / "templates" / "claude.md.j2").write_text("{{ bot.name }}")
        paths = Paths(root=root, fleet_dir=root)
        env = _build_jinja_env(paths)
        assert isinstance(env, SandboxedEnvironment)

    def test_build_jinja_env_blocks_ssti_in_template(self, tmp_path):
        import pytest
        from jinja2.sandbox import SecurityError
        from claudlobby.composer import _build_jinja_env

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "templates").mkdir()
        (root / "templates" / "evil.j2").write_text(
            "{{ ''.__class__.__mro__[1].__subclasses__() }}"
        )
        paths = Paths(root=root, fleet_dir=root)
        env = _build_jinja_env(paths)
        tmpl = env.get_template("evil.j2")
        with pytest.raises(SecurityError):
            tmpl.render()


class TestComposeBotConfShellEscaping:
    """compose_bot_conf must shell-escape all interpolated values to prevent injection."""

    def _compose(
        self,
        tmp_path,
        env=None,
        bot_id="worker",
        name="worker",
        telegram_handle="w_bot",
    ):
        from claudlobby.composer import compose_bot_conf

        bot = BotConfig(
            bot_id=bot_id,
            name=name,
            expertise=["eng"],
            telegram=TelegramConfig(handle=telegram_handle),
            env=env or {},
        )
        fleet = FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            telegram_group_chat_id="-100999",
        )
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)
        (root / "runtime" / "bots" / bot_id).mkdir(parents=True, exist_ok=True)
        (root / "lib").mkdir(exist_ok=True)
        paths = Paths(root=root, fleet_dir=root)
        return compose_bot_conf(bot, fleet, paths)

    def test_command_substitution_escaped(self, tmp_path):
        """$(cmd) in bot.env must not be interpreted by the shell."""
        conf = self._compose(tmp_path, env={"MALICIOUS": "$(touch /tmp/pwned)"})
        line = [l for l in conf.splitlines() if "MALICIOUS=" in l][0]
        assert line == "export MALICIOUS='$(touch /tmp/pwned)'"

    def test_backtick_escaped(self, tmp_path):
        """`cmd` in bot.env must not be interpreted by the shell."""
        conf = self._compose(tmp_path, env={"BAR": "`whoami`"})
        line = [l for l in conf.splitlines() if "BAR=" in l][0]
        assert line == "export BAR='`whoami`'"

    def test_double_quote_escaped(self, tmp_path):
        conf = self._compose(tmp_path, env={"Q": 'he said "hello"'})
        line = [l for l in conf.splitlines() if l.startswith("export Q=")][0]
        assert line == """export Q='he said "hello"'"""

    def test_newline_in_value(self, tmp_path):
        """Values with newlines must be wrapped safely."""
        conf = self._compose(tmp_path, env={"NL": "line1\nline2"})
        assert "export NL=" in conf

    def test_invalid_env_key_raises(self, tmp_path):
        import pytest

        with pytest.raises(ValueError, match="not a valid shell identifier"):
            self._compose(tmp_path, env={"FOO;ls": "x"})

    def test_invalid_env_key_with_space_raises(self, tmp_path):
        import pytest

        with pytest.raises(ValueError, match="not a valid shell identifier"):
            self._compose(tmp_path, env={"FOO BAR": "x"})

    def test_unsafe_bot_id_raises(self, tmp_path):
        import pytest

        with pytest.raises(ValueError, match="shell-unsafe"):
            self._compose(tmp_path, bot_id="evil$(whoami)")

    def test_safe_values_unquoted(self, tmp_path):
        """Simple alphanumeric values rendered without wrapper quotes."""
        conf = self._compose(tmp_path, env={"SIMPLE": "hello123"})
        assert "export SIMPLE=hello123" in conf

    def test_roundtrip_injection_payload(self, tmp_path):
        """Source generated bot.conf in bash — injection payloads must stay literal."""
        import subprocess

        conf = self._compose(
            tmp_path,
            env={
                "INJECT1": "$(touch /tmp/pwned)",
                "INJECT2": "`whoami`",
                "INJECT3": "${IFS}evil",
            },
        )
        conf_path = tmp_path / "test_bot.conf"
        conf_path.write_text(conf)

        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source {conf_path} && echo "I1=$INJECT1" && echo "I2=$INJECT2" && echo "I3=$INJECT3"',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert "$(touch /tmp/pwned)" in result.stdout
        assert "`whoami`" in result.stdout
        assert "${IFS}evil" in result.stdout


class TestComposeAutonomousRunner:
    """compose_claude_md renders the autonomous_runner block when configured."""

    def _setup(self, tmp_path):
        from claudlobby.composer import compose_claude_md

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
        (root / "templates").mkdir()
        import shutil

        shutil.copy(
            Path(__file__).parent.parent / "templates" / "claude.md.j2",
            root / "templates" / "claude.md.j2",
        )
        (root / "runtime" / "bots").mkdir(parents=True)
        (root / "voices").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        return compose_claude_md, paths

    def test_section_omitted_when_not_configured(self, tmp_path):
        compose_claude_md, paths = self._setup(tmp_path)
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_claude_md(bot, fleet, paths)
        assert "Autonomous Runner" not in result

    def test_minimal_block_rendered(self, tmp_path):
        from claudlobby.config import AutonomousRunnerConfig

        compose_claude_md, paths = self._setup(tmp_path)
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            autonomous_runner=AutonomousRunnerConfig(
                skill="/claudna:tech-debt",
                cadence="1h",
                target_repo="org/repo",
            ),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_claude_md(bot, fleet, paths)
        assert "## Autonomous Runner — Your Continuous Job" in result
        assert "/claudna:tech-debt" in result
        assert "every `1h`" in result
        assert "org/repo" in result
        # Optional fields not present → those bullets omitted
        assert "Additional args" not in result
        assert "Picker:" not in result
        assert "Bypass:" not in result
        assert "Pre-hooks:" not in result
        assert "Post-hooks:" not in result
        assert "On-outcome:" not in result

    def test_full_block_rendered(self, tmp_path):
        from claudlobby.config import (
            AutonomousRunnerBypass,
            AutonomousRunnerConfig,
            AutonomousRunnerPicker,
        )

        compose_claude_md, paths = self._setup(tmp_path)
        bot = BotConfig(
            bot_id="dbt-bot",
            name="dbt-bot",
            expertise=["eng"],
            autonomous_runner=AutonomousRunnerConfig(
                skill="/claudna:implement-plan",
                cadence="2h",
                target_repo="artemis-xyz/dbt",
                args="--source github",
                picker=AutonomousRunnerPicker(
                    type="github_issues",
                    label="claudna-eligible",
                    score_by="mission_alignment",
                ),
                bypass=AutonomousRunnerBypass(
                    risk_classifier="structural_vs_mechanical",
                    block_on=["structural"],
                    on_bypass="comment_and_label",
                ),
                pre_hooks=["/claudna:adversarial-review"],
                post_hooks=["/claudna:simplify"],
                on_outcome={
                    "completed": "report",
                    "blocked": "report_and_pause",
                },
            ),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"dbt-bot": bot})
        result = compose_claude_md(bot, fleet, paths)
        assert "/claudna:implement-plan" in result
        assert "artemis-xyz/dbt" in result
        assert "--source github" in result
        assert "claudna-eligible" in result
        assert "mission_alignment" in result
        assert "structural_vs_mechanical" in result
        assert "structural" in result
        assert "comment_and_label" in result
        assert "/claudna:adversarial-review" in result
        assert "/claudna:simplify" in result
        assert "completed" in result and "report" in result
        assert "blocked" in result and "report_and_pause" in result


class TestResolveEffectiveIntegrations:
    """resolve_effective_integrations: explicit list used verbatim; auto-derived from MCP when empty."""

    def _paths_with_integrations(self, tmp_path: Path, names: list[str]) -> Paths:
        root = tmp_path / "claudlobby"
        root.mkdir()
        int_dir = root / "library" / "integrations"
        int_dir.mkdir(parents=True)
        for name in names:
            (int_dir / f"{name}.md").write_text(f"# {name}\n")
        return Paths(root=root, fleet_dir=root)

    def test_explicit_integrations_used_verbatim(self, tmp_path):
        from claudlobby.composer import resolve_effective_integrations
        from claudlobby.config import McpEntry

        paths = self._paths_with_integrations(tmp_path, ["github", "neon"])
        bot = BotConfig(
            bot_id="w",
            name="w",
            expertise=["eng"],
            integrations=["neon"],  # explicit; github not listed
            mcp=[McpEntry(name="github"), McpEntry(name="neon")],
        )
        assert resolve_effective_integrations(bot, paths) == ["neon"]

    def test_auto_derived_from_mcp_when_empty(self, tmp_path):
        from claudlobby.composer import resolve_effective_integrations
        from claudlobby.config import McpEntry

        # github has an integration doc; slack does not
        paths = self._paths_with_integrations(tmp_path, ["github"])
        bot = BotConfig(
            bot_id="w",
            name="w",
            expertise=["eng"],
            mcp=[McpEntry(name="github"), McpEntry(name="slack")],
        )
        result = resolve_effective_integrations(bot, paths)
        assert result == ["github"]
        assert "slack" not in result

    def test_empty_when_no_mcp_has_integration_doc(self, tmp_path):
        from claudlobby.composer import resolve_effective_integrations
        from claudlobby.config import McpEntry

        # No integration docs exist at all
        paths = self._paths_with_integrations(tmp_path, [])
        bot = BotConfig(
            bot_id="w",
            name="w",
            expertise=["eng"],
            mcp=[McpEntry(name="github")],
        )
        assert resolve_effective_integrations(bot, paths) == []
