"""Tests for validator.py — validate() with missing env vars and library refs."""

from __future__ import annotations

from pathlib import Path


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

    def test_unreadable_integration_skips_with_warning(self, fleet_dir, caplog):
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

        # Verify the warning was emitted via logging
        assert "skipping" in caplog.text

    def test_empty_bots_is_error(self, fleet_dir):
        (fleet_dir / "fleet.yaml").write_text("fleet:\n  name: empty\n  bots: {}\n")
        fleet = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert report.has_errors
        assert any("empty" in e for e in report.errors)

    def test_reports_to_invalid_ref_is_warning(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [software-engineering]",
            "expertise: [software-engineering]\n      reports_to: nonexistent-bot",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        fleet = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any(
            "reports_to" in w and "nonexistent-bot" in w for w in report.warnings
        )

    def test_manages_invalid_ref_is_warning(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [orchestration]",
            "expertise: [orchestration]\n      manages: [worker-1, ghost-bot]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        fleet = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("manages" in w and "ghost-bot" in w for w in report.warnings)
        # worker-1 exists, so no warning for it
        assert not any("manages" in w and "worker-1" in w for w in report.warnings)

    def test_tools_deny_conflicts_with_expertise(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [software-engineering]",
            "expertise: [software-engineering]\n      tools:\n        deny: [Write, Edit]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        fleet = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any(
            "tools.deny" in w and "software-engineering" in w for w in report.warnings
        )

    def test_tools_deny_no_conflict_for_reviewer(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        monkeypatch.setenv("GITHUB_PAT", "ghp_test123")
        # Add code-review expertise file
        (fleet_dir / "library" / "expertise" / "code-review.md").write_text(
            "# Reviewer\n\nReview PRs.\n"
        )
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [software-engineering]",
            "expertise: [code-review]\n      tools:\n        deny: [Write, Edit]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        fleet = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        # code-review has no core tools that conflict with Write/Edit deny
        assert not any("tools.deny" in w for w in report.warnings)

    def test_tools_allow_deny_overlap_warns(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [software-engineering]",
            "expertise: [software-engineering]\n      tools:\n        deny: [Write]\n        allow: [Write, Read]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        fleet = load_fleet(fleet_dir / "fleet.yaml")
        paths = _make_paths(fleet_dir)
        report = validate(fleet, paths)
        assert any("both allow and deny" in w for w in report.warnings)
