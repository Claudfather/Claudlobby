"""Tests for composer.py — gap coverage: _expand, _bot_template_context, _compose_expertise,
compose_claude_md, compose_mcp_json, link_skills, _shq, compose_bot, compose_launchd_plist."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from claudlobby.config import (
    BotConfig,
    McpEntry,
    TelegramConfig,
    load_fleet,
)
from claudlobby.composer import (
    _bot_template_context,
    _compose_expertise,
    _expand,
    _expand_item,
    _shq,
    compose_bot,
    compose_claude_md,
    compose_fleet,
    compose_launchd_plist,
    compose_mcp_json,
    link_skills,
)
from claudlobby.loader import LibraryItem
from claudlobby.paths import Paths


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root)


# ── _expand ──────────────────────────────────────────────────────────


class TestExpand:
    def test_basic_replacement(self):
        assert _expand("Hello {{NAME}}!", {"NAME": "World"}) == "Hello World!"

    def test_multiple_keys(self):
        result = _expand("{{A}} and {{B}}", {"A": "1", "B": "2"})
        assert result == "1 and 2"

    def test_missing_key_left_as_is(self):
        assert _expand("{{MISSING}}", {}) == "{{MISSING}}"

    def test_empty_text(self):
        assert _expand("", {"A": "1"}) == ""

    def test_no_placeholders(self):
        assert _expand("plain text", {"A": "1"}) == "plain text"

    def test_repeated_placeholder(self):
        assert _expand("{{X}} {{X}}", {"X": "Y"}) == "Y Y"


# ── _bot_template_context ────────────────────────────────────────────


class TestBotTemplateContext:
    def test_builds_expected_keys(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        ctx = _bot_template_context(bot, fleet, paths)
        assert ctx["BOT_ID"] == "lead"
        assert ctx["BOT_NAME"] == "lead"
        assert ctx["BOT_ID_UPPER"] == "LEAD"
        assert ctx["FLEET_NAME"] == "test-fleet"
        assert ctx["SERVICE_PREFIX"] == "com.test"
        assert ctx["CLAUDLOBBY_ROOT"] == str(fleet_dir)

    def test_telegram_chat_id_from_fleet(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        ctx = _bot_template_context(bot, fleet, paths)
        assert ctx["TELEGRAM_GROUP_CHAT_ID"] == "-100999"


# ── _expand_item ─────────────────────────────────────────────────────


class TestExpandItem:
    def test_expands_title_and_body(self):
        item = LibraryItem(
            title="{{BOT_NAME}} Protocol",
            description="Protocol for {{BOT_NAME}}",
            body="Body for {{BOT_NAME}}.",
            source_path=Path("/fake"),
        )
        expanded = _expand_item(item, {"BOT_NAME": "astrid"})
        assert expanded.title == "astrid Protocol"
        assert expanded.description == "Protocol for astrid"
        assert expanded.body == "Body for astrid."
        assert expanded.source_path == Path("/fake")

    def test_none_description_stays_none(self):
        item = LibraryItem(
            title="T", description=None, body="B", source_path=Path("/f")
        )
        expanded = _expand_item(item, {"X": "Y"})
        assert expanded.description is None


# ── _compose_expertise ───────────────────────────────────────────────


class TestComposeExpertise:
    def test_single_expertise(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["worker-1"]
        ctx = _bot_template_context(bot, fleet, paths)
        label, body = _compose_expertise(bot, paths, ctx)
        assert label == "Engineer"
        assert "Build things." in body

    def test_empty_expertise_raises(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        bot_copy = BotConfig(
            bot_id="empty",
            name="empty",
            expertise=[],
            telegram=TelegramConfig(handle="e_bot", token_env="T"),
        )
        ctx = _bot_template_context(bot_copy, fleet, paths)
        with pytest.raises(ValueError, match="expertise list is empty"):
            _compose_expertise(bot_copy, paths, ctx)

    def test_multi_expertise_concatenation(self, fleet_dir):
        # Add a second expertise file
        (fleet_dir / "library" / "expertise" / "ops.md").write_text(
            "# {{BOT_NAME}} — Ops\n\nOperate things.\n"
        )
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = BotConfig(
            bot_id="multi",
            name="multi",
            expertise=["orchestration", "ops"],
            telegram=TelegramConfig(handle="m_bot", token_env="T"),
        )
        ctx = _bot_template_context(bot, fleet, paths)
        label, body = _compose_expertise(bot, paths, ctx)
        assert label == "Orchestrator"  # from first expertise
        assert "Manage the team." in body
        assert "Operate things." in body


# ── _shq ─────────────────────────────────────────────────────────────


class TestShq:
    def test_simple_value(self):
        # shlex.quote leaves shell-safe strings unquoted
        assert _shq("hello") == "hello"

    def test_value_with_spaces(self):
        assert _shq("hello world") == "'hello world'"

    def test_dollar_sign(self):
        result = _shq("$HOME")
        assert "$" in result
        assert "HOME" in result

    def test_backtick(self):
        result = _shq("`whoami`")
        assert "`" in result

    def test_integer(self):
        assert _shq(42) == "42"

    def test_empty_string(self):
        assert _shq("") == "''"


# ── compose_mcp_json ────────────────────────────────────────────────


class TestComposeMcpJson:
    def test_basic_mcp_fragment(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        # lead has no mcp config in minimal fleet, add it
        bot_with_mcp = BotConfig(
            bot_id="lead",
            name="lead",
            expertise=["orchestration"],
            mcp=[McpEntry(name="github")],
            telegram=TelegramConfig(handle="lead_bot", token_env="TELEGRAM_TOKEN_LEAD"),
        )
        result = compose_mcp_json(bot_with_mcp, paths)
        assert "mcpServers" in result
        assert "github" in result["mcpServers"]
        assert result["mcpServers"]["github"]["command"] == "gh"

    def test_empty_mcp_returns_empty(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = BotConfig(
            bot_id="t",
            name="t",
            expertise=["orchestration"],
            mcp=[],
            telegram=TelegramConfig(handle="t_bot", token_env="T"),
        )
        result = compose_mcp_json(bot, paths)
        assert result == {"mcpServers": {}}

    def test_missing_fragment_skipped(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        bot = BotConfig(
            bot_id="t",
            name="t",
            expertise=["orchestration"],
            mcp=[McpEntry(name="nonexistent")],
            telegram=TelegramConfig(handle="t_bot", token_env="T"),
        )
        result = compose_mcp_json(bot, paths)
        assert result == {"mcpServers": {}}

    def test_multi_instance(self, fleet_dir):
        # Create a notion MCP fragment with instances
        (fleet_dir / "library" / "mcp" / "notion.json").write_text(
            json.dumps(
                {
                    "notion": {
                        "command": "npx",
                        "args": ["-y", "@notionhq/notion-mcp-server"],
                    },
                    "_env_contract": {
                        "NOTION_TOKEN": {"description": "Notion token", "default_tier": "bot"},
                    },
                }
            )
        )
        paths = _make_paths(fleet_dir)
        bot = BotConfig(
            bot_id="t",
            name="t",
            expertise=["orchestration"],
            mcp=[McpEntry(name="notion", instances=["work", "personal"])],
            telegram=TelegramConfig(handle="t_bot", token_env="T"),
        )
        result = compose_mcp_json(bot, paths)
        assert "notion-work" in result["mcpServers"]
        assert "notion-personal" in result["mcpServers"]


# ── compose_claude_md ────────────────────────────────────────────────


class TestComposeClaudeMd:
    def test_renders_bot_name(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        md = compose_claude_md(fleet.bots["lead"], fleet, paths)
        assert "lead" in md

    def test_includes_expertise(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        md = compose_claude_md(fleet.bots["worker-1"], fleet, paths)
        assert "Build things." in md

    def test_includes_guardrails_when_template_supports_it(self, fleet_dir):
        """Guardrails appear in output when the template renders them."""
        (fleet_dir / "templates" / "claude.md.j2").write_text(
            "# {{ bot.name }}\n\n{{ expertise_body }}\n\n"
            "{% for g in guardrails %}### {{ g.title }}\n\n{{ g.body }}\n\n{% endfor %}"
        )
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        md = compose_claude_md(fleet.bots["lead"], fleet, paths)
        assert "Never push to main" in md

    def test_includes_protocols_when_template_supports_it(self, fleet_dir):
        (fleet_dir / "templates" / "claude.md.j2").write_text(
            "# {{ bot.name }}\n\n{{ expertise_body }}\n\n"
            "{% for p in protocols %}### {{ p.title }}\n\n{{ p.body }}\n\n{% endfor %}"
        )
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        md = compose_claude_md(fleet.bots["lead"], fleet, paths)
        assert "Report back" in md


# ── compose_launchd_plist ────────────────────────────────────────────


class TestComposeLaunchdPlist:
    def test_basic_plist(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        plist = compose_launchd_plist(bot, fleet, paths)
        assert "<?xml" in plist
        assert "com.test.lead" in plist
        assert "start-bot.sh" in plist

    def test_label_uses_service_prefix(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        plist = compose_launchd_plist(fleet.bots["worker-1"], fleet, paths)
        assert "com.test.worker-1" in plist


# ── link_skills ──────────────────────────────────────────────────────


class TestLinkSkills:
    def _setup_skill(self, fleet_dir, name):
        skill_dir = fleet_dir / "library" / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}\n\nDo the thing.\n")
        return skill_dir

    def test_links_single_skill(self, fleet_dir):
        self._setup_skill(fleet_dir, "simplify")
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = BotConfig(
            bot_id="sk",
            name="sk",
            expertise=["orchestration"],
            skills=["simplify"],
            telegram=TelegramConfig(handle="sk_bot", token_env="T"),
        )
        bot_dir = paths.bot_runtime(bot.bot_id)
        (bot_dir / ".claude" / "skills").mkdir(parents=True)
        link_skills(bot, paths, logging.getLogger("test").info)
        assert (bot_dir / ".claude" / "skills" / "simplify").is_symlink()

    def test_skips_missing_skill(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        bot = BotConfig(
            bot_id="sk2",
            name="sk2",
            expertise=["orchestration"],
            skills=["nonexistent-skill"],
            telegram=TelegramConfig(handle="sk2_bot", token_env="T"),
        )
        bot_dir = paths.bot_runtime(bot.bot_id)
        (bot_dir / ".claude" / "skills").mkdir(parents=True)
        # Should not raise
        link_skills(bot, paths, logging.getLogger("test").info)

    def test_empty_skills(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        bot = BotConfig(
            bot_id="sk3",
            name="sk3",
            expertise=["orchestration"],
            skills=[],
            telegram=TelegramConfig(handle="sk3_bot", token_env="T"),
        )
        bot_dir = paths.bot_runtime(bot.bot_id)
        (bot_dir / ".claude" / "skills").mkdir(parents=True)
        link_skills(bot, paths, logging.getLogger("test").info)
        # No symlinks created
        assert list((bot_dir / ".claude" / "skills").iterdir()) == []


# ── compose_bot ──────────────────────────────────────────────────────


class TestComposeBot:
    def test_creates_all_expected_files(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        bot_dir = compose_bot(bot, fleet, paths)
        assert bot_dir.is_dir()
        assert (bot_dir / "CLAUDE.md").is_file()
        assert (bot_dir / "bot.conf").is_file()
        assert (bot_dir / ".mcp.json").is_file()
        assert (bot_dir / ".claude").is_dir()
        assert (bot_dir / "memory").is_dir()
        assert (bot_dir / "data").is_dir()
        assert (bot_dir / "data" / "events").is_dir()

    def test_creates_settings_local_json(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["worker-1"]
        bot_dir = compose_bot(bot, fleet, paths)
        settings_path = bot_dir / ".claude" / "settings.local.json"
        assert settings_path.is_file()
        settings = json.loads(settings_path.read_text())
        assert "permissions" in settings

    def test_mcp_trust_survives_regenerate(self, fleet_dir):
        """MCP trust is re-derived every generate; blanket flag + stray runtime keys are dropped."""
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["worker-1"]
        bot.mcp = [McpEntry(name="github")]  # fixture ships library/mcp/github.json

        bot_dir = compose_bot(bot, fleet, paths)
        settings_path = bot_dir / ".claude" / "settings.local.json"
        first = json.loads(settings_path.read_text())
        assert first["enabledMcpjsonServers"] == ["github"]
        assert "enableAllProjectMcpServers" not in first

        # Simulate what Claude Code persists at runtime: a blanket grant + an unrelated key,
        # and the allowlist removed — the exact state a naive overwrite would strip.
        first["enableAllProjectMcpServers"] = True
        first["runtimeOnlyKey"] = "x"
        del first["enabledMcpjsonServers"]
        settings_path.write_text(json.dumps(first))

        # Regenerate: trust is re-derived (survives); blanket + stray gone (narrow full-overwrite).
        compose_bot(bot, fleet, paths)
        second = json.loads(settings_path.read_text())
        assert second["enabledMcpjsonServers"] == ["github"]
        assert "enableAllProjectMcpServers" not in second
        assert "runtimeOnlyKey" not in second

    def test_returns_bot_dir_path(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        bot_dir = compose_bot(bot, fleet, paths)
        assert bot_dir == paths.bot_runtime("lead")

    def test_unit_filename_matches_bot_service(self, fleet_dir):
        """Unit filename must use service_prefix so fleet-pulse can find it."""
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        bot_dir = compose_bot(bot, fleet, paths)

        expected_name = f"{fleet.service_prefix}.{bot.bot_id}"

        # .service file uses service_prefix
        svc = bot_dir / f"{expected_name}.service"
        assert svc.is_file(), (
            f"expected {svc.name}, got {[f.name for f in bot_dir.glob('*.service')]}"
        )

        # .plist file uses service_prefix
        plist = bot_dir / f"{expected_name}.plist"
        assert plist.is_file(), (
            f"expected {plist.name}, got {[f.name for f in bot_dir.glob('*.plist')]}"
        )

        # BOT_SERVICE in bot.conf matches the unit name
        conf = (bot_dir / "bot.conf").read_text()
        for line in conf.splitlines():
            if line.strip().startswith("BOT_SERVICE="):
                val = line.split("=", 1)[1].strip().strip("'\"")
                assert val == expected_name, f"BOT_SERVICE={val!r} != {expected_name!r}"
                break
        else:
            raise AssertionError("BOT_SERVICE not found in bot.conf")


# ── compose_fleet ────────────────────────────────────────────────────


class TestComposeFleet:
    def test_composes_all_bots(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        result = compose_fleet(fleet, paths)
        assert "lead" in result
        assert "worker-1" in result
        assert all(p.is_dir() for p in result.values())

    def test_returns_dict_of_bot_dirs(self, fleet_dir):
        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        result = compose_fleet(fleet, paths)
        assert isinstance(result, dict)
        assert len(result) == 2  # lead + worker-1

    def test_written_units_carry_distinct_boot_rungs(self, fleet_dir):
        """The boot ladder reaches the unit ON DISK (#1002).

        Asserted against the real generate rather than the helper: the defect
        this guards is a compose path that never applies the rung, which a test
        that recomputes the delay itself and calls compose_systemd_unit by hand
        cannot see.
        """
        import re

        paths = _make_paths(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        result = compose_fleet(fleet, paths)

        rungs = {}
        for bot_id, bot_dir in result.items():
            unit = (bot_dir / f"{fleet.service_prefix}.{bot_id}.service").read_text()
            m = re.search(r"^ExecStartPre=/bin/sleep (\d+)$", unit, re.M)
            rungs[bot_id] = int(m.group(1)) if m else 0

        assert len(set(rungs.values())) == len(rungs), f"bots share a rung: {rungs}"
