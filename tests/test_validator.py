"""Tests for validator.py — validate() with missing env vars and library refs."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from claudlobby.validator import validate


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=None)


class TestValidate:
    def test_valid_fleet_no_errors(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_test123")
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        fleet = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert not report.has_errors

    def test_missing_expertise_is_error(self, fleet_dir, monkeypatch):
        # Overwrite fleet.yaml with a bot referencing nonexistent expertise
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace("orchestration", "nonexistent-role")
        (fleet_dir / "fleet.yaml").write_text(yaml_text)

        fleet = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert report.has_errors
        assert any("nonexistent-role" in e for e in report.errors)

    def test_missing_env_var_is_warning(self, fleet_dir, monkeypatch):
        # Ensure GITHUB_PAT is NOT set
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        monkeypatch.delenv("TELEGRAM_TOKEN_LEAD", raising=False)
        monkeypatch.delenv("TELEGRAM_TOKEN_WORKER1", raising=False)

        # Give the lead bot the github MCP so it needs GITHUB_PAT
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [orchestration]",
            "expertise: [orchestration]\n      mcp: [github]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)

        fleet = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("GITHUB_PAT" in w for w in report.warnings)

    def test_missing_telegram_token_is_warning(self, fleet_dir, monkeypatch):
        monkeypatch.delenv("TELEGRAM_TOKEN_LEAD", raising=False)
        fleet = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("TELEGRAM_TOKEN_LEAD" in w for w in report.warnings)

    def test_unreadable_integration_skips_with_warning(self, fleet_dir, capsys):
        # Give lead bot an explicit integration so validator walks it
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [orchestration]",
            "expertise: [orchestration]\n      integrations: [github]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)

        # Make the integration file unreadable
        int_file = fleet_dir / "library" / "integrations" / "github.md"
        int_file.chmod(0o000)

        fleet = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        # Must not crash — OSError is caught
        report = validate(fleet, paths)
        assert not report.has_errors

        # Restore permissions for cleanup
        int_file.chmod(0o644)

        # Verify the warning was printed to stderr
        captured = capsys.readouterr()
        assert "WARN" in captured.err
        assert "skipping" in captured.err

    def test_empty_bots_is_error(self, fleet_dir):
        (fleet_dir / "fleet.yaml").write_text(
            "fleet:\n  name: empty\n  bots: {}\n"
        )
        fleet = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert report.has_errors
        assert any("empty" in e for e in report.errors)
