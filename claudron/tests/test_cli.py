"""Tests for claudron CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudron.cli import main


class TestInit:
    def test_init_creates_vault(self, tmp_path: Path, capsys):
        target = tmp_path / "new-vault"
        rc = main(["init", str(target)])
        assert rc == 0
        assert (target / "_shared" / "knowledge").is_dir()
        out = capsys.readouterr().out
        assert "vault created" in out


class TestStatus:
    def test_status_output_human(self, vault_dir: Path, capsys):
        rc = main(["--vault", str(vault_dir), "status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "docs:" in out
        assert "shared/knowledge:" in out

    def test_status_output_json(self, vault_dir: Path, capsys):
        rc = main(["--vault", str(vault_dir), "status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "total_docs" in data
        assert data["total_docs"] == 2
        assert "tiers" in data


class TestLookup:
    def test_lookup_output_human(self, vault_dir: Path, capsys):
        rc = main(["--vault", str(vault_dir), "lookup", "auth"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Auth Patterns" in out

    def test_lookup_output_json(self, vault_dir: Path, capsys):
        rc = main(["--vault", str(vault_dir), "lookup", "--json", "auth"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "results" in data
        assert len(data["results"]) >= 1
        assert data["results"][0]["title"] == "Auth Patterns Across Services"

    def test_lookup_no_results(self, vault_dir: Path, capsys):
        rc = main(["--vault", str(vault_dir), "lookup", "nonexistent-topic-xyz"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no results" in out

    def test_no_vault_error_message(self, tmp_path: Path, capsys):
        nowhere = tmp_path / "nowhere"
        nowhere.mkdir()
        # Point --vault at a dir with no _shared/
        with pytest.raises(SystemExit) as exc_info:
            main(["--vault", str(nowhere), "lookup", "anything"])
        assert exc_info.value.code == 2


class TestVersion:
    def test_version_prints_semver(self, capsys):
        rc = main(["version"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "claudron" in out
        assert "0.1.0" in out
