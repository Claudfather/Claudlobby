"""Tests for claudlobby doctor — pre-flight fleet health diagnostic."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby.config import load_fleet
from claudlobby.doctor import (
    DoctorReport,
    check_env_vars,
    check_mcp_configs,
    check_services,
    format_report,
    run_doctor,
)
from claudlobby.paths import Paths


@pytest.fixture
def doctor_fleet(tmp_path: Path) -> tuple[Path, "FleetConfig", Paths]:
    """Minimal fleet layout for doctor tests."""
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
    """)
    )

    for kind in (
        "expertise",
        "mcp",
        "integrations",
        "guardrails",
        "protocols",
        "skills",
        "resources",
        "lessons",
    ):
        (root / "library" / kind).mkdir(parents=True)

    (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    (root / "library" / "mcp" / "github.json").write_text(
        json.dumps(
            {
                "github": {"command": "gh", "args": ["mcp"]},
                "_env_contract": {
                    "GITHUB_PAT": {"description": "GitHub PAT", "tier": "fleet"},
                },
            }
        )
    )

    (root / "templates").mkdir()
    (root / "templates" / "claude.md.j2").write_text("# {{ bot.name }}\n")
    (root / "runtime" / "bots").mkdir(parents=True)
    (root / "lib").mkdir()

    paths = Paths(root=root, fleet_dir=root)
    fleet, _md = load_fleet(root / "fleet.yaml")
    return root, fleet, paths


class TestCheckEnvVars:
    def test_pass_when_all_present(self, doctor_fleet, monkeypatch):
        _, fleet, paths = doctor_fleet
        monkeypatch.setenv("GITHUB_PAT", "ghp_test123")
        report = DoctorReport()
        check_env_vars(fleet, paths, report)
        assert report.checks[0].status == "pass"
        assert "1 contracted" in report.checks[0].detail

    def test_fail_when_missing(self, doctor_fleet, monkeypatch):
        _, fleet, paths = doctor_fleet
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        report = DoctorReport()
        check_env_vars(fleet, paths, report)
        assert report.checks[0].status == "fail"
        assert "GITHUB_PAT" in report.checks[0].detail

    def test_warn_when_empty(self, doctor_fleet, monkeypatch):
        _, fleet, paths = doctor_fleet
        monkeypatch.setenv("GITHUB_PAT", "")
        report = DoctorReport()
        check_env_vars(fleet, paths, report)
        assert report.checks[0].status == "warn"
        assert "empty" in report.checks[0].detail


class TestCheckMcpConfigs:
    def test_pass_when_fragments_exist(self, doctor_fleet):
        _, fleet, paths = doctor_fleet
        report = DoctorReport()
        check_mcp_configs(fleet, paths, report)
        assert report.checks[0].status == "pass"

    def test_fail_when_fragment_missing(self, doctor_fleet):
        root, fleet, paths = doctor_fleet
        (root / "library" / "mcp" / "github.json").unlink()
        report = DoctorReport()
        check_mcp_configs(fleet, paths, report)
        assert report.checks[0].status == "fail"
        assert "github" in report.checks[0].detail


class TestCheckServices:
    def test_warn_when_not_enrolled(self, doctor_fleet):
        _, fleet, paths = doctor_fleet
        report = DoctorReport()
        check_services(fleet, paths, report)
        # In test env, no systemd/launchd enrollment expected
        assert report.checks[0].status == "warn"
        assert "not enrolled" in report.checks[0].detail


class TestFormatReport:
    def test_format_shows_pass_fail_counts(self):
        report = DoctorReport()
        report.add("env-vars", "pass", "all good")
        report.add("services", "fail", "3 down")
        output = format_report(report)
        assert "[PASS] env-vars" in output
        assert "[FAIL] services" in output
        assert "1 passed" in output
        assert "1 failures" in output


class TestRunDoctor:
    def test_returns_report_with_all_checks(self, doctor_fleet, monkeypatch):
        _, fleet, paths = doctor_fleet
        monkeypatch.setenv("GITHUB_PAT", "ghp_test123")
        report = run_doctor(fleet, paths)
        check_names = [c.name for c in report.checks]
        assert "fleet-yaml" in check_names
        assert "env-vars" in check_names
        assert "mcp-configs" in check_names
        assert "services" in check_names
        assert "credentials" in check_names
