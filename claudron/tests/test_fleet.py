"""Tests for fleet add/list commands."""

from __future__ import annotations

from pathlib import Path


from claudron.cli import main


class TestFleetAdd:
    def test_scaffolds_fleet_dir(self, vault_dir: Path, capsys):
        rc = main(["--vault", str(vault_dir), "fleet", "add", "my-fleet"])
        assert rc == 0

        fleet = vault_dir / "my-fleet"
        assert fleet.is_dir()
        assert (fleet / "fleet.yaml").is_file()
        assert (fleet / "shared" / "knowledge").is_dir()
        assert (fleet / "shared" / "decisions").is_dir()
        assert (fleet / "shared" / "runbooks").is_dir()
        assert (fleet / "shared" / "planning" / "active").is_dir()
        assert (fleet / "shared" / "planning" / "completed").is_dir()
        assert (fleet / ".env").is_file()
        assert (fleet / ".gitignore").is_file()

        # fleet.yaml has correct name
        content = (fleet / "fleet.yaml").read_text()
        assert "name: my-fleet" in content

        out = capsys.readouterr().out
        assert "scaffolded" in out

    def test_errors_if_exists(self, vault_dir: Path, capsys):
        main(["--vault", str(vault_dir), "fleet", "add", "dup-fleet"])
        capsys.readouterr()

        rc = main(["--vault", str(vault_dir), "fleet", "add", "dup-fleet"])
        assert rc == 2
        assert "already exists" in capsys.readouterr().err


class TestFleetList:
    def test_lists_fleets(self, vault_with_fleet: Path, capsys):
        rc = main(["--vault", str(vault_with_fleet), "fleet", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "test-fleet" in out

    def test_no_fleets(self, vault_dir: Path, capsys):
        rc = main(["--vault", str(vault_dir), "fleet", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no fleets" in out

    def test_lists_multiple_fleets(self, vault_dir: Path, capsys):
        # Add two fleets
        main(["--vault", str(vault_dir), "fleet", "add", "alpha"])
        main(["--vault", str(vault_dir), "fleet", "add", "beta"])
        capsys.readouterr()

        rc = main(["--vault", str(vault_dir), "fleet", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "beta" in out
