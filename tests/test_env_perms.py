"""Tests for .env file permission hardening (0o600).

Verifies that all .env write sites in composer.py and __main__.py
restrict files to owner-only read/write (mode 0o600), preventing
world-readable fleet secrets on shared hosts.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from textwrap import dedent

from claudlobby.config import FleetConfig, BotConfig, TelegramConfig, load_fleet
from claudlobby.composer import scaffold_env_files
from claudlobby.paths import Paths


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root)


class TestScaffoldEnvFilePermissions:
    """scaffold_env_files writes .env with mode 0o600."""

    def _setup_fleet(self, tmp_path: Path) -> tuple[Path, Paths]:
        """Minimal fleet layout with MCP env contract."""
        root = tmp_path / "claudlobby"
        root.mkdir()

        (root / "fleet.yaml").write_text(
            dedent("""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                worker:
                  expertise: [eng]
                  mcp: [github]
                  telegram:
                    handle: w_bot
                    token_env: TG_TOKEN_W
        """)
        )

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

        (root / "runtime" / "bots" / "worker").mkdir(parents=True)

        paths = _make_paths(root)
        return root, paths

    def test_fleet_env_has_0o600(self, tmp_path):
        """Fleet .env created by scaffold_env_files has mode 0o600."""
        root, paths = self._setup_fleet(tmp_path)
        fleet, _md = load_fleet(root / "fleet.yaml")

        scaffold_env_files(fleet, paths, log=lambda m: None)

        env_path = root / ".env"
        assert env_path.is_file()
        mode = stat.S_IMODE(env_path.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_bot_env_has_0o600(self, tmp_path):
        """Bot .env created by scaffold_env_files has mode 0o600."""
        root, paths = self._setup_fleet(tmp_path)

        # Add a bot-tier contract so a bot .env gets created
        (root / "library" / "mcp" / "telegram.json").write_text(
            json.dumps(
                {
                    "telegram": {"command": "tg-mcp", "args": []},
                    "_env_contract": {
                        "TG_TOKEN_W": {
                            "description": "Telegram bot token",
                            "default_tier": "bot",
                        },
                    },
                }
            )
        )

        # Update fleet.yaml to include telegram MCP
        (root / "fleet.yaml").write_text(
            dedent("""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                worker:
                  expertise: [eng]
                  mcp: [github, telegram]
                  telegram:
                    handle: w_bot
                    token_env: TG_TOKEN_W
        """)
        )

        fleet, _md = load_fleet(root / "fleet.yaml")
        scaffold_env_files(fleet, paths, log=lambda m: None)

        bot_env = root / "runtime" / "bots" / "worker" / ".env"
        assert bot_env.is_file()
        mode = stat.S_IMODE(bot_env.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_existing_env_gets_tightened_on_rewrite(self, tmp_path):
        """A pre-existing world-readable .env gets chmod'd to 0o600 on re-scaffold."""
        root, paths = self._setup_fleet(tmp_path)
        fleet, _md = load_fleet(root / "fleet.yaml")

        # Create a world-readable .env (simulating legacy state)
        env_path = root / ".env"
        env_path.write_text("# old\nexport GITHUB_PAT=ghp_test\n")
        env_path.chmod(0o644)

        scaffold_env_files(fleet, paths, log=lambda m: None)

        mode = stat.S_IMODE(env_path.stat().st_mode)
        assert mode == 0o600, f"expected 0o600 after re-scaffold, got {oct(mode)}"


class TestEnvMigratePermissions:
    """_apply_env_migration writes .env files with mode 0o600."""

    def test_fleet_env_migrate_has_0o600(self, tmp_path):
        """Fleet .env written by _apply_env_migration has mode 0o600."""
        from claudlobby.commands.env_migrate import _apply_env_migration

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "runtime" / "bots" / "worker").mkdir(parents=True)
        paths = _make_paths(root)

        fleet = FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            bots={
                "worker": BotConfig(
                    bot_id="worker",
                    name="worker",
                    expertise=["eng"],
                    telegram=TelegramConfig(handle="w_bot", token_env="TG_TOKEN_W"),
                )
            },
        )

        fleet_vars = {"GITHUB_PAT": "ghp_xxxxxxxxxxxxxxxxxxxx"}
        bot_vars = {"worker": {"TG_TOKEN_W": "123456:AAAA"}}

        _apply_env_migration(fleet, paths, Path("/fake/source"), fleet_vars, bot_vars)

        fleet_env = root / ".env"
        assert fleet_env.is_file()
        mode = stat.S_IMODE(fleet_env.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_bot_env_migrate_has_0o600(self, tmp_path):
        """Bot .env written by _apply_env_migration has mode 0o600."""
        from claudlobby.commands.env_migrate import _apply_env_migration

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "runtime" / "bots" / "worker").mkdir(parents=True)
        paths = _make_paths(root)

        fleet = FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            bots={
                "worker": BotConfig(
                    bot_id="worker",
                    name="worker",
                    expertise=["eng"],
                    telegram=TelegramConfig(handle="w_bot", token_env="TG_TOKEN_W"),
                )
            },
        )

        fleet_vars = {}
        bot_vars = {"worker": {"TG_TOKEN_W": "123456:AAAA"}}

        _apply_env_migration(fleet, paths, Path("/fake/source"), fleet_vars, bot_vars)

        bot_env = root / "runtime" / "bots" / "worker" / ".env"
        assert bot_env.is_file()
        mode = stat.S_IMODE(bot_env.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
