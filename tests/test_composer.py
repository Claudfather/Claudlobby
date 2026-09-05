"""Tests for composer.py — tools, hooks, scaffold_env_files, access.json reconcile, bot.conf model strategy, and expertise permissions."""

from __future__ import annotations

import json
import os
import re
import plistlib
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from claudlobby.config import (
    BotConfig,
    FleetConfig,
    McpEntry,
    TeamConfig,
    TelegramConfig,
    ToolPermissionsConfig,
    load_fleet,
)
from claudlobby.path_audit import ExternalDecl
from tests.conftest import _write_exec, git_isolation_env, install_real_template
from claudlobby.composer import (
    _BOOT_STAGGER_SECONDS,
    _compose_hooks,
    _host_boot_rung_bases,
    bot_boot_delay_s,
    _reconcile_access_json,
    _write_timer_units,
    compose_access_json,
    compose_bot,
    GITCONFIG_FILENAME,
    compose_bot_conf,
    compose_fleet,
    compose_mcp_json,
    compose_settings_local,
    compose_systemd_unit,
    scaffold_env_files,
    telegram_handle,
)
from claudlobby.diff import diff_bot
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
                        "GITHUB_PAT": {"description": "GitHub PAT", "default_tier": "fleet"},
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
                            "default_tier": "fleet",
                        },
                        "SHOPIFY_STORE_DOMAIN": {
                            "description": "Shopify domain",
                            "default_tier": "fleet",
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
        fleet, _md = load_fleet(root / "fleet.yaml")

        scaffold_env_files(fleet, paths, log=lambda m: None)

        env_path = root / ".env"
        assert env_path.is_file()
        content = env_path.read_text()
        assert "GITHUB_PAT" in content
        assert "SHOPIFY_ACCESS_TOKEN" in content

    def test_preserves_existing_values_and_appends_new(self, tmp_path):
        root, paths = self._setup_fleet(tmp_path)
        fleet, _md = load_fleet(root / "fleet.yaml")

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
        fleet, _md = load_fleet(root / "fleet.yaml")

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
        # channels=[] — a bot with no Telegram channel; see #1107.
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(bot_id="solo", name="solo", expertise=["eng"], channels=[])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"solo": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert "permissions" not in result

    def test_headless_ux_defaults_emitted(self, tmp_path):
        """The 3 settings.local headless UX keys are always emitted at their defaults."""
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(bot_id="solo", name="solo", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"solo": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert result["spinnerTipsEnabled"] is False
        assert result["preferredNotifChannel"] == "notifications_disabled"
        assert result["prefersReducedMotion"] is True

    def test_headless_ux_defaults_overridable(self, tmp_path):
        """Per-bot overrides flow through to settings.local."""
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="solo",
            name="solo",
            expertise=["eng"],
            spinner_tips_enabled=True,
            preferred_notif_channel="iterm2",
            prefers_reduced_motion=False,
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"solo": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert result["spinnerTipsEnabled"] is True
        assert result["preferredNotifChannel"] == "iterm2"
        assert result["prefersReducedMotion"] is False

    def test_sandbox_disabled_by_default(self, tmp_path):
        """G7: a bot with no sandbox config still composes sandbox.enabled = False.

        The sandbox is off by default as a low-friction system default applied at
        the compose boundary — without this a fleet that omits sandbox config emits
        no sandbox key and a fresh box runs unsandboxed.
        """
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(bot_id="solo", name="solo", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"solo": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert result["sandbox"]["enabled"] is False

    def test_sandbox_enabled_opt_in(self, tmp_path):
        """An explicit sandbox.enabled=True opt-in still composes enabled = True."""
        from claudlobby.config import SandboxConfig

        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="solo",
            name="solo",
            expertise=["eng"],
            sandbox=SandboxConfig(enabled=True),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"solo": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert result["sandbox"]["enabled"] is True

    def test_skip_permission_prompts_emitted(self, tmp_path):
        """G6: both first-run consent skip-flags compose into settings.local at
        their default (True) so a headless bot never hangs on the first-run
        permission prompt."""
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(bot_id="solo", name="solo", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"solo": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert result["skipAutoPermissionPrompt"] is True
        assert result["skipDangerousModePermissionPrompt"] is True

    def test_skip_permission_prompts_overridable(self, tmp_path):
        """Per-bot overrides to False flow through to settings.local."""
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="solo",
            name="solo",
            expertise=["eng"],
            skip_auto_permission_prompt=False,
            skip_dangerous_mode_permission_prompt=False,
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"solo": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert result["skipAutoPermissionPrompt"] is False
        assert result["skipDangerousModePermissionPrompt"] is False

    def test_skip_prompts_independent_of_cli_flag(self, tmp_path):
        """The settings.local skip-flags are a distinct knob from the
        --dangerously-skip-permissions CLI flag: the flag still composes into
        CLAUDE_FLAGS while the settings booleans follow their own values."""

        root = tmp_path / "claudlobby"
        (root / "runtime" / "bots" / "solo").mkdir(parents=True)
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        bot = BotConfig(
            bot_id="solo",
            name="solo",
            expertise=["eng"],
            dangerously_skip_permissions=True,
            skip_auto_permission_prompt=False,
            skip_dangerous_mode_permission_prompt=False,
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"solo": bot})
        settings = compose_settings_local(bot, fleet, paths)
        conf = compose_bot_conf(bot, fleet, paths)
        # settings.local booleans follow their own (False) values...
        assert settings["skipAutoPermissionPrompt"] is False
        assert settings["skipDangerousModePermissionPrompt"] is False
        # ...while the CLI flag still lands in CLAUDE_FLAGS, unaffected.
        assert "--dangerously-skip-permissions" in conf

    def test_sibling_isolation_only(self, tmp_path):
        """R9 cross-bot isolation: TWO //-absolute rules per sibling (#1312, #873).

        Was Read/Write/Edit with bare-absolute paths, and all three were inert or
        useless: a single leading slash anchors at the SETTINGS SOURCE, so the
        path named a directory that never existed, and Claude Code accepts a
        Write(path) rule without ever consulting it.
        """
        paths = self._make_paths_with_runtime(tmp_path)
        fleet = self._make_fleet_with_bots("bot-a", "bot-b")
        result = compose_settings_local(fleet.bots["bot-a"], fleet, paths)
        assert "permissions" in result
        deny = result["permissions"]["deny"]
        assert len(deny) == 2
        assert {d.split("(")[0] for d in deny} == {"Read", "Edit"}
        assert all("bot-b" in d for d in deny)
        # The prefix is the whole fix, so assert it on EVERY rule rather than
        # trusting that one correct rule implies the rest.
        assert all(d.split("(", 1)[1].startswith("//") for d in deny), deny

    def test_tool_deny_generates_patterns(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="reviewer",
            name="reviewer",
            expertise=["code-review"],
            tool_permissions=ToolPermissionsConfig(
                deny=["Write", "Edit", "NotebookEdit"]
            ),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"reviewer": bot})
        result = compose_settings_local(bot, fleet, paths)
        deny = result["permissions"]["deny"]
        assert "Write" in deny
        assert "Edit" in deny
        assert "NotebookEdit" in deny

    def test_tool_allow_generates_patterns(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(
            bot_id="reader",
            name="reader",
            expertise=["eng"],
            tool_permissions=ToolPermissionsConfig(allow=["Read", "Grep", "Glob"]),
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
            tool_permissions=ToolPermissionsConfig(
                deny=["Write", "Edit"], allow=["Agent", "Bash"]
            ),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"lead": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert "Write" in result["permissions"]["deny"]
        assert "Agent" in result["permissions"]["allow"]

    def test_tool_deny_merged_with_sibling_isolation(self, tmp_path):
        paths = self._make_paths_with_runtime(tmp_path)
        bot_a = BotConfig(
            bot_id="bot-a",
            name="bot-a",
            expertise=["code-review"],
            tool_permissions=ToolPermissionsConfig(deny=["Write"]),
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
        assert "Write" in deny


class TestComposeBotConfModelStrategy:
    """compose_bot_conf emits MODEL_STRATEGY_* env vars when model_strategy is set."""

    def _compose(self, tmp_path, model_strategy=None, model=None):

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

    def test_model_flag_fable_in_claude_flags(self, tmp_path):
        conf = self._compose(tmp_path, model="fable")
        assert "--model fable" in conf

    def test_model_strategy_section_header(self, tmp_path):
        from claudlobby.config import ModelStrategyConfig

        ms = ModelStrategyConfig(base="sonnet")
        conf = self._compose(tmp_path, model_strategy=ms)
        assert "# Model strategy" in conf


class TestComposeBotConfExportedVars:
    """bot.conf must export BOT_ID so hook subprocesses (bot-vitals.sh) can read it."""

    def test_bot_id_is_exported(self, tmp_path):

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

    def test_disable_nonessential_traffic_default_and_override(self, tmp_path):

        def _conf(**kw):
            bot = BotConfig(
                bot_id="w",
                name="w",
                expertise=["eng"],
                telegram=TelegramConfig(handle="w_bot"),
                **kw,
            )
            fleet = FleetConfig(
                name="test-fleet",
                service_prefix="com.test",
                telegram_group_chat_id="-100999",
            )
            root = tmp_path / "cl"
            (root / "runtime" / "bots" / "w").mkdir(parents=True, exist_ok=True)
            (root / "lib").mkdir(exist_ok=True)
            return compose_bot_conf(bot, fleet, Paths(root=root, fleet_dir=root))

        # Default on → the RC-safe granular set, never the umbrella (which
        # silently disables --remote-control — #533).
        conf = _conf()
        assert "export DISABLE_AUTOUPDATER=1" in conf
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in conf
        assert "DISABLE_TELEMETRY" not in conf
        # Override off → omitted entirely (never "=0", which CC could read as truthy).
        assert "DISABLE_AUTOUPDATER" not in _conf(disable_nonessential_traffic=False)


class TestComposeBotConfServicePrefix:
    """compose_bot_conf derives BOT_SERVICE and SERVICE_PREFIX from fleet config."""

    def test_service_prefix_from_fleet_config(self, tmp_path):

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


class TestComposeBotConfFleetRoot:
    """FLEET_ROOT — the fleet-scoped sibling of CLAUDLOBBY_ROOT. Anchors every
    fleet-relative path (secret files, MCP build dirs) so they resolve to the
    fleet's real, nested location and survive a fleet move by construction."""

    def _bot(self):
        return BotConfig(
            bot_id="kev",
            name="kev",
            expertise=["eng"],
            telegram=TelegramConfig(handle="kev_bot"),
        )

    def _fleet(self):
        return FleetConfig(
            name="tl",
            service_prefix="com.crog.tl",
            telegram_group_chat_id="-100999",
        )

    def test_root_mode_fleet_root_equals_install_root(self, tmp_path):

        root = tmp_path / "claudlobby"
        (root / "runtime" / "bots" / "kev").mkdir(parents=True)
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        conf = compose_bot_conf(self._bot(), self._fleet(), paths)
        # Root-mode fleet IS the install root — the anchor collapses cleanly.
        assert 'export FLEET_ROOT="$CLAUDLOBBY_ROOT"' in conf

    def test_nested_overlay_fleet_root_is_anchor_relative(self, tmp_path):

        root = tmp_path / "claudlobby"
        fleet_dir = root / "local" / "home" / "tl"
        (fleet_dir / "runtime" / "bots" / "kev").mkdir(parents=True)
        (root / "lib").mkdir(parents=True)
        paths = Paths(root=root, fleet_dir=fleet_dir)
        conf = compose_bot_conf(self._bot(), self._fleet(), paths)
        # Nested overlay — anchored under the install root, so a re-nest of the
        # fleet only moves FLEET_ROOT; every derived path follows.
        assert 'export FLEET_ROOT="$CLAUDLOBBY_ROOT/local/home/tl"' in conf

    def test_vault_mode_fleet_root_is_absolute(self, tmp_path):

        root = tmp_path / "install"
        vault_fleet = tmp_path / "vault" / "tl"
        (vault_fleet / "runtime" / "bots" / "kev").mkdir(parents=True)
        (root / "lib").mkdir(parents=True)
        paths = Paths(root=root, fleet_dir=vault_fleet)
        conf = compose_bot_conf(self._bot(), self._fleet(), paths)
        # Fleet outside the install tree (vault) — absolute, no anchor to lean on.
        assert f"export FLEET_ROOT={vault_fleet}" in conf

    def test_fleet_root_always_emitted(self, tmp_path):

        root = tmp_path / "claudlobby"
        (root / "runtime" / "bots" / "kev").mkdir(parents=True)
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        conf = compose_bot_conf(self._bot(), self._fleet(), paths)
        assert "FLEET_ROOT" in conf


class TestComposeBotConfSecretFiles:
    """secret_files declares fleet-relative paths to secret files (GA4/GSC keys,
    credentials). The composer anchors them on FLEET_ROOT so the path is derived,
    never hand-typed absolute — the migration-dangle class this closes. The secret
    VALUES stay in .env; only the derivable PATHS are composed."""

    def _fleet(self):
        return FleetConfig(
            name="tl", service_prefix="com.crog.tl", telegram_group_chat_id="-100999"
        )

    def _paths(self, tmp_path):
        root = tmp_path / "claudlobby"
        fleet_dir = root / "local" / "home" / "tl"
        (fleet_dir / "runtime" / "bots" / "kev").mkdir(parents=True)
        (root / "lib").mkdir(parents=True)
        return Paths(root=root, fleet_dir=fleet_dir)

    def _bot(self, **kw):
        return BotConfig(
            bot_id="kev",
            name="kev",
            expertise=["eng"],
            telegram=TelegramConfig(handle="kev_bot"),
            **kw,
        )

    def test_secret_file_path_anchored_on_fleet_root(self, tmp_path):

        bot = self._bot(
            secret_files={"GA4_SA_KEY_PATH": ".secrets/ga4-service-account.json"}
        )
        conf = compose_bot_conf(bot, self._fleet(), self._paths(tmp_path))
        assert (
            'export GA4_SA_KEY_PATH="$FLEET_ROOT/.secrets/ga4-service-account.json"'
            in conf
        )

    def test_multiple_secret_files_all_emitted(self, tmp_path):

        bot = self._bot(
            secret_files={
                "GA4_SA_KEY_PATH": ".secrets/ga4-service-account.json",
                "GSC_SA_KEY_PATH": ".secrets/ga4-service-account.json",
            }
        )
        conf = compose_bot_conf(bot, self._fleet(), self._paths(tmp_path))
        assert (
            'export GA4_SA_KEY_PATH="$FLEET_ROOT/.secrets/ga4-service-account.json"'
            in conf
        )
        assert (
            'export GSC_SA_KEY_PATH="$FLEET_ROOT/.secrets/ga4-service-account.json"'
            in conf
        )

    def test_no_secret_files_emits_nothing(self, tmp_path):

        conf = compose_bot_conf(self._bot(), self._fleet(), self._paths(tmp_path))
        assert "SA_KEY_PATH" not in conf

    def test_absolute_subpath_rejected(self, tmp_path):
        """An absolute path defeats the whole purpose — it is exactly the
        hand-typed path this feature removes. Fail loud at compose."""

        from claudlobby.composer import compose_bot_conf

        bot = self._bot(
            secret_files={"GA4_SA_KEY_PATH": "/home/user/local/tl/.secrets/ga4.json"}
        )
        with pytest.raises(ValueError, match="fleet-relative"):
            compose_bot_conf(bot, self._fleet(), self._paths(tmp_path))

    def test_bad_var_name_rejected(self, tmp_path):

        from claudlobby.composer import compose_bot_conf

        bot = self._bot(secret_files={"not a var": ".secrets/x.json"})
        with pytest.raises(ValueError, match="shell identifier"):
            compose_bot_conf(bot, self._fleet(), self._paths(tmp_path))

    def test_subpath_command_subst_rejected(self, tmp_path):
        """The secret-file path is anchored on FLEET_ROOT and emitted
        double-quoted, so a command substitution in the subpath would execute
        when bot.conf is sourced — reject it loud (#731)."""
        bot = self._bot(secret_files={"K": ".secrets/$(touch pwned).json"})
        with pytest.raises(ValueError, match="fleet-relative"):
            compose_bot_conf(bot, self._fleet(), self._paths(tmp_path))

    def test_subpath_backtick_rejected(self, tmp_path):
        bot = self._bot(secret_files={"K": ".secrets/`id`.json"})
        with pytest.raises(ValueError, match="fleet-relative"):
            compose_bot_conf(bot, self._fleet(), self._paths(tmp_path))

    def test_subpath_double_quote_rejected(self, tmp_path):
        bot = self._bot(secret_files={"K": '.secrets/a".json'})
        with pytest.raises(ValueError, match="fleet-relative"):
            compose_bot_conf(bot, self._fleet(), self._paths(tmp_path))


class TestComposerProvidedPathAnchorsExported:
    """Every blessed path anchor is actually assigned in bot.conf, so a
    ${ANCHOR} token in .mcp.json has a value to expand at runtime. Ties the
    anchor SSOT (path_audit.COMPOSER_PROVIDED_PATH_ANCHORS) to the emission —
    drop an export and this fails."""

    def test_all_anchors_assigned_in_bot_conf(self, tmp_path):
        from claudlobby.path_audit import COMPOSER_PROVIDED_PATH_ANCHORS

        bot = BotConfig(
            bot_id="kev",
            name="kev",
            expertise=["eng"],
            telegram=TelegramConfig(handle="kev_bot"),
        )
        fleet = FleetConfig(
            name="tl", service_prefix="com.crog.tl", telegram_group_chat_id="-100999"
        )
        root = tmp_path / "claudlobby"
        fleet_dir = root / "local" / "home" / "tl"
        (fleet_dir / "runtime" / "bots" / "kev").mkdir(parents=True)
        (root / "lib").mkdir(parents=True)
        paths = Paths(root=root, fleet_dir=fleet_dir)
        conf = compose_bot_conf(bot, fleet, paths)
        for anchor in COMPOSER_PROVIDED_PATH_ANCHORS:
            assert f"{anchor}=" in conf, f"{anchor} not assigned in bot.conf"


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

        fleet, _md = load_fleet(root / "fleet.yaml")
        bot = fleet.bots["worker"]

        # Verify merge: system defaults + fleet defaults + bot overrides.
        # System injects bot-vitals.sh on PreToolUse & PostToolUse.
        # Fleet defaults add log-pre.sh / log-post.sh.
        # Bot stanza adds notify.sh on PostToolUse.
        assert "PreToolUse" in bot.hooks
        pre_cmds = [h["command"] for h in bot.hooks["PreToolUse"]]
        assert "$CLAUDLOBBY_ROOT/lib/bot-vitals.sh" in pre_cmds
        assert "log-pre.sh" in pre_cmds

        assert "PostToolUse" in bot.hooks
        post_cmds = [h["command"] for h in bot.hooks["PostToolUse"]]
        assert "$CLAUDLOBBY_ROOT/lib/bot-vitals.sh" in post_cmds
        assert "log-post.sh" in post_cmds
        assert "notify.sh" in post_cmds

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
              system_defaults: false
              bots:
                worker:
                  expertise: [eng]
                  hooks:
                    PreToolUse:
                      - command: "my-hook.sh"
                        matcher: "Write|Edit"
        """)
        )

        fleet, _md = load_fleet(root / "fleet.yaml")
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
              system_defaults: false
              defaults:
                hooks:
                  PostToolUse:
                    - command: "fleet-log.sh"
              bots:
                worker:
                  expertise: [eng]
        """)
        )

        fleet, _md = load_fleet(root / "fleet.yaml")
        bot = fleet.bots["worker"]
        assert len(bot.hooks["PostToolUse"]) == 1
        assert bot.hooks["PostToolUse"][0]["command"] == "fleet-log.sh"


class TestTelegramHandleDefault:
    """The handle defaults to bot_id for a channel bot (#1097, #1107).

    `fleet.yaml` documents "handle defaults to bot_id" and TELEGRAM_STATE_DIR has
    applied that since #43. The handle VARIABLE did not, so a fleet using the
    default composed a live bridge with no TELEGRAM_BOT_HANDLE — and every
    consumer reading it classified a working channel bot as non-channel.
    """

    def test_explicit_handle_wins(self):
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle="custom_handle"),
        )
        assert telegram_handle(bot) == "custom_handle"

    def test_defaults_to_bot_id_for_channel_bot(self):
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        assert telegram_handle(bot) == "worker"

    def test_defaults_to_bot_id_not_display_name(self):
        """bot_id, never name: the state dir keys off bot_id and the two must agree."""
        bot = BotConfig(bot_id="worker", name="Display Name", expertise=["eng"])
        assert telegram_handle(bot) == "worker"

    def test_none_for_non_channel_bot(self):
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"], channels=[])
        assert telegram_handle(bot) is None

    def test_bare_channel_name_counts_as_telegram(self):
        bot = BotConfig(
            bot_id="worker", name="worker", expertise=["eng"], channels=["telegram"]
        )
        assert telegram_handle(bot) == "worker"

    def test_non_telegram_channel_is_not_a_telegram_bot(self):
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            channels=["plugin:slack@somewhere"],
        )
        assert telegram_handle(bot) is None

    def test_bot_conf_emits_defaulted_handle(self, tmp_path):
        """The regression that broke bridge_state and creds-check: the emitted line."""
        paths = _make_paths(tmp_path)
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        conf = compose_bot_conf(bot, fleet, paths)
        assert "export TELEGRAM_BOT_HANDLE=worker" in conf

    def test_bot_conf_omits_handle_for_non_channel_bot(self, tmp_path):
        paths = _make_paths(tmp_path)
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"], channels=[])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        conf = compose_bot_conf(bot, fleet, paths)
        assert "TELEGRAM_BOT_HANDLE" not in conf

    def test_declared_handle_also_emits_username(self, tmp_path):
        """A declared handle is a @username too — creds-check's cross-wire input."""
        paths = _make_paths(tmp_path)
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle="example_worker_bot"),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        conf = compose_bot_conf(bot, fleet, paths)
        assert "export TELEGRAM_BOT_HANDLE=example_worker_bot" in conf
        assert "export TELEGRAM_BOT_USERNAME=example_worker_bot" in conf

    def test_defaulted_handle_emits_no_username(self, tmp_path):
        """A slug is not a username: comparing them false-fails a correct token."""
        paths = _make_paths(tmp_path)
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        conf = compose_bot_conf(bot, fleet, paths)
        assert "export TELEGRAM_BOT_HANDLE=worker" in conf
        assert "TELEGRAM_BOT_USERNAME" not in conf

    def test_state_dir_and_handle_agree(self, tmp_path):
        """Both sites resolve through one function, so they cannot drift apart again."""
        paths = _make_paths(tmp_path)
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        conf = compose_bot_conf(bot, fleet, paths)
        assert "channels/telegram-worker" in conf
        assert "export TELEGRAM_BOT_HANDLE=worker" in conf


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

    def test_returns_telegram_tools_when_handle_defaulted(self):
        """A channel bot relying on the documented handle default still gets its tools.

        Keying off the raw field granted these to zero bots on a fleet that omits
        `handle` — the #1097/#1107 conflation of "no explicit handle" with "no
        channel". A bot's own reply tools must not depend on spelling its handle.
        """
        from claudlobby.composer import _resolve_channel_permissions

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle=None),
        )
        result = _resolve_channel_permissions(bot)
        assert len(result) == 4
        assert "mcp__plugin_telegram_telegram__reply" in result

    def test_returns_empty_when_no_telegram_channel(self):
        """The genuine non-channel class — the invariant #901's gate contract rests on."""
        from claudlobby.composer import _resolve_channel_permissions

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            channels=[],
            telegram=TelegramConfig(handle=None),
        )
        assert _resolve_channel_permissions(bot) == []

    def test_empty_string_handle_falls_back_to_default(self):
        from claudlobby.composer import _resolve_channel_permissions

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle=""),
        )
        result = _resolve_channel_permissions(bot)
        assert len(result) == 4


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
            tool_permissions=ToolPermissionsConfig(deny=["Write"]),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert "Write" in result["permissions"]["deny"]
        # Auto-derived still in allow
        assert "mcp__plugin_telegram_telegram__reply" in result["permissions"]["allow"]

    def test_no_telegram_no_skills_no_permissions(self, tmp_path):
        """Bot with no telegram, no skills, no explicit tools → no permissions block.

        `channels=[]` is what "no telegram" means; omitting the handle does not,
        since `channels` defaults to the Telegram plugin (#1107).
        """
        paths = self._make_paths_with_runtime(tmp_path)
        bot = BotConfig(bot_id="solo", name="solo", expertise=["eng"], channels=[])
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
            tool_permissions=ToolPermissionsConfig(allow=["Bash", "Agent"]),
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
            # Pair every fragment with an integration file carrying covering
            # tool_grants — the invariant the grant-superset gate enforces.
            tools = content.get("_permissions_contract", {}).get("tools", [])
            _write_integration(
                root, name, tool_grants=[f"mcp__{name}__*"] if tools else []
            )
        return Paths(root=root, fleet_dir=root)

    def test_mcp_trust_allowlist_sorted_no_blanket(self, tmp_path):
        """enabledMcpjsonServers = sorted server set; blanket enableAllProjectMcpServers never emitted."""
        paths = self._setup_mcp_library(tmp_path, {})
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths, ["notion", "github"])
        assert result["enabledMcpjsonServers"] == ["github", "notion"]
        assert "enableAllProjectMcpServers" not in result

    def test_mcp_trust_absent_without_servers(self, tmp_path):
        """No project MCP servers → neither trust key is emitted (nothing to trust)."""
        paths = self._setup_mcp_library(tmp_path, {})
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        for names in (None, []):
            result = compose_settings_local(bot, fleet, paths, names)
            assert "enabledMcpjsonServers" not in result
            assert "enableAllProjectMcpServers" not in result

    def test_mcp_trust_allowlist_matches_composed_mcp_json(self, tmp_path):
        """The allowlist equals the composed .mcp.json server keys exactly — no drift."""

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
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        mcp = compose_mcp_json(bot, paths)
        result = compose_settings_local(
            bot, fleet, paths, list(mcp["mcpServers"].keys())
        )
        assert result["enabledMcpjsonServers"] == sorted(mcp["mcpServers"].keys())
        assert result["enabledMcpjsonServers"] == ["github"]

    def test_mcp_permissions_in_allow_list(self, tmp_path):
        """Wildcard is emitted for github MCP in settings.local allow list."""

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
        """MCP permissions should appear even if bot.tool_permissions.allow is empty."""

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
            tool_permissions=ToolPermissionsConfig(allow=[], deny=[]),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths)
        assert "permissions" in result
        assert "mcp__notion__*" in result["permissions"]["allow"]

    def test_explicit_deny_alongside_mcp_permissions(self, tmp_path):

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
            tool_permissions=ToolPermissionsConfig(deny=["Write", "Edit"]),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        result = compose_settings_local(bot, fleet, paths)
        deny = result["permissions"]["deny"]
        allow = result["permissions"]["allow"]
        assert "Write" in deny
        assert "Edit" in deny
        assert "mcp__github__*" in allow

    def test_multi_instance_in_settings_local(self, tmp_path):

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
        assert "Write" in deny
        assert "Edit" in deny
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
        assert "Write" in deny
        assert "Edit" in deny
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
            tool_permissions=ToolPermissionsConfig(deny=["Write", "Edit"]),
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
        result = compose_settings_local(bot, fleet, paths)
        deny = result["permissions"]["deny"]
        allow = result["permissions"]["allow"]
        assert "Write" in deny
        assert "Edit" in deny
        # Bot deny removes from allow
        assert "Write" not in allow
        assert "Edit" not in allow
        # Other tools still present
        assert "Bash" in allow
        assert "Read" in allow

    def test_expertise_plus_mcp_plus_skills_combined(self, tmp_path):
        """Full integration: expertise + MCP contracts + skills + telegram all compose."""

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
        _write_integration(root, "github", tool_grants=["mcp__github__*"])

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
        assert "Write" in deny
        assert "Edit" in deny
        assert "NotebookEdit" in deny
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

    # Anchored to line starts: the unit body documents the boot state machine in
    # comments, and a comment naming a directive is not that directive.
    @staticmethod
    def _stagger(unit: str) -> str | None:
        m = re.search(r"^ExecStartPre=(.*)$", unit, re.M)
        return m.group(1) if m else None

    def test_no_stagger_when_delay_zero(self, tmp_path):
        bot, fleet, paths = self._make(tmp_path)
        unit = compose_systemd_unit(bot, fleet, paths, boot_delay_s=0)
        assert self._stagger(unit) is None

    def test_stagger_injected_when_delay_positive(self, tmp_path):
        bot, fleet, paths = self._make(tmp_path)
        unit = compose_systemd_unit(bot, fleet, paths, boot_delay_s=3)
        assert self._stagger(unit) == "/bin/sleep 3"

    def test_stagger_value_varies(self, tmp_path):
        bot, fleet, paths = self._make(tmp_path)
        unit6 = compose_systemd_unit(bot, fleet, paths, boot_delay_s=6)
        assert self._stagger(unit6) == "/bin/sleep 6"
        unit0 = compose_systemd_unit(bot, fleet, paths, boot_delay_s=0)
        assert self._stagger(unit0) is None

    def test_unit_shape_keeps_substate_meaningful(self, tmp_path):
        """The three directives service_is_starting depends on (#1002).

        lib/lib-common.sh service_is_starting reads active/running as "boot in
        flight". That is only true while ExecStart is a spawner that exits and
        RemainAfterExit holds the unit active afterwards — together they make
        active/exited the steady state. Drop either and active/running becomes
        the steady state, so the predicate returns "starting" forever and
        BOTH keepalive's dead-session watchdog and fleet-pulse's service_down
        alarm go permanently quiet while every surface reads healthy.

        This asserts the shape so that change breaks a test instead.
        """
        bot, fleet, paths = self._make(tmp_path)
        unit = compose_systemd_unit(bot, fleet, paths, boot_delay_s=0)
        assert "Type=simple" in unit
        assert "RemainAfterExit=yes" in unit
        # The spawner half: ExecStart runs start-bot.sh, which backgrounds tmux
        # and returns. A foreground exec here would invert SubState's meaning.
        assert re.search(r"^ExecStart=\S*start-bot\.sh ", unit, re.M)


class TestHostBootOffset:
    """Host-global boot ladder: managers first, then workers, one per rung (#1002)."""

    # spec shape, shared by every helper below:
    #   {fleet_name: (n_bots, [indices of bots a team names as manager])}
    # Manager indices rather than "the first N", so a fleet whose manager is not
    # declared first still exercises the tier split.

    @pytest.fixture(autouse=True)
    def _clear_ladder_cache(self):
        """The walk is cached per process; tests reuse paths across mutations."""
        _host_boot_rung_bases.cache_clear()
        yield
        _host_boot_rung_bases.cache_clear()

    def _host(self, tmp_path, spec, nested: bool = False):
        """Build local/<fleet>/fleet.yaml (or local/<system>/<fleet>/) trees."""
        root = tmp_path / "claudlobby"
        local = root / "local"
        for name, (n_bots, mgr_idx) in spec.items():
            d = (local / "sys" / name) if nested else (local / name)
            d.mkdir(parents=True)
            doc = {
                "fleet": {
                    "name": name,
                    "teams": {
                        f"t{i}": {"manager": f"b{m}"} for i, m in enumerate(mgr_idx)
                    },
                    "bots": {f"b{i}": {"expertise": ["eng"]} for i in range(n_bots)},
                }
            }
            (d / "fleet.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
        return root, local

    def _fleet(self, name, spec):
        """A FleetConfig matching this fleet's entry in ``spec``."""
        n_bots, mgr_idx = spec[name]
        bots = {
            f"b{i}": BotConfig(bot_id=f"b{i}", name=f"b{i}", expertise=["eng"])
            for i in range(n_bots)
        }
        teams = {
            f"t{i}": TeamConfig(name=f"t{i}", manager=f"b{m}")
            for i, m in enumerate(mgr_idx)
        }
        return FleetConfig(name=name, service_prefix="p", bots=bots, teams=teams)

    def _bases(self, root, local, spec, name, nested: bool = False):
        """This fleet's (manager_base, worker_base), via the real door."""
        d = (local / "sys" / name) if nested else (local / name)
        own_managers = len(set(spec[name][1]))
        return _host_boot_rung_bases(Paths(root=root, fleet_dir=d), own_managers)

    def _rungs(self, root, local, spec):
        """Every bot's delay on the host, keyed <fleet>.<bot>, via the real door."""
        out = {}
        for name in spec:
            fleet = self._fleet(name, spec)
            paths = Paths(root=root, fleet_dir=local / name)
            for bot in fleet.bots.values():
                out[f"{name}.{bot.bot_id}"] = bot_boot_delay_s(bot, fleet, paths)
        return out

    def test_first_fleet_starts_at_zero(self, tmp_path):
        spec = {"aaa": (3, [0]), "bbb": (2, [0])}
        root, local = self._host(tmp_path, spec)
        # 2 managers on the host, so aaa's workers start at rung 2.
        assert self._bases(root, local, spec, "aaa") == (0, 2)

    def test_later_fleet_starts_past_the_earlier_ones(self, tmp_path):
        """Managers ladder past earlier managers; workers past ALL managers."""
        spec = {"aaa": (3, [0]), "bbb": (2, [0]), "ccc": (4, [0])}
        root, local = self._host(tmp_path, spec)
        # 3 managers on the host, so every worker block starts at rung 3 or past.
        assert self._bases(root, local, spec, "aaa") == (0, 3)
        assert self._bases(root, local, spec, "bbb") == (1, 5)
        assert self._bases(root, local, spec, "ccc") == (2, 6)

    def test_no_two_bots_share_a_rung(self, tmp_path):
        """The property the ladder exists for, asserted end to end.

        Manager-first reorders the ladder; it must not perforate or overrun it.
        """
        spec = {"aaa": (3, [0]), "bbb": (2, [1]), "ccc": (4, [0, 2])}
        root, local = self._host(tmp_path, spec)
        rungs = self._rungs(root, local, spec)
        assert len(rungs) == 9
        assert sorted(rungs.values()) == [i * _BOOT_STAGGER_SECONDS for i in range(9)]

    def test_all_managers_precede_all_workers(self, tmp_path):
        """The change itself: the whole observation layer is up first.

        Not "each fleet's manager leads its own block" — that was already true
        of the contiguous per-fleet ladder. The property asserted is host-global.
        """
        spec = {"aaa": (3, [0]), "bbb": (2, [1]), "ccc": (4, [0, 2])}
        root, local = self._host(tmp_path, spec)
        rungs = self._rungs(root, local, spec)
        mgrs = {"aaa.b0", "bbb.b1", "ccc.b0", "ccc.b2"}
        assert max(rungs[k] for k in mgrs) < min(
            v for k, v in rungs.items() if k not in mgrs
        )
        # 4 managers → the first four rungs, in fleet order.
        assert sorted(rungs[k] for k in mgrs) == [0, 3, 6, 9]

    def test_last_fleets_manager_boots_before_first_fleets_worker(self, tmp_path):
        """The inversion the ordering buys, stated as the case that changed."""
        spec = {"aaa": (3, [0]), "zzz": (2, [0])}
        root, local = self._host(tmp_path, spec)
        rungs = self._rungs(root, local, spec)
        assert rungs["zzz.b0"] < rungs["aaa.b1"]

    def test_manager_declared_last_still_takes_a_manager_rung(self, tmp_path):
        """Tier membership comes from teams, not from declaration position."""
        spec = {"aaa": (3, [2])}
        root, local = self._host(tmp_path, spec)
        rungs = self._rungs(root, local, spec)
        assert rungs["aaa.b2"] == 0
        assert sorted(rungs.values()) == [0, 3, 6]

    def test_two_teams_one_manager_counts_once(self, tmp_path):
        """A bot managing two teams occupies one rung, not two."""
        spec = {"aaa": (3, [0, 0]), "bbb": (2, [0])}
        root, local = self._host(tmp_path, spec)
        rungs = self._rungs(root, local, spec)
        assert sorted(rungs.values()) == [i * _BOOT_STAGGER_SECONDS for i in range(5)]

    def test_team_naming_an_absent_bot_does_not_shift_the_ladder(self, tmp_path):
        """Only bots the manifest lists are counted, or later fleets slide off."""
        spec = {"aaa": (2, []), "bbb": (2, [0])}
        root, local = self._host(tmp_path, spec)
        (local / "aaa" / "fleet.yaml").write_text(
            "fleet:\n  name: aaa\n  teams:\n    t0: {manager: ghost}\n"
            "  bots:\n    b0: {expertise: [eng]}\n    b1: {expertise: [eng]}\n",
            encoding="utf-8",
        )
        # aaa contributes 0 managers and 2 workers; bbb's manager still leads.
        assert self._bases(root, local, spec, "bbb") == (0, 3)

    def test_fleet_with_no_teams_is_all_workers(self, tmp_path):
        spec = {"aaa": (3, []), "bbb": (2, [0])}
        root, local = self._host(tmp_path, spec)
        # Only bbb has a manager, so it owns rung 0 and aaa's bots follow it.
        assert self._bases(root, local, spec, "aaa") == (0, 1)
        assert self._bases(root, local, spec, "bbb") == (0, 4)

    def test_nested_system_container_fleets_are_ordered_too(self, tmp_path):
        """local/<system>/<fleet>/ resolves like the flat layout."""
        spec = {"aaa": (3, [0]), "bbb": (2, [0])}
        root, local = self._host(tmp_path, spec, nested=True)
        assert self._bases(root, local, spec, "bbb", nested=True) == (1, 4)

    def test_unplaceable_fleet_ladders_standalone_managers_first(self, tmp_path):
        """No host placement is not licence to stack two bots on rung 0."""
        spec = {"aaa": (3, [1])}
        root, _ = self._host(tmp_path, spec)
        paths = Paths(root=root, fleet_dir=None)  # root mode
        assert _host_boot_rung_bases(paths, 1) == (0, 1)
        fleet = self._fleet("aaa", spec)
        rungs = [bot_boot_delay_s(b, fleet, paths) for b in fleet.bots.values()]
        assert rungs == [3, 0, 6]  # b1 manages, so it leads; b0/b2 follow in order

    def _host_manages_only(self, tmp_path):
        """Two fleets where the FIRST declares its manager only via ``manages:``.

        The ``spec`` helper above can express a ``teams:`` manager and nothing
        else, which is precisely what hid this: every existing multi-fleet test
        declares managers the single way the sibling-counting helper happened
        to read, so the host walk was never exercised against the other
        declaration. The only test that used ``manages:`` as a real manager
        designation runs in ROOT mode, which returns before the sibling walk.
        """
        root = tmp_path / "claudlobby"
        local = root / "local"
        docs = {
            # No teams: block at all — b0 manages b1 and nothing else says so.
            "aaa": {
                "name": "aaa",
                "bots": {
                    "b0": {"expertise": ["eng"], "manages": ["b1"]},
                    "b1": {"expertise": ["eng"]},
                },
            },
            "bbb": {
                "name": "bbb",
                "teams": {"t0": {"manager": "b0"}},
                "bots": {"b0": {"expertise": ["eng"]}, "b1": {"expertise": ["eng"]}},
            },
        }
        for name, fleet in docs.items():
            d = local / name
            d.mkdir(parents=True)
            (d / "fleet.yaml").write_text(
                yaml.safe_dump({"fleet": fleet}), encoding="utf-8"
            )
        return root, local

    def _manages_only_fleets(self):
        aaa = FleetConfig(
            name="aaa",
            service_prefix="p",
            bots={
                "b0": BotConfig(
                    bot_id="b0", name="b0", expertise=["eng"], manages=["b1"]
                ),
                "b1": BotConfig(bot_id="b1", name="b1", expertise=["eng"]),
            },
        )
        bbb = FleetConfig(
            name="bbb",
            service_prefix="p",
            bots={
                "b0": BotConfig(bot_id="b0", name="b0", expertise=["eng"]),
                "b1": BotConfig(bot_id="b1", name="b1", expertise=["eng"]),
            },
            teams={"t0": TeamConfig(name="t0", manager="b0")},
        )
        return aaa, bbb

    def test_a_manages_only_manager_in_an_earlier_fleet_is_counted(self, tmp_path):
        """A sibling's manager counts however it was DECLARED, not however the
        counting helper happened to look for it.

        ``aaa`` contributes one manager and one worker. Undercounting its
        manager tier shifts every later fleet's rung base, so this asserts on
        the later fleet: it is the one that inherits the shortfall.
        """
        root, local = self._host_manages_only(tmp_path)
        bases = _host_boot_rung_bases(Paths(root=root, fleet_dir=local / "bbb"), 1)
        assert bases == (1, 3), (
            "bbb's manager must ladder past aaa's; a manages:-only manager that "
            "the sibling count cannot see collapses that gap"
        )

    def test_manages_only_manager_takes_its_own_rung_on_the_host_ladder(self, tmp_path):
        """The whole ladder, as exact rungs — asserted through
        ``bot_boot_delay_s``, the value a composed unit actually carries.

        THE INVARIANT IS ONE BOT PER RUNG, COLLISION-FREE, ALWAYS — every
        manager on the host first, then every worker. Two single-manager
        fleets therefore have exactly one correct answer, and equality is the
        only assertion that says so.

        This test has now been tightened twice for one underlying reason, so
        the reason is recorded rather than the instances. Both loose drafts
        were written to a docstring that framed the property as an ORDERING —
        "boots no later than" — which is strictly weaker than what the ladder
        guarantees. Draft one asserted a collision shape the two-fleet
        arrangement cannot produce; draft two asserted ``a <= b``, which cannot
        tell a tie from correct ordering and so passed on a pre-fix tree where
        the two managers land on the SAME rung. Neither author departed from
        the docstring; the docstring was wrong, and an inequality is what let
        it stay wrong. An equality cannot be quietly loosened the same way.

        Rungs below are derived from the invariant, not fitted to output: 2
        managers then 2 workers, in host-walk order, at the 3s stagger.
        """
        root, local = self._host_manages_only(tmp_path)
        aaa, bbb = self._manages_only_fleets()
        p_aaa = Paths(root=root, fleet_dir=local / "aaa")
        p_bbb = Paths(root=root, fleet_dir=local / "bbb")
        rungs = {
            "aaa.b0": bot_boot_delay_s(aaa.bots["b0"], aaa, p_aaa),  # manager
            "bbb.b0": bot_boot_delay_s(bbb.bots["b0"], bbb, p_bbb),  # manager
            "aaa.b1": bot_boot_delay_s(aaa.bots["b1"], aaa, p_aaa),  # worker
            "bbb.b1": bot_boot_delay_s(bbb.bots["b1"], bbb, p_bbb),  # worker
        }
        assert rungs == {
            "aaa.b0": 0,
            "bbb.b0": 3,
            "aaa.b1": 6,
            "bbb.b1": 9,
        }, f"host ladder is not one-bot-per-rung managers-first: {rungs}"

    def test_unparseable_sibling_does_not_block_generate(self, tmp_path):
        """A broken fleet contributes no rungs; it must never raise here."""
        spec = {"aaa": (3, [0]), "bbb": (2, [0])}
        root, local = self._host(tmp_path, spec)
        (local / "aaa" / "fleet.yaml").write_text("fleet: [oops\n", encoding="utf-8")
        assert self._bases(root, local, spec, "bbb") == (0, 1)

    def test_manifest_that_is_not_a_mapping_contributes_nothing(self, tmp_path):
        """Shape is checked, not caught — a list manifest must not raise."""
        spec = {"aaa": (3, [0]), "bbb": (2, [0])}
        root, local = self._host(tmp_path, spec)
        (local / "aaa" / "fleet.yaml").write_text("- a\n- b\n", encoding="utf-8")
        assert self._bases(root, local, spec, "bbb") == (0, 1)

    def test_bot_boot_delay_places_each_bot_on_its_own_rung(self, tmp_path):
        """The derivation every compose entry point shares.

        Threading the rung from compose_fleet is what let `generate --bot` and
        move-bot write a unit with NO stagger at all (#1002), so the rung is
        derived from (tier, fleet position, position within tier) instead — and
        this asserts the derivation, not a re-implementation of it in the test
        body.
        """
        spec = {"aaa": (2, [0]), "bbb": (3, [0])}
        root, local = self._host(tmp_path, spec)
        paths = Paths(root=root, fleet_dir=local / "bbb")
        fleet = self._fleet("bbb", spec)
        # 2 managers (aaa.b0 rung 0, bbb.b0 rung 1); workers start at rung 2,
        # and aaa's one worker takes it — so bbb's workers are rungs 3-4.
        assert [bot_boot_delay_s(b, fleet, paths) for b in fleet.bots.values()] == [
            3,
            9,
            12,
        ]


class TestCrossFleetManagerRecognition:
    """manager_bots() must see a manager whose reports live in OTHER fleets.

    `teams:` can only name a manager of a team in its own fleet. A top-level
    coordinator whose reports are themselves managers of other fleets is named
    by no `teams:` block anywhere, so the predicate missed it — while the same
    manifest declared `manages: [...]` all along.
    """

    LIB_COMMON = Path(__file__).resolve().parents[1] / "lib" / "lib-common.sh"

    def _fleet(self, name="crog", **bots):
        """bots kwargs: name=(manages_list_or_None); teams built separately."""
        return FleetConfig(
            name=name,
            service_prefix="p",
            bots={
                b: BotConfig(bot_id=b, name=b, expertise=["eng"], manages=m)
                for b, m in bots.items()
            },
        )

    def _bash_is_manager(self, bot_dir: Path) -> bool:
        """The real shipped predicate, not a reimplementation of it."""
        proc = subprocess.run(
            [
                "bash",
                "-c",
                '. "$1"; bot_is_manager "$2"',
                "_",
                str(self.LIB_COMMON),
                str(bot_dir),
            ],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0

    def _compose_conf(self, tmp_path, fleet, bot_id):
        root = tmp_path / "claudlobby"
        (root / "runtime" / "bots" / bot_id).mkdir(parents=True)
        (root / "lib").mkdir(exist_ok=True)
        paths = Paths(root=root, fleet_dir=root)
        d = tmp_path / bot_id
        d.mkdir()
        (d / "bot.conf").write_text(
            compose_bot_conf(fleet.bots[bot_id], fleet, paths), encoding="utf-8"
        )
        return d

    # -- the predicate itself ------------------------------------------------

    def test_manages_makes_a_manager_with_no_team_at_all(self):
        """The clog shape: zero teams declared, manages: [...] present."""
        fleet = self._fleet(clog=["ari", "kev"])
        assert fleet.manager_bots() == {"clog"}

    def test_manages_targets_outside_this_fleet_still_count(self):
        """The whole point — the reports are in other fleets.

        _validate_teams warns rather than errors on exactly this, because
        "bot_ids may reference other fleets". ari/kev are absent from
        fleet.bots here, and clog is still a manager.
        """
        fleet = self._fleet(clog=["ari", "kev"])
        assert "ari" not in fleet.bots and "kev" not in fleet.bots
        assert "clog" in fleet.manager_bots()

    def test_a_plain_worker_is_still_not_a_manager(self):
        """CONTROL. A predicate that answers True for everything is not a fix."""
        fleet = self._fleet(name="tl", todd=None)
        assert fleet.manager_bots() == set()

    def test_the_predicate_is_not_unanimous_on_a_mixed_fleet(self):
        """The unanimity tell, asserted directly rather than hoped for."""
        fleet = self._fleet(name="mixed", clog=["ari"], todd=None, greg=None)
        assert fleet.manager_bots() == {"clog"}  # exactly one, not all three

    def test_empty_manages_is_not_a_claim_to_manage_anyone(self):
        assert self._fleet(z=[]).manager_bots() == set()
        assert self._fleet(z=None).manager_bots() == set()

    def test_teams_only_managers_are_untouched(self):
        """No regression for the ordinary within-fleet case."""
        fleet = self._fleet(name="eng", ari=None, alex=None)
        fleet.teams = {"personal": TeamConfig(name="personal", manager="ari")}
        assert fleet.manager_bots() == {"ari"}

    # -- outcome, through real composition and the real bash predicate -------

    def test_bash_bot_is_manager_is_true_for_a_manages_only_manager(self, tmp_path):
        """End to end: no teams block anywhere, and bot_is_manager still says yes.

        This is the assertion that lets the `teams: estate: manager: clog`
        workaround be deleted rather than merely tolerated.
        """
        fleet = self._fleet(clog=["ari", "kev"])
        assert fleet.teams == {}  # the workaround block is genuinely absent
        d = self._compose_conf(tmp_path, fleet, "clog")
        assert "MANAGER_TMUX" in (d / "bot.conf").read_text()
        assert self._bash_is_manager(d) is True

    def test_bash_bot_is_manager_is_false_for_a_real_worker(self, tmp_path):
        """CONTROL, through the same composition path."""
        fleet = self._fleet(name="tl", kev=["todd"], todd=None)
        fleet.teams = {"tl": TeamConfig(name="tl", manager="kev", workers=["todd"])}
        d = self._compose_conf(tmp_path, fleet, "todd")
        assert self._bash_is_manager(d) is False


class TestPluginsBotConf:
    """compose_bot_conf emits CLAUDE_CODE_SYNC_PLUGIN_INSTALL when fleet has plugins."""

    def _compose(self, tmp_path, plugins=None, channels=None):
        from claudlobby.config import PluginsConfig

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle="w_bot"),
            channels=channels or [],
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

    def test_bot_conf_derives_channel_plugin_into_required(self, tmp_path):
        # G3: the plugin a bot's --channels flag pins must be restored on a cold
        # box even when the fleet did not list it in plugins.required.
        from claudlobby.config import PluginsConfig

        plugins = PluginsConfig(required=["claudna@Claudfather"])
        conf = self._compose(
            tmp_path, plugins=plugins, channels=["plugin:telegram@claudfather-plugins"]
        )
        assert (
            "export FLEET_PLUGINS_REQUIRED="
            "'claudna@Claudfather telegram@claudfather-plugins'" in conf
        )

    def test_bot_conf_channel_plugin_deduped(self, tmp_path):
        # A channel plugin already in plugins.required is not double-listed.
        from claudlobby.config import PluginsConfig

        plugins = PluginsConfig(
            required=["claudna@Claudfather", "telegram@claudfather-plugins"]
        )
        conf = self._compose(
            tmp_path, plugins=plugins, channels=["plugin:telegram@claudfather-plugins"]
        )
        line = next(ln for ln in conf.splitlines() if "FLEET_PLUGINS_REQUIRED" in ln)
        assert line.count("telegram@claudfather-plugins") == 1

    def test_channel_plugins_parses_marketplace_pinned_refs(self):
        from claudlobby.composer import _channel_plugins

        assert _channel_plugins(["plugin:telegram@claudfather-plugins"]) == [
            "telegram@claudfather-plugins"
        ]
        # non-plugin / unpinned channels skipped; order + dedup preserved
        assert _channel_plugins(
            ["plugin:telegram@mp", "slack", "plugin:telegram@mp", "plugin:nomarket"]
        ) == ["telegram@mp"]


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


class TestChannelPluginInstallEnableCarveout:
    """G3/G5 contract: a channel plugin is INSTALLED but not ambiently ENABLED.

    enabledPlugins is the equipped NON-channel set (fleet.plugins.required =
    claudna + superpowers + additional). A channel plugin (derived from
    ``channels``) is unioned into FLEET_PLUGINS_REQUIRED so a cold box installs
    it, then activated by the ``--channels`` flag — NOT by ambient settings.local
    enablement. This locks PR #646's ratified carve-out against future drift.
    """

    def test_channel_plugin_installed_but_not_enabled(self, tmp_path):
        from claudlobby.config import PluginsConfig

        root = tmp_path / "claudlobby"
        (root / "runtime" / "bots" / "worker").mkdir(parents=True)
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle="w_bot"),
            channels=["plugin:telegram@claudfather-plugins"],
        )
        plugins = PluginsConfig(
            required=[
                "claudna@Claudfather",
                "superpowers@claude-plugins-official",
            ]
        )
        fleet = FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            telegram_group_chat_id="-100999",
            bots={"worker": bot},
            plugins=plugins,
        )

        conf = compose_bot_conf(bot, fleet, paths)
        settings = compose_settings_local(bot, fleet, paths)

        # Install side: the channel plugin the --channels flag pins is unioned into
        # FLEET_PLUGINS_REQUIRED alongside the equipped plugins so a cold box
        # installs it.
        required_line = next(
            ln for ln in conf.splitlines() if "FLEET_PLUGINS_REQUIRED" in ln
        )
        assert "telegram@claudfather-plugins" in required_line
        assert "claudna@Claudfather" in required_line
        assert "superpowers@claude-plugins-official" in required_line

        # Enable side: enabledPlugins is the equipped NON-channel set only. The
        # channel plugin must NOT be ambiently enabled (it is activated by
        # --channels, not settings.local).
        enabled = settings["enabledPlugins"]
        assert "telegram@claudfather-plugins" not in enabled
        assert enabled == {
            "claudna@Claudfather": True,
            "superpowers@claude-plugins-official": True,
        }


class TestComposeBotEventsDir:
    """compose_bot creates data/events/ directory."""

    def test_events_dir_created(self, tmp_path):

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
        # After system defaults merge, fields are populated.
        # Simulate by passing explicit values matching system.yaml defaults.
        from claudlobby.config import ObservabilityConfig

        obs = ObservabilityConfig(
            pulse_interval=300,
            activity_stuck_threshold=1800,
            dispatch_deadline=1800,
        )
        conf = self._compose(tmp_path, observability=obs)
        assert "export OBSERVABILITY_PULSE_INTERVAL=300" in conf
        assert "OBSERVABILITY_REAP_DAYS" not in conf          # the knob is retired (F18 closure): never composed
        assert "export OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD=1800" in conf
        assert "export OBSERVABILITY_DISPATCH_DEADLINE=1800" in conf

    def test_custom_observability_values(self, tmp_path):
        from claudlobby.config import ObservabilityConfig

        obs = ObservabilityConfig(
            pulse_interval=60,
            activity_stuck_threshold=600,
            dispatch_deadline=900,
        )
        conf = self._compose(tmp_path, observability=obs)
        assert "export OBSERVABILITY_PULSE_INTERVAL=60" in conf
        assert "export OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD=600" in conf
        assert "export OBSERVABILITY_DISPATCH_DEADLINE=900" in conf

    def test_observability_section_header(self, tmp_path):
        from claudlobby.config import ObservabilityConfig

        obs = ObservabilityConfig(pulse_interval=300)
        conf = self._compose(tmp_path, observability=obs)
        assert "# Observability" in conf

    def test_a_deadline_is_composed_even_when_the_fleet_declares_none(self, tmp_path):
        """M1 (#1481): an unset `dispatch_deadline` used to compose NOTHING —
        with the system-defaults tier off, the one fact the overdue watchdog
        reads depended on a bash literal nobody could see from fleet.yaml. The
        ruled default is 24h, and it is composed in SECONDS: 1440 is 24h in
        MINUTES, and composing that number would make every dispatch overdue
        in 24 minutes fleet-wide."""
        from claudlobby.composer import DEFAULT_DISPATCH_DEADLINE_S

        conf = self._compose(tmp_path)                 # no observability at all
        assert DEFAULT_DISPATCH_DEADLINE_S == 86_400   # 24h, the ruling, in seconds
        assert "# Observability" in conf
        assert "export OBSERVABILITY_DISPATCH_DEADLINE=86400" in conf
        # ...and nothing else is invented for a fleet that declared nothing
        assert "OBSERVABILITY_PULSE_INTERVAL" not in conf
        assert "OBSERVABILITY_BRIDGE_HEAL" not in conf

    def test_a_zero_deadline_composes_as_zero_and_disables_the_clock(self, tmp_path):
        """`0` is the open-ended dispatch, not "now": it must reach bot.conf
        as 0 so the door withholds `expected_by` entirely."""
        from claudlobby.config import ObservabilityConfig

        conf = self._compose(tmp_path, observability=ObservabilityConfig(dispatch_deadline=0))
        assert "export OBSERVABILITY_DISPATCH_DEADLINE=0" in conf

    def test_bridge_heal_emits_shell_1_not_python_true(self, tmp_path):
        """keepalive.sh:130 gates on the string '1'; a bool True rendered via the
        int-field pattern (_shq) would emit 'True' and silently recreate the
        cycle-1 no-op. It must land as 1."""
        from claudlobby.config import ObservabilityConfig

        obs = ObservabilityConfig(bridge_heal=True, bridge_heal_max_attempts=3)
        conf = self._compose(tmp_path, observability=obs)
        assert "export OBSERVABILITY_BRIDGE_HEAL=1" in conf
        assert "OBSERVABILITY_BRIDGE_HEAL=True" not in conf
        assert "export BRIDGE_HEAL_MAX_ATTEMPTS=3" in conf

    def test_bridge_heal_emits_0_when_false(self, tmp_path):
        from claudlobby.config import ObservabilityConfig

        obs = ObservabilityConfig(bridge_heal=False)
        conf = self._compose(tmp_path, observability=obs)
        assert "export OBSERVABILITY_BRIDGE_HEAL=0" in conf
        assert "OBSERVABILITY_BRIDGE_HEAL=False" not in conf

    def test_bridge_heal_absent_when_none(self, tmp_path):
        from claudlobby.config import ObservabilityConfig

        obs = ObservabilityConfig(bridge_heal=None)
        conf = self._compose(tmp_path, observability=obs)
        assert "OBSERVABILITY_BRIDGE_HEAL" not in conf

    def test_bridge_heal_alone_emits_observability_section(self, tmp_path):
        """The section guard must include bridge_heal so the block emits even when
        the four int fields are all None."""
        from claudlobby.config import ObservabilityConfig

        obs = ObservabilityConfig(bridge_heal=True)
        conf = self._compose(tmp_path, observability=obs)
        assert "# Observability" in conf
        assert "export OBSERVABILITY_BRIDGE_HEAL=1" in conf


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
        install_real_template(root)
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
        from jinja2.sandbox import SecurityError
        from claudlobby.composer import _render_startup_prompt

        bot = BotConfig(bot_id="evil", name="evil", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p")
        prompt = "{{ ''.__class__.__mro__[1].__subclasses__() }}"
        with pytest.raises(SecurityError):
            _render_startup_prompt(prompt, bot, fleet)

    def test_render_startup_prompt_blocks_subclass_walk(self):
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

        with pytest.raises(ValueError, match="not a valid shell identifier"):
            self._compose(tmp_path, env={"FOO;ls": "x"})

    def test_invalid_env_key_with_space_raises(self, tmp_path):

        with pytest.raises(ValueError, match="not a valid shell identifier"):
            self._compose(tmp_path, env={"FOO BAR": "x"})

    def test_unsafe_bot_id_raises(self, tmp_path):

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
        install_real_template(root)
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
                target_repo="example-org/dbt",
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
        assert "example-org/dbt" in result
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
    """resolve_effective_integrations: explicit list unioned with auto-paired mcp names; auto-derived from MCP when empty."""

    def _paths_with_integrations(self, tmp_path: Path, names: list[str]) -> Paths:
        root = tmp_path / "claudlobby"
        root.mkdir()
        int_dir = root / "library" / "integrations"
        int_dir.mkdir(parents=True)
        for name in names:
            (int_dir / f"{name}.md").write_text(f"# {name}\n")
        return Paths(root=root, fleet_dir=root)

    def test_explicit_integrations_union_mcp_paired(self, tmp_path):
        """Explicit integrations are unioned with auto-paired mcp names, not replaced.

        The old verbatim return would drop ``github`` here, stripping its grants.
        """
        from claudlobby.composer import resolve_effective_integrations

        paths = self._paths_with_integrations(tmp_path, ["github", "neon"])
        bot = BotConfig(
            bot_id="w",
            name="w",
            expertise=["eng"],
            integrations=["neon"],  # explicit; github implied by mcp
            mcp=[McpEntry(name="github"), McpEntry(name="neon")],
        )
        # neon (explicit) first; github auto-paired and appended; neon not duplicated
        assert resolve_effective_integrations(bot, paths) == ["neon", "github"]

    def test_auto_derived_from_mcp_when_empty(self, tmp_path):
        from claudlobby.composer import resolve_effective_integrations

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

        # No integration docs exist at all
        paths = self._paths_with_integrations(tmp_path, [])
        bot = BotConfig(
            bot_id="w",
            name="w",
            expertise=["eng"],
            mcp=[McpEntry(name="github")],
        )
        assert resolve_effective_integrations(bot, paths) == []


class TestPermissionMode:
    """compose_bot_conf handles permission_mode vs dangerously_skip_permissions."""

    def _compose(
        self, tmp_path, permission_mode=None, dangerously_skip_permissions=False
    ):

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle="w_bot"),
            permission_mode=permission_mode,
            dangerously_skip_permissions=dangerously_skip_permissions,
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

    def test_permission_mode_auto(self, tmp_path):
        conf = self._compose(tmp_path, permission_mode="auto")
        assert "--permission-mode auto" in conf
        assert "--dangerously-skip-permissions" not in conf

    def test_permission_mode_bypass(self, tmp_path):
        conf = self._compose(tmp_path, permission_mode="bypassPermissions")
        assert "--permission-mode bypassPermissions" in conf
        assert "--dangerously-skip-permissions" not in conf

    def test_explicit_dangerously_skip_opt_in(self, tmp_path):
        # dangerously-skip stays available as an EXPLICIT opt-in: set it true
        # (with no permission_mode) and the dangerous flag is still emitted.
        conf = self._compose(
            tmp_path, permission_mode=None, dangerously_skip_permissions=True
        )
        assert "--dangerously-skip-permissions" in conf
        assert "--permission-mode" not in conf

    def test_default_is_acceptedits(self, tmp_path):
        # A bot that configures neither field gets the conservative default:
        # --permission-mode acceptEdits (headless-safe, respects allow/deny lists),
        # NOT --dangerously-skip-permissions.
        conf = self._compose(tmp_path)
        assert "--permission-mode acceptEdits" in conf
        assert "--dangerously-skip-permissions" not in conf

    def test_permission_mode_wins_when_both_set(self, tmp_path):
        # Behavior 2: an explicit permission_mode takes precedence over an explicit
        # dangerously_skip_permissions when BOTH are set. Kills the if/elif
        # order-swap mutation — every other permission_mode test leaves
        # dangerously_skip at the default False, so only this one sets both.
        conf = self._compose(
            tmp_path, permission_mode="plan", dangerously_skip_permissions=True
        )
        assert "--permission-mode plan" in conf
        assert "--dangerously-skip-permissions" not in conf

    def test_invalid_permission_mode_raises(self):
        from claudlobby.config import _parse_enum
        from claudlobby.known_values import VALID_PERMISSION_MODES

        with pytest.raises(ValueError, match="Invalid permission_mode"):
            _parse_enum("permission_mode", "yolo", VALID_PERMISSION_MODES)


class TestComposeBotConfHandleValidation:
    """compose_bot_conf validates telegram.handle against _TELEGRAM_HANDLE_RE."""

    def _compose(self, tmp_path, handle):

        bot = _make_bot(handle=handle)
        root = tmp_path / "claudlobby"
        (root / "runtime" / "bots" / bot.bot_id).mkdir(parents=True, exist_ok=True)
        (root / "lib").mkdir(exist_ok=True)
        return compose_bot_conf(bot, _make_fleet(), _make_paths(root))

    def test_valid_handles_accepted(self, tmp_path):
        # "1bot" (leading digit) is intentionally accepted by the canonical rule.
        for handle in ("w_bot", "Alex", "a-b", "x1", "1bot"):
            conf = self._compose(tmp_path, handle)
            assert f"export TELEGRAM_BOT_HANDLE={handle}" in conf

    def test_invalid_handles_raise(self, tmp_path):

        for handle in ("-x", "a b", "a.b"):
            with pytest.raises(ValueError, match="telegram handle"):
                self._compose(tmp_path, handle)


def test_compose_hooks_type_hints_resolve():
    """_compose_hooks' typing.Any annotations must resolve via get_type_hints."""
    import typing

    hints = typing.get_type_hints(_compose_hooks)
    assert "hooks" in hints


def _write_integration(
    root: Path,
    name: str,
    *,
    tool_grants: list[str] | None = None,
    type_: str | None = None,
    body: str = "doc",
) -> None:
    """Write a minimal library/integrations/<name>.md with optional tool_grants."""
    d = root / "library" / "integrations"
    d.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"title: {name}"]
    if type_:
        fm.append(f"type: {type_}")
    if tool_grants is not None:
        fm.append("tool_grants:")
        fm.extend(f'  - "{g}"' for g in tool_grants)
    fm.append("---")
    (d / f"{name}.md").write_text("\n".join(fm) + f"\n# {name}\n\n{body}\n")


class TestResolveEffectiveIntegrationsUnion:
    """resolve_effective_integrations unions explicit integrations with auto-paired mcp names.

    The pre-fix behavior returned bot.integrations verbatim, which stripped mcp-derived
    grants the moment a bot set integrations: explicitly (the cycle-1 F2 blocker).
    """

    def _setup(self, tmp_path: Path, integration_names: list[str]) -> Paths:
        root = tmp_path / "claudlobby"
        (root / "runtime" / "bots").mkdir(parents=True)
        for n in integration_names:
            _write_integration(root, n)
        return Paths(root=root, fleet_dir=root)

    def test_explicit_integrations_union_auto_paired_mcp(self, tmp_path):
        """A bot with explicit integrations AND an auto-pairable mcp gets BOTH."""
        from claudlobby.composer import resolve_effective_integrations

        paths = self._setup(tmp_path, ["neon", "github"])
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            integrations=["neon"],
            mcp=[McpEntry(name="github")],
        )
        eff = resolve_effective_integrations(bot, paths)
        assert "neon" in eff  # explicit integration kept
        assert "github" in eff  # auto-paired mcp unioned in (the fix)

    def test_only_mcp_auto_pairs(self, tmp_path):
        """No explicit integrations → auto-pair from mcp (unchanged behavior)."""
        from claudlobby.composer import resolve_effective_integrations

        paths = self._setup(tmp_path, ["github", "notion"])
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            mcp=[McpEntry(name="github"), McpEntry(name="notion")],
        )
        assert sorted(resolve_effective_integrations(bot, paths)) == [
            "github",
            "notion",
        ]

    def test_dedup_when_explicit_and_mcp_paired(self, tmp_path):
        """An integration both listed explicitly and mcp-paired appears once."""
        from claudlobby.composer import resolve_effective_integrations

        paths = self._setup(tmp_path, ["github"])
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            integrations=["github"],
            mcp=[McpEntry(name="github")],
        )
        assert resolve_effective_integrations(bot, paths) == ["github"]

    def test_explicit_order_preserved_mcp_appended(self, tmp_path):
        """Explicit integrations come first in order; mcp-paired names append after."""
        from claudlobby.composer import resolve_effective_integrations

        paths = self._setup(tmp_path, ["neon", "vercel", "github"])
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            integrations=["neon", "vercel"],
            mcp=[McpEntry(name="github")],
        )
        assert resolve_effective_integrations(bot, paths) == [
            "neon",
            "vercel",
            "github",
        ]

    def test_mcp_without_integration_file_not_paired(self, tmp_path):
        """An mcp entry with no integrations/<name>.md is not auto-paired."""
        from claudlobby.composer import resolve_effective_integrations

        paths = self._setup(tmp_path, ["neon"])  # no github integration file
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            integrations=["neon"],
            mcp=[McpEntry(name="github")],
        )
        assert resolve_effective_integrations(bot, paths) == ["neon"]


class TestResolveIntegrationGrants:
    """_resolve_integration_grants resolves tool_grants across effective integrations.

    Three emergent shapes from one rule (a fragment-backed integration shares its
    name with its mcp server):
      - fragment-backed with a matching bot.mcp entry → the ``mcp__<name>__``
        prefix is instance-expanded, reproducing _resolve_mcp_permissions output;
      - fragment-backed equipped WITHOUT a matching mcp entry → grant emitted
        literally (the default ``mcp__<name>__*``; validator warns);
      - connector-backed (no matching mcp entry) → literal ``mcp__claude_ai_*``;
      - CLI-backed (no tool_grants) → nothing.
    """

    def _setup(self, tmp_path: Path, integrations: dict[str, dict]) -> Paths:
        root = tmp_path / "claudlobby"
        (root / "runtime" / "bots").mkdir(parents=True)
        for name, spec in integrations.items():
            _write_integration(
                root,
                name,
                tool_grants=spec.get("tool_grants"),
                type_=spec.get("type_"),
            )
        return Paths(root=root, fleet_dir=root)

    def test_fragment_backed_default_instance(self, tmp_path):
        from claudlobby.composer import _resolve_integration_grants

        paths = self._setup(tmp_path, {"github": {"tool_grants": ["mcp__github__*"]}})
        bot = BotConfig(
            bot_id="w", name="w", expertise=["eng"], mcp=[McpEntry(name="github")]
        )
        assert _resolve_integration_grants(bot, paths) == ["mcp__github__*"]

    def test_fragment_backed_multi_instance_expands(self, tmp_path):
        """Instance-scoped fragment servers rewrite the prefix per instance."""
        from claudlobby.composer import _resolve_integration_grants

        paths = self._setup(tmp_path, {"gws": {"tool_grants": ["mcp__gws__*"]}})
        bot = BotConfig(
            bot_id="w",
            name="w",
            expertise=["eng"],
            mcp=[McpEntry(name="gws", instances=["personal", "work"])],
        )
        assert _resolve_integration_grants(bot, paths) == [
            "mcp__gws-personal__*",
            "mcp__gws-work__*",
        ]

    def test_connector_grant_literal(self, tmp_path):
        """A connector equipped via integrations: (no mcp entry) emits its grant literally."""
        from claudlobby.composer import _resolve_integration_grants

        paths = self._setup(
            tmp_path,
            {
                "gmail": {
                    "tool_grants": ["mcp__claude_ai_Gmail__*"],
                    "type_": "connector",
                }
            },
        )
        bot = BotConfig(
            bot_id="w", name="w", expertise=["eng"], integrations=["gmail"], mcp=[]
        )
        assert _resolve_integration_grants(bot, paths) == ["mcp__claude_ai_Gmail__*"]

    def test_cli_backed_no_grants(self, tmp_path):
        """A CLI integration carries no tool_grants → no grants."""
        from claudlobby.composer import _resolve_integration_grants

        paths = self._setup(tmp_path, {"neon": {"type_": "cli"}})
        bot = BotConfig(
            bot_id="w", name="w", expertise=["eng"], integrations=["neon"], mcp=[]
        )
        assert _resolve_integration_grants(bot, paths) == []

    def test_fragment_backed_without_mcp_entry_emits_default(self, tmp_path):
        """Fragment-backed integration equipped without a matching mcp entry → literal default."""
        from claudlobby.composer import _resolve_integration_grants

        paths = self._setup(tmp_path, {"github": {"tool_grants": ["mcp__github__*"]}})
        bot = BotConfig(
            bot_id="w", name="w", expertise=["eng"], integrations=["github"], mcp=[]
        )
        assert _resolve_integration_grants(bot, paths) == ["mcp__github__*"]


class TestGrantSupersetGate:
    """_assert_grant_superset hard-fails when new tool_grants would drop a legacy grant."""

    def test_gate_raises_when_new_drops_legacy(self):

        from claudlobby.composer import _assert_grant_superset

        with pytest.raises(ValueError, match="mcp__github__"):
            _assert_grant_superset("worker", ["mcp__github__*"], [])

    def test_gate_passes_when_new_is_superset(self):
        from claudlobby.composer import _assert_grant_superset

        # new adds a connector grant on top of legacy — superset, no raise
        _assert_grant_superset(
            "worker",
            ["mcp__github__*"],
            ["mcp__github__*", "mcp__claude_ai_Gmail__*"],
        )

    def test_gate_is_set_comparison_not_textual_order(self):
        from claudlobby.composer import _assert_grant_superset

        _assert_grant_superset(
            "worker",
            ["mcp__github__*", "mcp__notion__*"],
            ["mcp__notion__*", "mcp__github__*"],
        )


class TestGrantUnionInSettingsLocal:
    """compose_settings_local emits the union of legacy + integration grants, gated."""

    def _setup(
        self, tmp_path: Path, fragments: dict[str, dict], integrations: dict[str, dict]
    ) -> Paths:
        root = tmp_path / "claudlobby"
        (root / "runtime" / "bots").mkdir(parents=True)
        mcp_dir = root / "library" / "mcp"
        mcp_dir.mkdir(parents=True)
        for name, content in fragments.items():
            (mcp_dir / f"{name}.json").write_text(json.dumps(content))
        for name, spec in integrations.items():
            _write_integration(
                root,
                name,
                tool_grants=spec.get("tool_grants"),
                type_=spec.get("type_"),
            )
        return Paths(root=root, fleet_dir=root)

    _GH_FRAGMENT = {
        "github": {
            "_permissions_contract": {"tools": ["search_code"]},
            "github": {"command": "npx", "args": []},
        }
    }

    def test_fragment_backed_grant_not_duplicated(self, tmp_path):
        """Legacy and new both emit mcp__github__* → union dedups to one entry."""

        paths = self._setup(
            tmp_path, self._GH_FRAGMENT, {"github": {"tool_grants": ["mcp__github__*"]}}
        )
        bot = BotConfig(
            bot_id="w", name="w", expertise=["eng"], mcp=[McpEntry(name="github")]
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
        allow = compose_settings_local(bot, fleet, paths)["permissions"]["allow"]
        assert allow.count("mcp__github__*") == 1

    def test_connector_grant_added_alongside_legacy(self, tmp_path):
        """Equipping a connector integration adds its literal grant; legacy grant retained."""

        paths = self._setup(
            tmp_path,
            self._GH_FRAGMENT,
            {
                "github": {"tool_grants": ["mcp__github__*"]},
                "gmail": {
                    "tool_grants": ["mcp__claude_ai_Gmail__*"],
                    "type_": "connector",
                },
            },
        )
        bot = BotConfig(
            bot_id="w",
            name="w",
            expertise=["eng"],
            integrations=["gmail"],
            mcp=[McpEntry(name="github")],
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
        allow = compose_settings_local(bot, fleet, paths)["permissions"]["allow"]
        assert "mcp__github__*" in allow  # legacy grant retained (union)
        assert "mcp__claude_ai_Gmail__*" in allow  # connector addition

    def test_compose_hard_fails_when_fragment_grant_uncovered(self, tmp_path):
        """A contract fragment with no paired integration tool_grants trips the gate end-to-end."""

        from claudlobby.config import McpEntry

        # fragment has a live grant, but NO integration file carries covering tool_grants
        paths = self._setup(tmp_path, self._GH_FRAGMENT, {})
        bot = BotConfig(
            bot_id="w", name="w", expertise=["eng"], mcp=[McpEntry(name="github")]
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
        with pytest.raises(ValueError, match="mcp__github__"):
            compose_settings_local(bot, fleet, paths)


# ── #702 L1 source-guard wiring (composer choke-sites) ────────────────


class TestSourceGuardWiring:
    """The L1 deny-by-default guard wired into every composer choke-site.

    The grammar itself is proven in test_source_audit.py; these prove the
    composer actually calls it at generate time — compose_bot before any write,
    the grant / timer / bot.conf-emission sites, and compose_fleet's aggregation.
    """

    def test_compose_bot_denies_foreign_env_before_any_write(self, fleet_dir):
        # A foreign absolute in bot.env raises — and, because the guard fires
        # before the first mkdir, leaves no partial wiring behind.
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        bot.env = {"GA4_KEY": "/Users/x/ga4.json"}
        with pytest.raises(ValueError, match="/Users/x/ga4.json"):
            compose_bot(bot, fleet, paths)
        assert not (paths.bot_runtime("lead") / "bot.conf").exists()

    def test_compose_bot_denies_foreign_mcp_fragment_arg(self, fleet_dir):
        # A planted MCP fragment carrying an absolute arg is denied via the
        # fragment choke — the composer loads it and hands it to the guard.
        frag = {"srv": {"command": "node", "args": ["/opt/evil/index.js"]}}
        (fleet_dir / "library" / "mcp" / "evil.json").write_text(json.dumps(frag))
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        bot.mcp = [McpEntry(name="evil")]
        with pytest.raises(ValueError, match="/opt/evil/index.js"):
            compose_bot(bot, fleet, paths)

    def test_compose_bot_clean_when_anchored(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        bot.env = {"P": "${FLEET_ROOT}/mcp/x.py"}
        compose_bot(bot, fleet, paths)  # no raise
        assert (paths.bot_runtime("lead") / "bot.conf").is_file()

    def test_r1_anchor_headed_env_emits_double_quoted(self, tmp_path):
        # R1: an anchor-headed value emits DOUBLE-quoted so the shell expands the
        # composer anchor at source time; a plain ${VAR} stays single-quoted
        # (frozen) via _shq.
        root = tmp_path / "claudlobby"
        root.mkdir()
        paths = _make_paths(root)
        bot = BotConfig(
            bot_id="b",
            name="b",
            expertise=["eng"],
            telegram=TelegramConfig(handle="b_bot"),
            env={"ANCHORED": "${FLEET_ROOT}/mcp/x.py", "PLAINVAR": "${GITHUB_PAT}"},
        )
        fleet = FleetConfig(name="t", service_prefix="com.t")
        conf = compose_bot_conf(bot, fleet, paths)
        assert 'export ANCHORED="${FLEET_ROOT}/mcp/x.py"' in conf
        assert "export PLAINVAR='${GITHUB_PAT}'" in conf

    def test_r1_anchor_headed_env_injection_neutralized_at_emission(self, tmp_path):
        # Defense-in-depth (#731): even bypassing the source guard (a direct
        # compose_bot_conf, which the guard-fronted compose_bot never reaches for
        # a denied value), an anchor-headed value whose tail carries a shell
        # metacharacter must NOT emit double-quoted-raw where sourcing bot.conf
        # would execute it — it falls back to the frozen _shq sink.
        root = tmp_path / "claudlobby"
        root.mkdir()
        paths = _make_paths(root)
        bot = BotConfig(
            bot_id="b",
            name="b",
            expertise=["eng"],
            telegram=TelegramConfig(handle="b_bot"),
            env={"ANCHORED": "${FLEET_ROOT}/$(touch pwned)/x"},
        )
        fleet = FleetConfig(name="t", service_prefix="com.t")
        conf = compose_bot_conf(bot, fleet, paths)
        # never the injectable double-quoted emission
        assert 'export ANCHORED="${FLEET_ROOT}/$(touch pwned)/x"' not in conf
        # frozen literal instead — the $(...) cannot execute when sourced
        assert "export ANCHORED='${FLEET_ROOT}/$(touch pwned)/x'" in conf

    def test_grant_choke_denies_foreign_absolute_grant(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        bot.tool_permissions = ToolPermissionsConfig(allow=["Read(/Users/x/secret)"])
        with pytest.raises(ValueError, match="/Users/x/secret"):
            compose_settings_local(bot, fleet, paths)

    def test_grant_choke_passes_declared_absolute_grant(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        bot.tool_permissions = ToolPermissionsConfig(allow=["Bash(/opt/tool/bin *)"])
        bot.external_paths = [ExternalDecl(path="/opt/tool/**", purpose="tool tree")]
        compose_settings_local(bot, fleet, paths)  # declared → no raise

    def test_grant_choke_excludes_composer_sibling_isolation(self, fleet_dir):
        # Deny grants aren't classified (they restrict, not wire-to-a-path), so the
        # Layer-0 sibling-isolation denies — which embed a composer-DERIVED absolute
        # (the sibling's runtime dir) — never trip the allow-only guard, on any
        # multi-bot fleet. The deny is still emitted.
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]  # sibling worker-1 → Read(/abs/.../worker-1/**)
        settings = compose_settings_local(bot, fleet, paths)  # must not raise
        deny = settings["permissions"]["deny"]
        assert any("worker-1" in d for d in deny)

    def test_grant_choke_ignores_foreign_deny_grant(self, fleet_dir):
        # A foreign absolute in a DENY grant is a restriction, not a self-containment
        # violation — allow-only, so it is deliberately not flagged.
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        bot.tool_permissions = ToolPermissionsConfig(deny=["Read(/Users/x/secret)"])
        compose_settings_local(bot, fleet, paths)  # deny not classified → no raise

    def test_timer_choke_denies_raw_absolute_script(self, tmp_path):
        root = tmp_path / "claudlobby"
        root.mkdir()
        paths = _make_paths(root)
        timers_dir = tmp_path / "timers"
        timers_dir.mkdir()
        with pytest.raises(ValueError, match="/opt/rogue/job.sh"):
            _write_timer_units(
                timers_dir,
                "com.t.job",
                "job",
                {"type": "calendar", "expression": "daily"},
                "/opt/rogue/job.sh",
                "oneshot",
                "t",
                paths,
            )

    def test_timer_choke_passes_anchored_script(self, tmp_path):
        root = tmp_path / "claudlobby"
        root.mkdir()
        paths = _make_paths(root)
        timers_dir = tmp_path / "timers"
        timers_dir.mkdir()
        _write_timer_units(
            timers_dir,
            "com.t.job",
            "job",
            {"type": "calendar", "expression": "daily"},
            "$CLAUDLOBBY_ROOT/lib/job.sh",
            "oneshot",
            "t",
            paths,
        )  # anchor-headed → no raise
        assert (timers_dir / "com.t.job.service").is_file()

    def test_compose_fleet_aggregates_multiple_bot_failures(self, fleet_dir):
        # G1: one generate surfaces EVERY offender, not just the first bot.
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        fleet.bots["lead"].env = {"A": "/Users/x/a.json"}
        fleet.bots["worker-1"].env = {"B": "/opt/y/b.json"}
        with pytest.raises(ValueError) as ei:
            compose_fleet(fleet, paths)
        msg = str(ei.value)
        assert "2 bot(s) failed" in msg
        assert "lead" in msg and "worker-1" in msg
        assert "/Users/x/a.json" in msg and "/opt/y/b.json" in msg


class TestTimerUnitPath:
    """#798 — composed timer units must carry a PATH that resolves fleet tools.

    systemd and launchd start jobs with a minimal PATH (roughly ``/usr/bin:/bin``)
    that excludes Homebrew and the per-user npm/bun/.local bins where ``claude``
    and ``claudlobby`` live, so a timer such as reload-fleet's
    ``claude plugin update`` dies with 'command not found' and fires a false
    ``reload_failed`` alert. The composer mirrors lib/start-bot.sh:49's PATH
    construction into every emitted timer unit, identically on both platforms
    (launchd/systemd parity, #708).
    """

    # The always-present segments (system bins + per-user bins). The Homebrew
    # segment is host/OS-dependent, so it is deliberately not asserted here.
    def _required_segments(self) -> list[str]:
        home = str(Path.home())
        return [
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            f"{home}/.local/bin",
            f"{home}/.bun/bin",
            f"{home}/.npm-global/bin",
        ]

    def _emit(self, tmp_path: Path) -> Path:
        root = tmp_path / "claudlobby"
        root.mkdir()
        paths = _make_paths(root)
        timers_dir = tmp_path / "timers"
        timers_dir.mkdir()
        _write_timer_units(
            timers_dir,
            "com.t.reload-fleet",
            "reload-fleet",
            {"type": "calendar", "expression": "*-*-* 03:30:00"},
            "$CLAUDLOBBY_ROOT/lib/reload-fleet.sh",
            "oneshot",
            "t",
            paths,
        )
        return timers_dir

    @staticmethod
    def _systemd_path(service_text: str) -> str:
        lines = [
            ln for ln in service_text.splitlines() if ln.startswith("Environment=PATH=")
        ]
        assert len(lines) == 1, f"expected one Environment=PATH= line, got {lines}"
        return lines[0].split("=", 2)[2]

    @staticmethod
    def _launchd_path(plist_text: str) -> str:
        # Parse as a real plist so PATH is asserted to live *inside* the
        # EnvironmentVariables dict — a stray top-level PATH would not appear here.
        return plistlib.loads(plist_text.encode())["EnvironmentVariables"]["PATH"]

    def test_systemd_service_sets_path(self, tmp_path):
        svc = (self._emit(tmp_path) / "com.t.reload-fleet.service").read_text()
        segs = self._systemd_path(svc).split(":")
        for seg in self._required_segments():
            assert seg in segs, f"{seg} missing from systemd PATH: {segs}"

    def test_launchd_plist_sets_path_in_env_dict(self, tmp_path):
        plist = (self._emit(tmp_path) / "com.t.reload-fleet.plist").read_text()
        segs = self._launchd_path(plist).split(":")
        for seg in self._required_segments():
            assert seg in segs, f"{seg} missing from launchd PATH: {segs}"

    def test_launchd_systemd_path_parity(self, tmp_path):
        d = self._emit(tmp_path)
        svc_path = self._systemd_path((d / "com.t.reload-fleet.service").read_text())
        plist_path = self._launchd_path((d / "com.t.reload-fleet.plist").read_text())
        assert svc_path == plist_path, (
            f"launchd/systemd PATH diverge:\n  systemd={svc_path}\n  launchd={plist_path}"
        )

    def test_path_includes_the_repo_venv_bin(self, tmp_path):
        """#805 — the user-prefix segments resolve `claude`, but `claudlobby` is
        a console script that a repo-local venv puts on no system PATH at all.
        Without this segment a timer fixed for `claude` still died one line
        later on `claudlobby generate`. Asserted on both platforms."""
        d = self._emit(tmp_path)
        venv_bin = f"{tmp_path / 'claudlobby'}/.venv/bin"
        for unit, extract in (
            ("com.t.reload-fleet.service", self._systemd_path),
            ("com.t.reload-fleet.plist", self._launchd_path),
        ):
            segs = extract((d / unit).read_text()).split(":")
            assert venv_bin in segs, f"{venv_bin} missing from {unit} PATH: {segs}"

    def test_venv_bin_is_last_so_it_never_shadows_a_real_install(self, tmp_path):
        """The venv is a fallback for the console script, not a preference: a
        system/user install must keep winning."""
        d = self._emit(tmp_path)
        segs = self._systemd_path((d / "com.t.reload-fleet.service").read_text()).split(
            ":"
        )
        assert segs[-1] == f"{tmp_path / 'claudlobby'}/.venv/bin", segs


def _git_cred_bot(creds=None):
    """A bot declaring per-org git credentials (default: one org)."""
    return BotConfig(
        bot_id="kev",
        name="kev",
        expertise=["eng"],
        telegram=TelegramConfig(handle="kev_bot"),
        git_credentials={"OrgA": "ORG_A_PAT"} if creds is None else creds,
    )


_GH_STUB = "#!/bin/sh\necho username=x-access-token\necho password=HOST_DEFAULT\n"


class TestPerOrgGitCredentialRouting:
    """A bot pushing to two orgs needs two tokens (fine-grained PATs are
    single-resource-owner), and `gh auth setup-git` installs a host-wide helper
    for https://github.com that answers first for every org. THREE ordering
    properties make routing work; each is pinned separately because a reorder
    breaks routing SILENTLY — git takes the first helper that answers, and there
    is no most-specific-wins rule. `useHttpPath` is a fourth line but is not one
    of the three; see its own test for what it does and does not do.

    Spec: documentation/plans/2026-07-27-per-org-git-credential-routing.md
    """

    def _render(self, tmp_path, monkeypatch, creds=None):
        """Both host seams stubbed so the assertions are host-independent — and
        so a test can never invoke the real gh helper (which would answer with
        the operator's actual token and print it into pytest output)."""
        import claudlobby.composer as comp

        monkeypatch.setattr(comp, "_resolve_gh_executable", lambda: "/usr/bin/gh")
        monkeypatch.setattr(
            comp, "_operator_gitconfig", lambda: tmp_path / "user.gitconfig"
        )
        return comp.compose_bot_gitconfig(_git_cred_bot(creds), _make_paths(tmp_path))

    # --- the three load-bearing ordering properties -----------------------

    def test_use_http_path_is_set(self, tmp_path, monkeypatch):
        """Present deliberately, but NOT as the thing that makes an org section
        match — the path is in the credential context at match time regardless,
        because config is read (and URL-matched) before the !useHttpPath strip.
        That read-then-strip order is a git internal rather than a documented
        contract, so this line pins it; TestGitCredentialRoutingResolvesForReal
        pins both halves behaviourally."""
        assert "useHttpPath = true" in self._render(tmp_path, monkeypatch)

    def test_include_of_operator_config_comes_first(self, tmp_path, monkeypatch):
        """Preserves user.name/user.email — the git-identity-no-overrides
        guardrail forbids per-bot identity, so we extend global config."""
        out = self._render(tmp_path, monkeypatch)
        assert out.index("[include]") < out.index("useHttpPath")

    def test_reset_follows_the_include(self, tmp_path, monkeypatch):
        """The include drags in gh's own reset+helper pair; without re-resetting
        after it, gh's helper is first in the list and wins for every org."""
        out = self._render(tmp_path, monkeypatch)
        assert out.index("[include]") < out.index("\thelper =\n")

    def test_org_helper_precedes_the_generic_fallback(self, tmp_path, monkeypatch):
        """Ordering IS the selection mechanism."""
        out = self._render(tmp_path, monkeypatch)
        assert out.index('[credential "https://github.com/OrgA"]') < out.index(
            "auth git-credential"
        )

    # --- secret hygiene ---------------------------------------------------

    def test_env_var_is_referenced_never_expanded(self, tmp_path, monkeypatch):
        """The composed file is world-readable; it must carry the NAME so the
        token is read from the process env at push time."""
        out = self._render(tmp_path, monkeypatch)
        assert "password=$ORG_A_PAT" in out
        assert "github_pat_" not in out and "ghp_" not in out

    def test_username_is_the_documented_placeholder_not_a_real_login(
        self, tmp_path, monkeypatch
    ):
        """GitHub ignores the username for PAT auth; a real login would be PII
        and wrong for any other operator."""
        assert "username=x-access-token" in self._render(tmp_path, monkeypatch)

    # --- multi-org + opt-out ---------------------------------------------

    def test_each_declared_org_gets_its_own_section(self, tmp_path, monkeypatch):
        out = self._render(tmp_path, monkeypatch, {"OrgA": "A_PAT", "OrgB": "B_PAT"})
        assert '[credential "https://github.com/OrgA"]' in out
        assert '[credential "https://github.com/OrgB"]' in out
        assert "password=$A_PAT" in out and "password=$B_PAT" in out

    def test_no_credentials_declared_composes_nothing(self, tmp_path, monkeypatch):
        """Fleets declaring none must compose byte-identically to before."""
        assert self._render(tmp_path, monkeypatch, {}) is None

    def test_bot_conf_exports_git_config_global_only_when_declared(self, tmp_path):
        root = tmp_path / "claudlobby"
        (root / "runtime" / "bots" / "kev").mkdir(parents=True)
        (root / "lib").mkdir()
        paths = _make_paths(root)
        fleet = FleetConfig(name="tl", service_prefix="com.crog.tl")
        assert 'export GIT_CONFIG_GLOBAL="$BOT_DIR/.gitconfig"' in compose_bot_conf(
            _git_cred_bot(), fleet, paths
        )
        assert "GIT_CONFIG_GLOBAL" not in compose_bot_conf(
            _git_cred_bot({}), fleet, paths
        )


class TestGitCredentialRoutingResolvesForReal:
    """The class above pins ORDERING; this pins BEHAVIOUR by running the composed
    config through real `git credential fill`. No network, no real tokens — a stub
    helper stands in for gh, and the include points at a fixture.

    CI-runnable proxy for the control/treatment real push recorded in the spec
    (that needs two real tokens across two orgs)."""

    def _composed(
        self, tmp_path, monkeypatch, user_cfg=None, creds=None, gh_body=_GH_STUB
    ):
        import claudlobby.composer as comp

        gh_stub = tmp_path / "gh-stub"
        _write_exec(str(gh_stub), gh_body)
        if user_cfg is None:
            user_cfg = tmp_path / "user.gitconfig"
            user_cfg.write_text("[user]\n\temail = operator@example.com\n")
        monkeypatch.setattr(comp, "_resolve_gh_executable", lambda: str(gh_stub))
        monkeypatch.setattr(comp, "_operator_gitconfig", lambda: user_cfg)
        cfg = tmp_path / "composed.gitconfig"
        cfg.write_text(comp.compose_bot_gitconfig(_git_cred_bot(creds), _make_paths(tmp_path)))
        return cfg

    @staticmethod
    def _git(cfg, args, extra_env=None):
        return subprocess.run(
            ["git", *args],
            env=git_isolation_env(cfg, **(extra_env or {})),
            capture_output=True,
            text=True,
        )

    def _fill(self, cfg, path, extra_env):
        r = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost=github.com\npath={path}\n\n",
            env=git_isolation_env(cfg, **extra_env),
            capture_output=True,
            text=True,
        )
        for line in r.stdout.splitlines():
            if line.startswith("password="):
                return line.removeprefix("password=")
        return None

    def test_declared_org_and_other_orgs_resolve_to_different_credentials(
        self, tmp_path, monkeypatch
    ):
        """The whole point: same host, different org -> different credential."""
        cfg = self._composed(tmp_path, monkeypatch)
        env = {"ORG_A_PAT": "ORG_SPECIFIC"}
        assert self._fill(cfg, "OrgA/somerepo.git", env) == "ORG_SPECIFIC"
        assert self._fill(cfg, "OtherOrg/somerepo.git", env) == "HOST_DEFAULT"

    def test_operator_identity_survives_the_include(self, tmp_path, monkeypatch):
        """The git-identity guardrail forbids per-bot identity, so the composed
        config must not shadow the operator's user.email."""
        cfg = self._composed(tmp_path, monkeypatch)
        r = self._git(cfg, ["config", "--get", "user.email"])
        assert r.stdout.strip() == "operator@example.com"

    # --- what useHttpPath does, and does not, do --------------------------

    def test_org_routing_does_not_depend_on_use_http_path(self, tmp_path, monkeypatch):
        """Guards the RATIONALE, not the line. It is tempting to read
        `useHttpPath` as the thing that lets an org-scoped section match — it is
        not, because git reads (and URL-matches) config before the !useHttpPath
        strip. Stripping the line must therefore change nothing about routing;
        if this ever fails, git reordered that internal and the line stopped
        being merely defensive."""
        cfg = self._composed(tmp_path, monkeypatch)
        stripped = tmp_path / "no-usehttppath.gitconfig"
        stripped.write_text(
            "".join(
                ln
                for ln in cfg.read_text().splitlines(keepends=True)
                if "useHttpPath" not in ln
            )
        )
        env = {"ORG_A_PAT": "ORG_SPECIFIC"}
        assert self._fill(stripped, "OrgA/somerepo.git", env) == "ORG_SPECIFIC"
        assert self._fill(stripped, "OtherOrg/somerepo.git", env) == "HOST_DEFAULT"

    def test_use_http_path_forwards_the_repo_path_to_the_helper(
        self, tmp_path, monkeypatch
    ):
        """What the line actually buys: helpers receive `path=`. Harmless here
        only because the reset above drops every storage-backed helper for this
        host — otherwise osxkeychain/store would key one entry per repo."""
        echo_path = (
            "#!/bin/sh\np=$(sed -n 's/^path=//p')\n"
            'echo username=u\necho "password=saw:${p:-none}"\n'
        )
        cfg = self._composed(tmp_path, monkeypatch, gh_body=echo_path)
        assert self._fill(cfg, "OtherOrg/repo.git", {}) == "saw:OtherOrg/repo.git"

    # --- properties the composed shape silently relies on -----------------

    def test_the_reset_leaves_other_hosts_alone(self, tmp_path, monkeypatch):
        """The empty `helper =` is URL-filtered, so it clears github.com's list
        WITHOUT disarming the operator's helper for every other host. A reset
        written un-scoped would silently break pushes to GitLab et al."""
        other_host = tmp_path / "other-host-helper"
        _write_exec(
            str(other_host), "#!/bin/sh\necho username=u\necho password=ELSEWHERE\n"
        )
        user_cfg = tmp_path / "user.gitconfig"
        user_cfg.write_text(
            f"[user]\n\temail = operator@example.com\n"
            f"[credential]\n\thelper = {other_host}\n"
        )
        cfg = self._composed(tmp_path, monkeypatch, user_cfg=user_cfg)
        r = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=gitlab.com\npath=team/repo.git\n\n",
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": str(cfg),
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
            },
            capture_output=True,
            text=True,
        )
        assert "password=ELSEWHERE" in r.stdout

    def test_an_org_section_does_not_capture_a_longer_org_name(
        self, tmp_path, monkeypatch
    ):
        """Org sections are URL path prefixes, so `Claud` must not swallow
        `Claudfather` — git matches a path prefix only at a `/` boundary. Real
        org names collide this way (Claud/Claudfather, acme/acme-labs), and the
        failure would be a wrong token, not an error."""
        cfg = self._composed(tmp_path, monkeypatch, creds={"Claud": "ORG_A_PAT"})
        env = {"ORG_A_PAT": "ORG_SPECIFIC"}
        assert self._fill(cfg, "Claud/repo.git", env) == "ORG_SPECIFIC"
        assert self._fill(cfg, "Claudfather/Claudlobby.git", env) == "HOST_DEFAULT"


class TestGitConfigLifecycleThroughComposeBot:
    """Drives the real compose_bot / diff paths rather than re-asserting the
    branch in the test body."""

    def test_dropping_the_declaration_removes_a_stale_file(self, fleet_dir):
        """Stale routing after a fleet.yaml edit would keep using an old token,
        so compose must DELETE the file, not merely stop writing it. Exercises
        compose_bot's unlink branch — asserting pathlib.unlink works would be a
        test of the standard library."""
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        bot.git_credentials = {}
        bot_dir = paths.bot_runtime("lead")
        bot_dir.mkdir(parents=True, exist_ok=True)
        stale = bot_dir / GITCONFIG_FILENAME
        stale.write_text("stale routing\n")
        compose_bot(bot, fleet, paths)
        assert not stale.exists(), "a dropped declaration must remove the file"

    def test_diff_surfaces_a_hand_edited_gitconfig(self, fleet_dir):
        """F1's whole mitigation: `git config --global` inside a bot session
        writes to the composed file and is lost on regenerate, so drift
        detection is the only thing that surfaces the loss."""
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        bot.git_credentials = {"OrgA": "ORG_A_PAT"}
        compose_bot(bot, fleet, paths)
        gitconfig = paths.bot_runtime("lead") / GITCONFIG_FILENAME
        assert gitconfig.is_file()
        gitconfig.write_text(gitconfig.read_text() + "\n[user]\n\tname = Hand Edited\n")
        out = diff_bot("lead", fleet, paths)
        assert ".gitconfig drift in lead" in out
        assert "Hand Edited" in out


class TestBotEnvStubDoesNotShadowUpstream:
    """A bot-tier stub must never blank a value the fleet tier already set.

    start-bot.sh sources .env tiers in order and the bot tier is LAST, so a live
    `export FOO=` in the bot's scaffold wins over the operator's real value.
    Following the /setup skill exactly hit this: the skill writes the Telegram
    token to the fleet-tier .env, generate then scaffolded an empty bot-tier
    stub, and the bot booted with no token and a dead inbound bridge — with
    nothing in the logs pointing at the cause.
    """

    def _env_var(self, name: str):
        from claudlobby.composer import EnvVar

        return EnvVar(
            name=name, description="test var", default_tier="bot", source="test"
        )

    def test_upstream_provided_var_is_commented_not_live(self, tmp_path):
        from claudlobby.composer import _scaffold_env_merge

        bot_env = tmp_path / "bot.env"
        _scaffold_env_merge(
            bot_env,
            "# Bot environment for: test",
            [self._env_var("TELEGRAM_TOKEN_X")],
            provided_upstream=frozenset({"TELEGRAM_TOKEN_X"}),
        )
        text = bot_env.read_text()
        assert "# export TELEGRAM_TOKEN_X=" in text
        assert not re.search(r"^export TELEGRAM_TOKEN_X=", text, re.MULTILINE), (
            "a live empty export would override the upstream value:\n" + text
        )

    def test_var_absent_upstream_still_gets_a_live_stub(self, tmp_path):
        from claudlobby.composer import _scaffold_env_merge

        bot_env = tmp_path / "bot.env"
        _scaffold_env_merge(
            bot_env, "# Bot environment for: test", [self._env_var("SOME_OTHER_TOKEN")]
        )
        assert re.search(
            r"^export SOME_OTHER_TOKEN=", bot_env.read_text(), re.MULTILINE
        ), "vars with no upstream value must still be prompted for"

    def test_scaffold_env_files_derives_upstream_from_the_real_fleet_env(
        self, tmp_path
    ):
        """End-to-end through the PUBLIC entry point, not the private merge.

        The three tests around this one hand-feed `provided_upstream`, which
        leaves the part most likely to be miswired — `scaffold_env_files`
        actually reading the fleet-tier .env off disk and threading the result
        through the per-bot loop — with no coverage at all. This drives the real
        function against a real fleet tree so that wiring is exercised.

        Reuses TestScaffoldEnvMerge._setup_fleet rather than standing up a
        second fleet idiom in the same file; its `worker` bot already declares a
        bot-tier token_env (TG_TOKEN_W), which is exactly the shape at issue.
        """
        root, paths = TestScaffoldEnvMerge()._setup_fleet(tmp_path)
        # Operator has already set the token at the FLEET tier — as /setup does.
        (root / ".env").write_text("TG_TOKEN_W=realvalue123\n")

        fleet, _md = load_fleet(root / "fleet.yaml")
        scaffold_env_files(fleet, paths, log=lambda m: None)

        bot_env = (root / "runtime" / "bots" / "worker" / ".env").read_text()
        assert "# export TG_TOKEN_W=" in bot_env, bot_env
        assert not re.search(r"^export TG_TOKEN_W=", bot_env, re.MULTILINE), (
            "scaffold_env_files emitted a live empty stub over a fleet-tier "
            f"value:\n{bot_env}"
        )

    def test_live_stub_is_neutralised_once_upstream_gains_a_value(self, tmp_path):
        """The ordinary dev loop, which the first version of this fix did not survive.

        Generate before the token exists (live stub is written), obtain the
        token, fill it in at the fleet tier, generate again. provided_upstream is
        consulted only when a key is FIRST scaffolded, so the original blanking
        stub used to survive every later generate and reproduce the bug in full —
        proven by running _scaffold_env_merge three times and watching the live
        stub persist unchanged.
        """
        from claudlobby.composer import _scaffold_env_merge

        bot_env = tmp_path / "bot.env"
        # Run 1: nothing upstream yet -> a live stub, correctly.
        _scaffold_env_merge(
            bot_env, "# Bot environment for: test", [self._env_var("TELEGRAM_TOKEN_X")]
        )
        assert re.search(
            r"^export TELEGRAM_TOKEN_X=", bot_env.read_text(), re.MULTILINE
        )

        # Run 2: operator has since set it at the fleet tier.
        _scaffold_env_merge(
            bot_env,
            "# Bot environment for: test",
            [self._env_var("TELEGRAM_TOKEN_X")],
            provided_upstream=frozenset({"TELEGRAM_TOKEN_X"}),
        )
        text = bot_env.read_text()
        assert not re.search(r"^export TELEGRAM_TOKEN_X=", text, re.MULTILINE), (
            "the pre-existing live stub still blanks the upstream value:\n" + text
        )
        assert "# export TELEGRAM_TOKEN_X=" in text

    def test_operator_written_value_is_never_rewritten(self, tmp_path):
        """Only a pristine `export FOO=` is neutralised — never a real value."""
        from claudlobby.composer import _scaffold_env_merge

        bot_env = tmp_path / "bot.env"
        bot_env.write_text(
            "# Bot environment for: test\nexport TELEGRAM_TOKEN_X=perbot\n"
        )
        _scaffold_env_merge(
            bot_env,
            "# Bot environment for: test",
            [self._env_var("TELEGRAM_TOKEN_X")],
            provided_upstream=frozenset({"TELEGRAM_TOKEN_X"}),
        )
        assert "export TELEGRAM_TOKEN_X=perbot" in bot_env.read_text(), (
            "a per-bot override the operator set was clobbered"
        )

    def test_upstream_spans_home_and_root_tiers_not_just_the_fleet_env(
        self, tmp_path, monkeypatch
    ):
        """The tier list must match start-bot.sh, and be TESTED to match.

        Crippling _upstream_env_names to (paths.env_file,) — the exact "one tier
        of four" bug — left every other test in this file passing, because the
        fleet fixture sets fleet_dir == root and so collapses two tiers onto one
        file. A var in $HOME/.env, which lib-common.sh's own deprecation notice
        tells operators to use, was never exercised at all.
        """
        from claudlobby.composer import _upstream_env_names

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".env").write_text("HOME_TIER_TOKEN=realvalue\nEMPTY_ONE=\n")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / ".env").write_text("ROOT_TIER_TOKEN=realvalue\n")
        paths = _make_paths(root)

        names = _upstream_env_names(paths)
        assert "HOME_TIER_TOKEN" in names, (
            "$HOME/.env is sourced above the bot tier by start-bot.sh and must "
            f"count as upstream; got {sorted(names)}"
        )
        assert "ROOT_TIER_TOKEN" in names, (
            f"the repo-root .env tier is also upstream; got {sorted(names)}"
        )
        assert "EMPTY_ONE" not in names, "an empty value is not 'provided'"

    def test_upstream_value_survives_real_shell_sourcing_order(self, tmp_path):
        """The property that actually matters, exercised through bash."""
        from claudlobby.composer import _scaffold_env_merge

        fleet_env = tmp_path / "fleet.env"
        fleet_env.write_text("TELEGRAM_TOKEN_X=realvalue123\n")
        bot_env = tmp_path / "bot.env"
        _scaffold_env_merge(
            bot_env,
            "# Bot environment for: test",
            [self._env_var("TELEGRAM_TOKEN_X")],
            provided_upstream=frozenset({"TELEGRAM_TOKEN_X"}),
        )
        script = f'set -a; . "{fleet_env}"; . "{bot_env}"; printf "%s" "${{TELEGRAM_TOKEN_X:-EMPTY}}"'
        out = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True
        ).stdout
        assert out == "realvalue123", (
            f"bot-tier scaffold clobbered the fleet-tier value: got {out!r}"
        )


class TestFleetEnvStubDoesNotShadowUpstream:
    """A FLEET-tier stub must not blank a value an upstream tier already set (#1206).

    The guard existed and was wired to the bot tier only. start-bot.sh sources
    $HOME/.env, then the repo-root .env, then the fleet .env, then the bot .env,
    so the fleet tier is downstream of two populated tiers and an empty stub
    there is an ASSIGNMENT, not a placeholder. Measured live on one fleet:
    NEON_API_KEY, RAILWAY_API_TOKEN and RAILWAY_PERSONAL_TOKEN all held real
    values in BOTH upstream tiers and were destroyed by their own fleet stubs.

    Every test here uses a REAL overlay layout (fleet_dir under local/), never
    `_make_paths`, which sets fleet_dir == root and collapses the two tiers onto
    one file — the same fixture degeneracy already called out at
    TestBotEnvStubDoesNotShadowUpstream.test_upstream_spans_home_and_root_tiers.
    Against that fixture none of this is observable.
    """

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path, monkeypatch):
        """No test here may read the host's real ``~/.env``.

        ``_upstream_env_names`` walks ``Path.home()/".env"``, so without this the
        computed tier set depends on whichever credentials the developer happens
        to have on disk. Caught while probing this very class: a differential run
        returned seven real credential NAMES off the host. That is the
        green-on-two-machines-red-in-CI shape, and it is silent.
        """
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    def _setup_overlay_fleet(self, tmp_path: Path) -> tuple[Path, Path, Paths]:
        root = tmp_path / "claudlobby"
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
        (root / "library" / "mcp").mkdir(parents=True)
        (root / "library" / "mcp" / "github.json").write_text(
            json.dumps(
                {
                    "github": {"command": "gh", "args": ["mcp"]},
                    "_env_contract": {
                        # Provided upstream — must NOT get a live fleet stub.
                        "UPSTREAM_TOKEN": {
                            "description": "set upstream",
                            "default_tier": "fleet",
                        },
                        # No upstream value anywhere — MUST still get one, or the
                        # test above passes for the trivial reason that nothing
                        # is ever stubbed.
                        "FLEET_ONLY_TOKEN": {"description": "unset", "default_tier": "fleet"},
                    },
                }
            )
        )
        fleet_dir = root / "local" / "test-fleet"
        fleet_dir.mkdir(parents=True)
        (fleet_dir / "fleet.yaml").write_text(
            dedent("""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                worker:
                  expertise: [eng]
                  mcp: [github]
        """)
        )
        for d in (
            fleet_dir / "runtime" / "bots" / "worker",
            root / "runtime" / "bots" / "worker",
        ):
            d.mkdir(parents=True, exist_ok=True)
        return root, fleet_dir, Paths(root=root, fleet_dir=fleet_dir)

    def _scaffold(self, root: Path, fleet_dir: Path, paths: Paths) -> str:
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        scaffold_env_files(fleet, paths, log=lambda m: None)
        return (fleet_dir / ".env").read_text()

    def test_upstream_provided_var_is_not_a_live_fleet_stub(self, tmp_path):
        """Composition arm, with its own vacuity control in the same run."""
        root, fleet_dir, paths = self._setup_overlay_fleet(tmp_path)
        (root / ".env").write_text("UPSTREAM_TOKEN=realvalue123\n")

        text = self._scaffold(root, fleet_dir, paths)

        assert not re.search(r"^export UPSTREAM_TOKEN=", text, re.MULTILINE), (
            "a live empty fleet stub blanks the upstream value at runtime:\n" + text
        )
        # Control: without this the assertion above is satisfied by a scaffold
        # that simply never writes anything.
        assert re.search(r"^export FLEET_ONLY_TOKEN=", text, re.MULTILINE), (
            "a var with no upstream value must still be prompted for:\n" + text
        )

    def test_existing_blanking_fleet_stub_is_healed_in_place(self, tmp_path):
        """The live damage is repaired by the next generate, not merely averted.

        Three real credentials were already destroyed on disk when this was
        found. A fix that only stops NEW stubs leaves those in place forever,
        because provided_upstream is otherwise consulted only when a key is
        first scaffolded.
        """
        root, fleet_dir, paths = self._setup_overlay_fleet(tmp_path)
        (root / ".env").write_text("UPSTREAM_TOKEN=realvalue123\n")
        (fleet_dir / ".env").write_text(
            "# Fleet environment for: test-fleet\nexport UPSTREAM_TOKEN=\n"
        )

        text = self._scaffold(root, fleet_dir, paths)

        assert not re.search(r"^export UPSTREAM_TOKEN=", text, re.MULTILINE), (
            "the pre-existing blanking stub was left live:\n" + text
        )

    def test_fleet_tier_upstream_excludes_the_fleet_file_itself(self, tmp_path):
        """`for_tier` selects the tier set, and the two must actually differ.

        `paths.env_file` resolves to the FLEET .env once one exists, so reusing
        the bot-tier tier list here would let the file being scaffolded count as
        its own upstream.

        MEASURED, so the next reader does not misdiagnose a green mutation run:
        swapping the call site to `_upstream_env_names(paths)` leaves this whole
        class green and produces BYTE-IDENTICAL scaffold output. That is an
        INERT MUTANT, not weak tests, and the difference is provable rather than
        lucky — every name the fleet file could add is by definition already a
        key of the file being written, so it can never be a NEW var needing a
        stub; and it can never be a pristine `export X=` stub either, because
        pristine means empty and this helper filters to non-empty values.

        So this test guards the helper's CONTRACT (the sets do differ, asserted
        below), not the call site — no test can catch that swap today. The call
        site is written for intent and for robustness if `existing_keys` or
        `Paths.env_file` semantics ever move.
        """
        from claudlobby.composer import _upstream_env_names

        root, fleet_dir, paths = self._setup_overlay_fleet(tmp_path)
        (root / ".env").write_text("ROOT_TIER_TOKEN=realvalue\n")
        (fleet_dir / ".env").write_text("FLEET_TIER_TOKEN=realvalue\n")
        assert paths.env_file == fleet_dir / ".env", (
            "fixture precondition: env_file must resolve to the fleet file"
        )

        fleet_view = _upstream_env_names(paths, for_tier="fleet")
        bot_view = _upstream_env_names(paths, for_tier="bot")

        assert "ROOT_TIER_TOKEN" in fleet_view
        assert "FLEET_TIER_TOKEN" not in fleet_view, (
            "the fleet file is not upstream of itself; counting it makes the "
            f"guard protect nothing. got {sorted(fleet_view)}"
        )
        assert "FLEET_TIER_TOKEN" in bot_view, (
            "the fleet tier IS upstream of a bot stub and must stay so; "
            f"got {sorted(bot_view)}"
        )

    def test_unknown_tier_raises_rather_than_silently_guarding_less(self, tmp_path):
        from claudlobby.composer import _upstream_env_names

        _root, _fleet_dir, paths = self._setup_overlay_fleet(tmp_path)
        with pytest.raises(ValueError, match="unknown tier"):
            _upstream_env_names(paths, for_tier="blot")

    def test_upstream_value_survives_real_shell_sourcing_order(self, tmp_path):
        """Behaviour arm — the property that actually matters, through bash.

        Composition is necessary and not sufficient: what breaks a credential is
        the shell, so the tier chain is sourced for real in start-bot.sh's order.
        """
        root, fleet_dir, paths = self._setup_overlay_fleet(tmp_path)
        (root / ".env").write_text("UPSTREAM_TOKEN=realvalue123\n")
        self._scaffold(root, fleet_dir, paths)

        script = (
            f'set -a; . "{root / ".env"}"; . "{fleet_dir / ".env"}"; '
            'printf "%s" "${UPSTREAM_TOKEN:-EMPTY}"'
        )
        out = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True
        ).stdout
        assert out == "realvalue123", (
            f"fleet-tier scaffold clobbered the upstream value: got {out!r}"
        )

    def test_an_empty_stub_really_does_blank_upstream(self, tmp_path):
        """The other arm, so the test above cannot pass for the wrong reason.

        If sourcing an empty assignment were harmless, every assertion in this
        class would hold no matter what the composer wrote. This pins the
        premise: absent falls through, empty blanks.
        """
        root = tmp_path / "tiers"
        root.mkdir()
        upstream = root / "upstream.env"
        upstream.write_text("UPSTREAM_TOKEN=realvalue123\n")

        absent = root / "absent.env"
        absent.write_text("# no mention of the key at all\n")
        empty = root / "empty.env"
        empty.write_text("export UPSTREAM_TOKEN=\n")

        def resolve(second: Path) -> str:
            script = (
                f'set -a; . "{upstream}"; . "{second}"; '
                'printf "%s" "${UPSTREAM_TOKEN:-EMPTY}"'
            )
            return subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True
            ).stdout

        assert resolve(absent) == "realvalue123", "an absent key must fall through"
        assert resolve(empty) == "EMPTY", (
            "an empty stub must blank the upstream value — if this fails the "
            "premise of #1206 is wrong and the rest of this class proves nothing"
        )


class TestLaunchdArgvSplitting:
    """A job whose `script` carries flags must still yield a valid plist (#969).

    systemd and launchd disagree about what a command string is. `ExecStart=` is
    a command LINE that systemd splits on whitespace, so
    `.../data-sweep.sh --purge` works there. launchd's `ProgramArguments[0]` is
    the executable PATH, so the same string becomes a file that does not exist
    and the job can never exec — silently, and only on macOS.

    Observed: the composed `data-sweep` plist on a real host had
    `argv[0] == '<root>/lib/data-sweep.sh --purge'`, `os.path.exists() == False`.
    It was also the only job of thirteen carrying a flag, which is why it went
    unnoticed — but the asymmetry is structural, so any future flag-bearing job
    would break the same way.
    """

    def _plist(self, tmp_path, script):
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)
        paths = _make_paths(root)
        timers_dir = tmp_path / "timers"
        timers_dir.mkdir(exist_ok=True)
        _write_timer_units(
            timers_dir,
            "com.t.job",
            "job",
            {"type": "calendar", "expression": "daily"},
            script,
            "oneshot",
            "t",
            paths,
        )
        return plistlib.loads((timers_dir / "com.t.job.plist").read_bytes())

    def test_flagged_script_splits_into_separate_argv_entries(self, tmp_path):
        pl = self._plist(tmp_path, "$CLAUDLOBBY_ROOT/lib/data-sweep.sh --purge")
        argv = pl["ProgramArguments"]
        assert argv[0].endswith("/lib/data-sweep.sh"), (
            f"argv[0] must be the executable alone, got {argv[0]!r}"
        )
        assert " " not in argv[0], f"argv[0] contains a space: {argv[0]!r}"
        assert "--purge" in argv, f"the flag was dropped: {argv}"

    def test_flag_precedes_fleet_name_as_on_systemd(self, tmp_path):
        """Order must match the systemd form (`script --purge <fleet>`), or the
        two platforms would pass arguments differently to the same script."""
        pl = self._plist(tmp_path, "$CLAUDLOBBY_ROOT/lib/data-sweep.sh --purge")
        argv = pl["ProgramArguments"]
        assert argv.index("--purge") < argv.index("t"), (
            f"flag must precede the fleet name, got {argv}"
        )

    def test_unflagged_script_is_unchanged(self, tmp_path):
        pl = self._plist(tmp_path, "$CLAUDLOBBY_ROOT/lib/plain.sh")
        argv = pl["ProgramArguments"]
        assert argv[0].endswith("/lib/plain.sh")
        assert argv[1] == "t"


class TestComposeBotConfTelegramStateDirExported:
    """TELEGRAM_STATE_DIR must be EXPORTED into bot.conf (#976).

    Most bot.conf lines deliberately lack `export` — lib/ reads them from the
    file via `bot_conf_get`. This one is different: it has to reach the `claude`
    CHILD process environment, because the telegram plugin resolves its state
    directory from `process.env.TELEGRAM_STATE_DIR` and silently falls back to
    the shared `~/.claude/channels/telegram` when it is unset. That fallback
    parks every bot's `bot.pid` outside its own state dir, and since the plugin
    enforces a singleton per state dir, only one bot on a host can ever hold a
    poller.

    `lib-common.sh` already assumes the export happened — its ownership check
    greps the poller's environ for this exact `KEY=VALUE`. So the export is a
    contract between compositor and consumer, not a stylistic choice.

    Observed unexported: ten per-handle channel dirs on one host, spanning bots
    created over three months, had never contained a `bot.pid`.
    """

    def _compose(self, tmp_path, handle="w_bot"):
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle=handle),
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

    def test_state_dir_is_exported(self, tmp_path):
        conf = self._compose(tmp_path)
        assert "export TELEGRAM_STATE_DIR=" in conf, (
            "TELEGRAM_STATE_DIR is emitted without `export`, so it never reaches "
            "the claude child process and the telegram plugin falls back to the "
            "shared channel dir (#976)."
        )

    def test_state_dir_is_per_handle(self, tmp_path):
        conf = self._compose(tmp_path, handle="w_bot")
        assert "telegram-w_bot" in conf

    def test_no_unexported_state_dir_line_remains(self, tmp_path):
        """Guard against a regression that adds the export while leaving the
        original bare assignment behind — the later line would win."""
        conf = self._compose(tmp_path)
        bare = [
            ln
            for ln in conf.splitlines()
            if ln.strip().startswith("TELEGRAM_STATE_DIR=")
        ]
        assert not bare, f"unexported TELEGRAM_STATE_DIR line(s) present: {bare}"
