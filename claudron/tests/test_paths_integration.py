"""Tests for Paths.detect() vault integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from claudlobby.paths import Paths


@pytest.fixture
def claudlobby_with_vault(tmp_path: Path):
    """Claudlobby root with .claudron pointing to a vault containing a fleet."""
    # Claudlobby root
    cl_root = tmp_path / "claudlobby"
    (cl_root / "library").mkdir(parents=True)
    (cl_root / "lib").mkdir()

    # Vault with fleet overlay
    vault = tmp_path / "vault"
    fleet_dir = vault / "my-fleet"
    fleet_dir.mkdir(parents=True)
    (vault / "_shared").mkdir()
    (fleet_dir / "fleet.yaml").write_text("fleet:\n  name: my-fleet\n  bots: {}\n")
    (fleet_dir / "shared" / "knowledge").mkdir(parents=True)
    (fleet_dir / "runtime" / "bots").mkdir(parents=True)

    # Write .claudron config
    (cl_root / ".claudron").write_text(f"vault={vault}\n")

    return cl_root, vault, fleet_dir


class TestPathsVaultDetection:
    def test_detect_resolves_through_claudron(self, claudlobby_with_vault):
        cl_root, vault, fleet_dir = claudlobby_with_vault
        paths = Paths.detect(hint=cl_root, fleet="my-fleet")
        assert paths.fleet_dir == fleet_dir
        assert paths.vault_root == vault
        assert paths.root == cl_root

    def test_fleet_yaml_from_vault(self, claudlobby_with_vault):
        cl_root, vault, fleet_dir = claudlobby_with_vault
        paths = Paths.detect(hint=cl_root, fleet="my-fleet")
        assert paths.fleet_yaml == fleet_dir / "fleet.yaml"

    def test_runtime_from_vault(self, claudlobby_with_vault):
        cl_root, vault, fleet_dir = claudlobby_with_vault
        paths = Paths.detect(hint=cl_root, fleet="my-fleet")
        assert paths.runtime == fleet_dir / "runtime"

    def test_shared_docs_from_vault(self, claudlobby_with_vault):
        cl_root, vault, fleet_dir = claudlobby_with_vault
        paths = Paths.detect(hint=cl_root, fleet="my-fleet")
        assert paths.shared_docs == fleet_dir / "shared"

    def test_falls_back_to_local(self, tmp_path: Path):
        """When .claudron exists but vault doesn't have the fleet, fall back to local/."""
        cl_root = tmp_path / "claudlobby"
        (cl_root / "library").mkdir(parents=True)
        (cl_root / "lib").mkdir()

        # Vault without the fleet we're looking for
        vault = tmp_path / "vault"
        (vault / "_shared").mkdir(parents=True)
        (cl_root / ".claudron").write_text(f"vault={vault}\n")

        # But local/ has it
        local_fleet = cl_root / "local" / "other-fleet"
        local_fleet.mkdir(parents=True)

        paths = Paths.detect(hint=cl_root, fleet="other-fleet")
        assert paths.fleet_dir == local_fleet
        assert paths.vault_root is None

    def test_requires_fleet_yaml_not_just_dir(self, tmp_path: Path):
        """Vault dir exists but has no fleet.yaml → falls back to local/."""
        cl_root = tmp_path / "claudlobby"
        (cl_root / "library").mkdir(parents=True)
        (cl_root / "lib").mkdir()

        vault = tmp_path / "vault"
        (vault / "_shared").mkdir(parents=True)
        # Dir exists but no fleet.yaml inside
        (vault / "bare-fleet").mkdir()
        (cl_root / ".claudron").write_text(f"vault={vault}\n")

        local_fleet = cl_root / "local" / "bare-fleet"
        local_fleet.mkdir(parents=True)

        paths = Paths.detect(hint=cl_root, fleet="bare-fleet")
        assert paths.fleet_dir == local_fleet
        assert paths.vault_root is None

    def test_no_claudron_config(self, tmp_path: Path):
        """Without .claudron, normal local/ resolution works."""
        cl_root = tmp_path / "claudlobby"
        (cl_root / "library").mkdir(parents=True)
        (cl_root / "lib").mkdir()
        local_fleet = cl_root / "local" / "normal-fleet"
        local_fleet.mkdir(parents=True)

        paths = Paths.detect(hint=cl_root, fleet="normal-fleet")
        assert paths.fleet_dir == local_fleet
        assert paths.vault_root is None

    def test_no_fleet_no_vault(self, tmp_path: Path):
        """Without --fleet, vault_root stays None."""
        cl_root = tmp_path / "claudlobby"
        (cl_root / "library").mkdir(parents=True)
        (cl_root / "lib").mkdir()

        paths = Paths.detect(hint=cl_root)
        assert paths.fleet_dir is None
        assert paths.vault_root is None


class TestComposeBotConfVaultMode:
    def test_bot_dir_absolute_when_outside_root(self, claudlobby_with_vault):
        """compose_bot_conf uses absolute path for BOT_DIR when bot_dir is outside claudlobby root."""
        from claudlobby.composer import compose_bot_conf
        from claudlobby.config import BotConfig, FleetConfig, TelegramConfig

        cl_root, vault, fleet_dir = claudlobby_with_vault
        paths = Paths(root=cl_root, fleet_dir=fleet_dir, vault_root=vault)
        bot = BotConfig(
            bot_id="testbot",
            name="testbot",
            expertise=["eng"],
            telegram=TelegramConfig(handle="testbot"),
        )
        fleet = FleetConfig(name="my-fleet", service_prefix="com.test")
        conf = compose_bot_conf(bot, fleet, paths)

        # BOT_DIR should be absolute (not $CLAUDLOBBY_ROOT-relative) since
        # fleet_dir is in the vault, outside claudlobby root
        for line in conf.splitlines():
            if line.startswith("BOT_DIR="):
                assert "$CLAUDLOBBY_ROOT" not in line
                assert str(fleet_dir / "runtime" / "bots" / "testbot") in line
                break
        else:
            pytest.fail("BOT_DIR line not found in bot.conf")
