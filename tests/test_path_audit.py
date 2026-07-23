"""Tests for path_audit.py — the compositor path-ownership guard.

Extends the fresh-box self-containment contract to cover PATHS: no emitted wiring
file may carry a flat/dangling/cross-fleet absolute path. A path written against a
composer anchor (FLEET_ROOT / BOT_DIR / CLAUDLOBBY_ROOT) resolves to the fleet's
real nested location and passes; a hand-typed flat husk fails.
"""

from __future__ import annotations

import json

import pytest

from claudlobby.config import BotConfig, FleetConfig, TelegramConfig
from claudlobby.path_audit import (
    COMPOSER_PROVIDED_PATH_ANCHORS,
    PathFinding,
    assert_bot_paths,
    audit_bot_paths,
    improper_fleet_paths,
)
from claudlobby.paths import Paths


def _paths(tmp_path):
    root = tmp_path / "claudlobby"
    fleet_dir = root / "local" / "home" / "tl"
    (fleet_dir / "runtime" / "bots" / "kev").mkdir(parents=True)
    (root / "lib").mkdir(parents=True)
    return Paths(root=root, fleet_dir=fleet_dir)


def _bot():
    return BotConfig(
        bot_id="kev",
        name="kev",
        expertise=["eng"],
        telegram=TelegramConfig(handle="kev_bot"),
    )


def _fleet():
    return FleetConfig(name="tl", service_prefix="com.crog.tl")


class TestImproperFleetPaths:
    def test_flat_husk_is_flagged(self, tmp_path):
        paths = _paths(tmp_path)
        flat = f"{paths.root}/local/tl/.secrets/ga4.json"  # local/tl not local/home/tl
        bad = improper_fleet_paths(f'"{flat}"', _bot(), paths)
        assert [p for p, _ in bad] == [flat]

    def test_nested_correct_absolute_is_ok(self, tmp_path):
        paths = _paths(tmp_path)
        good = f"{paths.root}/local/home/tl/.secrets/ga4.json"
        assert improper_fleet_paths(good, _bot(), paths) == []

    def test_fleet_root_token_is_ok(self, tmp_path):
        paths = _paths(tmp_path)
        txt = "${FLEET_ROOT}/runtime/bots/kev/data/printify-mcp/dist/index.js"
        assert improper_fleet_paths(txt, _bot(), paths) == []

    def test_claudlobby_root_anchored_bot_dir_is_ok(self, tmp_path):
        paths = _paths(tmp_path)
        txt = 'BOT_DIR="$CLAUDLOBBY_ROOT/local/home/tl/runtime/bots/kev"'
        assert improper_fleet_paths(txt, _bot(), paths) == []

    def test_bot_dir_token_is_ok(self, tmp_path):
        paths = _paths(tmp_path)
        txt = "${BOT_DIR}/data/printify-mcp/dist/index.js"
        assert improper_fleet_paths(txt, _bot(), paths) == []

    def test_system_and_home_paths_ignored(self, tmp_path):
        paths = _paths(tmp_path)
        txt = "/usr/bin/node --flag /tmp/scratch $HOME/.claude/settings.json"
        assert improper_fleet_paths(txt, _bot(), paths) == []

    def test_cross_fleet_path_flagged(self, tmp_path):
        paths = _paths(tmp_path)
        other = f"{paths.root}/local/home/other-fleet/runtime/bots/z/x.js"
        bad = improper_fleet_paths(other, _bot(), paths)
        assert [p for p, _ in bad] == [other]

    def test_fleet_state_path_style_root_anchor_is_ok(self, tmp_path):
        # $CLAUDLOBBY_ROOT/state/... resolves outside local/ entirely — never flagged.
        paths = _paths(tmp_path)
        txt = 'export FLEET_STATE_PATH="$CLAUDLOBBY_ROOT/state/fleet-state.json"'
        assert improper_fleet_paths(txt, _bot(), paths) == []


class TestAuditBotPaths:
    def _seed_bot_dir(self, paths):
        bot_dir = paths.bot_runtime("kev")
        (bot_dir / ".claude").mkdir(parents=True, exist_ok=True)
        return bot_dir

    def test_clean_bot_dir_no_findings(self, tmp_path):
        paths = _paths(tmp_path)
        bot_dir = self._seed_bot_dir(paths)
        (bot_dir / "bot.conf").write_text(
            'export FLEET_ROOT="$CLAUDLOBBY_ROOT/local/home/tl"\n'
            f"export FLEET_MISSION_FILE={paths.root}/local/home/tl/missions/f.md\n"
        )
        (bot_dir / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "printify": {
                            "args": [
                                "${FLEET_ROOT}/runtime/bots/kev/data/printify-mcp/dist/index.js"
                            ]
                        }
                    }
                }
            )
        )
        assert audit_bot_paths(_bot(), _fleet(), paths) == []
        assert_bot_paths(_bot(), _fleet(), paths)  # does not raise

    def test_flat_path_in_mcp_json_is_a_finding_and_raises(self, tmp_path):
        paths = _paths(tmp_path)
        bot_dir = self._seed_bot_dir(paths)
        flat = f"{paths.root}/local/tl/runtime/bots/kev/dist/index.js"  # flat husk
        (bot_dir / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"printify": {"args": [flat]}}})
        )
        findings = audit_bot_paths(_bot(), _fleet(), paths)
        assert len(findings) == 1
        assert isinstance(findings[0], PathFinding)
        assert findings[0].file == ".mcp.json"
        assert findings[0].path == flat
        with pytest.raises(ValueError, match="improper absolute fleet path"):
            assert_bot_paths(_bot(), _fleet(), paths)

    def test_flat_path_in_bot_conf_is_flagged(self, tmp_path):
        paths = _paths(tmp_path)
        bot_dir = self._seed_bot_dir(paths)
        (bot_dir / "bot.conf").write_text(
            f"export GA4_SA_KEY_PATH={paths.root}/local/tl/.secrets/ga4.json\n"
        )
        findings = audit_bot_paths(_bot(), _fleet(), paths)
        assert [f.file for f in findings] == ["bot.conf"]

    def test_missing_wiring_files_are_skipped(self, tmp_path):
        # A bot with no .mcp.json / units on disk audits clean, not crashing.
        paths = _paths(tmp_path)
        self._seed_bot_dir(paths)
        assert audit_bot_paths(_bot(), _fleet(), paths) == []


def test_anchor_ssot_is_the_three_blessed_anchors():
    assert set(COMPOSER_PROVIDED_PATH_ANCHORS) == {
        "CLAUDLOBBY_ROOT",
        "FLEET_ROOT",
        "BOT_DIR",
    }


def _write_nested_fleet(root, env_path_value):
    """A nested-overlay fleet (local/home/tl) reusing the fleet_dir fixture's
    root-level library, with one bot carrying a hand-typed env path."""
    fleet_dir = root / "local" / "home" / "tl"
    fleet_dir.mkdir(parents=True)
    (fleet_dir / "fleet.yaml").write_text(
        "fleet:\n"
        "  name: tl\n"
        "  service_prefix: com.crog.tl\n"
        '  telegram_group_chat_id: "-100999"\n'
        "  accounts:\n"
        "    default: ~/.claude\n"
        "  bots:\n"
        "    kev:\n"
        "      expertise: [software-engineering]\n"
        "      env:\n"
        f'        DANGLING_PATH: "{env_path_value}"\n'
        "      telegram:\n"
        "        handle: kev_bot\n"
        "        token_env: T\n"
    )
    return fleet_dir


class TestComposeBotFiresPathGuard:
    """End-to-end: compose_bot invokes the guard, so a hand-typed flat fleet path
    anywhere in the composed wiring fails the generate loudly."""

    def test_flat_env_path_makes_compose_bot_raise(self, fleet_dir):
        from claudlobby.composer import compose_bot
        from claudlobby.config import load_fleet

        root = fleet_dir
        flat = f"{root}/local/tl/data/x"  # flat husk: local/tl not local/home/tl
        nested = _write_nested_fleet(root, flat)
        paths = Paths(root=root, fleet_dir=nested)
        fleet, _ = load_fleet(nested / "fleet.yaml")
        with pytest.raises(ValueError, match="improper absolute fleet path"):
            compose_bot(fleet.bots["kev"], fleet, paths)

    def test_nested_correct_env_path_composes_clean(self, fleet_dir):
        from claudlobby.composer import compose_bot
        from claudlobby.config import load_fleet

        root = fleet_dir
        good = f"{root}/local/home/tl/data/x"  # nested-correct → allowed
        nested = _write_nested_fleet(root, good)
        paths = Paths(root=root, fleet_dir=nested)
        fleet, _ = load_fleet(nested / "fleet.yaml")
        bot_dir = compose_bot(fleet.bots["kev"], fleet, paths)
        assert bot_dir.is_dir()
