"""Tests for migrate command."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from claudron.cli import main


@pytest.fixture
def migration_setup(tmp_path: Path):
    """Set up a claudlobby root with local/<fleet>/ and a vault with the fleet scaffolded."""
    # Claudlobby root
    cl_root = tmp_path / "claudlobby"
    (cl_root / "library").mkdir(parents=True)
    (cl_root / "lib").mkdir()

    # local/test-fleet/ with content
    fleet_local = cl_root / "local" / "test-fleet"
    (fleet_local / "shared" / "knowledge").mkdir(parents=True)
    (fleet_local / "shared" / "knowledge" / "deploy-tips.md").write_text(
        dedent("""\
        ---
        title: Deploy Tips
        type: knowledge
        status: active
        owner: alex
        tags: [deploy]
        created: 2026-04-01
        updated: 2026-05-01
        ---

        # Deploy Tips

        Always check health endpoints after deploy.
    """)
    )
    (fleet_local / "library" / "expertise").mkdir(parents=True)
    (fleet_local / "library" / "expertise" / "custom.md").write_text("# Custom\n")
    (fleet_local / "voices").mkdir()
    (fleet_local / "voices" / "custom-voice.md").write_text("# Custom Voice\n")

    # Memory in runtime/bots/
    bot_mem = fleet_local / "runtime" / "bots" / "astrid" / "memory"
    bot_mem.mkdir(parents=True)
    (bot_mem / "MEMORY.md").write_text("- [Note](note.md)\n")
    (bot_mem / "note.md").write_text("some memory\n")

    # Vault with fleet scaffolded
    vault = tmp_path / "vault"
    (vault / "_shared" / "knowledge").mkdir(parents=True)
    fleet_vault = vault / "test-fleet"
    fleet_vault.mkdir()
    (fleet_vault / "fleet.yaml").write_text("fleet:\n  name: test-fleet\n  bots: {}\n")
    (fleet_vault / "shared" / "knowledge").mkdir(parents=True)

    return cl_root, vault


class TestMigrateDryRun:
    def test_dry_run_shows_plan(self, migration_setup, capsys, monkeypatch):
        cl_root, vault = migration_setup
        monkeypatch.chdir(cl_root)

        rc = main(["--vault", str(vault), "migrate", "--fleet", "test-fleet"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "would copy" in out
        assert "would preserve" in out
        assert "(new)" in out
        assert "dry run" in out

        # Source untouched
        assert (
            cl_root / "local" / "test-fleet" / "shared" / "knowledge" / "deploy-tips.md"
        ).is_file()
        # Vault not modified
        assert not (
            vault / "test-fleet" / "shared" / "knowledge" / "deploy-tips.md"
        ).is_file()

    def test_dry_run_shows_overwrites(self, migration_setup, capsys, monkeypatch):
        cl_root, vault = migration_setup
        # Pre-populate a file in vault to trigger overwrite detection
        dst = vault / "test-fleet" / "shared" / "knowledge" / "deploy-tips.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("old content\n")

        monkeypatch.chdir(cl_root)
        rc = main(["--vault", str(vault), "migrate", "--fleet", "test-fleet"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "(overwrite)" in out

    def test_claudlobby_flag(self, migration_setup, capsys, tmp_path):
        """--claudlobby flag lets migrate find claudlobby root from any CWD."""
        cl_root, vault = migration_setup
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        rc = main(
            [
                "--vault",
                str(vault),
                "migrate",
                "--fleet",
                "test-fleet",
                "--claudlobby",
                str(cl_root),
            ]
        )
        assert rc == 0
        assert "dry run" in capsys.readouterr().out


class TestMigrateApply:
    def test_apply_copies_correctly(self, migration_setup, capsys, monkeypatch):
        cl_root, vault = migration_setup
        monkeypatch.chdir(cl_root)

        rc = main(
            ["--vault", str(vault), "migrate", "--fleet", "test-fleet", "--apply"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "migration complete" in out

        # Content copied to vault
        assert (
            vault / "test-fleet" / "shared" / "knowledge" / "deploy-tips.md"
        ).is_file()
        assert (vault / "test-fleet" / "library" / "expertise" / "custom.md").is_file()
        assert (vault / "test-fleet" / "voices" / "custom-voice.md").is_file()

        # Memory preserved
        assert (
            vault
            / "test-fleet"
            / "runtime"
            / "bots"
            / "astrid"
            / "memory"
            / "MEMORY.md"
        ).is_file()

        # Source untouched
        assert (
            cl_root / "local" / "test-fleet" / "shared" / "knowledge" / "deploy-tips.md"
        ).is_file()

    def test_apply_blocks_overwrite_without_force(
        self, migration_setup, capsys, monkeypatch
    ):
        cl_root, vault = migration_setup
        # Pre-populate to trigger overwrite
        dst = vault / "test-fleet" / "shared" / "knowledge" / "deploy-tips.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("old content\n")

        monkeypatch.chdir(cl_root)
        rc = main(
            ["--vault", str(vault), "migrate", "--fleet", "test-fleet", "--apply"]
        )
        assert rc == 1
        assert "overwritten" in capsys.readouterr().err

    def test_apply_force_overwrites(self, migration_setup, capsys, monkeypatch):
        cl_root, vault = migration_setup
        dst = vault / "test-fleet" / "shared" / "knowledge" / "deploy-tips.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("old content\n")

        monkeypatch.chdir(cl_root)
        rc = main(
            [
                "--vault",
                str(vault),
                "migrate",
                "--fleet",
                "test-fleet",
                "--apply",
                "--force",
            ]
        )
        assert rc == 0
        assert "migration complete" in capsys.readouterr().out
        # File was overwritten with new content
        assert "Deploy Tips" in dst.read_text()

    def test_source_not_found(self, tmp_path: Path, capsys, monkeypatch):
        cl_root = tmp_path / "cl"
        (cl_root / "library").mkdir(parents=True)
        (cl_root / "lib").mkdir()
        monkeypatch.chdir(cl_root)

        vault = tmp_path / "vault"
        (vault / "_shared").mkdir(parents=True)

        rc = main(["--vault", str(vault), "migrate", "--fleet", "nonexistent"])
        assert rc == 2
        assert "source not found" in capsys.readouterr().err
