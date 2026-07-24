"""Tests for claudlobby doctor — pre-flight fleet health diagnostic."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby.claudron_compat import COMPAT_FLOOR
from claudlobby.config import load_fleet
from claudlobby.doctor import (
    DoctorReport,
    check_claudron,
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
    def test_warn_when_not_enrolled(self, doctor_fleet, monkeypatch):
        _, fleet, paths = doctor_fleet
        # CLI context (no FLEET_NAME): the resolver returns "" and the check
        # reconstructs service_name — no misconfig finding, just the enrollment warn.
        monkeypatch.delenv("FLEET_NAME", raising=False)
        report = DoctorReport()
        check_services(fleet, paths, report)
        # In test env, no systemd/launchd enrollment expected
        assert report.checks[0].status == "warn"
        assert "not enrolled" in report.checks[0].detail

    def test_tmux_check_uses_ssot_socket_from_bot_conf(self, doctor_fleet, monkeypatch):
        """The tmux check must use the socket resolved from the bot's bot.conf
        (SSOT), not one reconstructed from service_prefix.bot_id."""
        _, fleet, paths = doctor_fleet
        # bot.conf whose TMUX_SOCKET differs from the service_prefix.bot_id default.
        bot_dir = paths.bot_runtime("worker")
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "bot.conf").write_text(
            "BOT_NAME=worker\nBOT_SERVICE=com.test.worker\nTMUX_SOCKET=custom.sock.worker\n"
        )

        calls: list[list[str]] = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)

            class _R:
                returncode = 1
                stdout = ""
                stderr = ""

            return _R()

        monkeypatch.setattr("claudlobby.doctor.subprocess.run", fake_run)
        check_services(fleet, paths, DoctorReport())

        tmux_calls = [c for c in calls if c[:2] == ["tmux", "-L"]]
        assert tmux_calls, "expected a 'tmux -L … has-session' call"
        assert tmux_calls[0][2] == "custom.sock.worker"

    def test_surfaces_misconfigured_bot_when_fleet_name_set(
        self, doctor_fleet, monkeypatch
    ):
        """In a fleet context (FLEET_NAME set) the SSOT resolver fail-fasts on a
        bot with no resolvable socket; check_services must catch that, keep
        sweeping, and SURFACE it as a finding rather than silently reconstructing
        (so doctor doesn't report a misconfigured bot as healthy)."""
        _, fleet, paths = doctor_fleet  # worker has no bot.conf → resolver raises
        monkeypatch.setenv("FLEET_NAME", "test-fleet")
        report = DoctorReport()
        check_services(fleet, paths, report)  # must not raise
        socket_findings = [c for c in report.checks if c.name == "bot-sockets"]
        assert socket_findings, (
            "expected a bot-sockets finding for the misconfigured bot"
        )
        assert socket_findings[0].status == "fail"
        assert "worker" in socket_findings[0].detail


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
        monkeypatch.delenv("FLEET_NAME", raising=False)
        report = run_doctor(fleet, paths)
        check_names = [c.name for c in report.checks]
        assert "fleet-yaml" in check_names
        assert "env-vars" in check_names
        assert "mcp-configs" in check_names
        assert "services" in check_names
        assert "credentials" in check_names


class TestCheckClaudron:
    """The claudron door check `claudron_compat`'s docstring has promised since
    it was written (boundary phase L1)."""

    @staticmethod
    def _vault(tmp_path: Path, *, git: bool = False, hooks_log: str = "") -> Path:
        vault = tmp_path / "vault"
        (vault / "_shared").mkdir(parents=True, exist_ok=True)
        if hooks_log:
            (vault / ".claudron").mkdir(exist_ok=True)
            (vault / ".claudron" / "hooks.log").write_text(hooks_log)
        if git:
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-q", "--allow-empty",
                 "-m", "seed", "--no-gpg-sign"],
                check=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@e",
                    "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@e",
                },
            )
        return vault

    @staticmethod
    def _wire(root: Path, vault: Path) -> "FleetConfig":
        raw = (root / "fleet.yaml").read_text()
        text = raw.replace(
            "      expertise: [eng]",
            f"      expertise: [eng]\n      claudron_vault_path: {vault}",
        )
        assert text != raw, "fleet.yaml fixture shape changed — wiring no-oped"
        (root / "fleet.yaml").write_text(text)
        fleet, _md = load_fleet(root / "fleet.yaml")
        return fleet

    @staticmethod
    def _stub_cli(tmp_path: Path, monkeypatch, *, body: str) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        stub = bindir / "claudron"
        stub.write_text(body)
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def test_silent_for_a_fleet_with_no_vault_wired_bot(self, doctor_fleet):
        _, fleet, paths = doctor_fleet
        report = DoctorReport()
        check_claudron(fleet, paths, report)
        assert report.checks == []

    def test_cli_absent_warns_and_names_the_door(
        self, doctor_fleet, tmp_path, monkeypatch
    ):
        root, _fleet, paths = doctor_fleet
        fleet = self._wire(root, self._vault(tmp_path))
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        report = DoctorReport()
        check_claudron(fleet, paths, report)
        cli = [c for c in report.checks if c.name == "claudron-cli"][0]
        assert cli.status == "warn"
        assert "INTEGRATION.md" in cli.detail

    def test_floor_rows_render_parked_never_unmet(
        self, doctor_fleet, tmp_path, monkeypatch
    ):
        root, _fleet, paths = doctor_fleet
        fleet = self._wire(root, self._vault(tmp_path))
        # A stub CLI that answers the capability probe and every `--help`.
        self._stub_cli(
            tmp_path,
            monkeypatch,
            body=(
                "#!/bin/sh\n"
                'if [ "$1" = "status" ]; then\n'
                '  echo \'{"ok":true,"command":"status","data":'
                '{"engine_version":"0.3.0","root":"/v","total_docs":7}}\'\n'
                "fi\n"
                "exit 0\n"
            ),
        )
        report = DoctorReport()
        check_claudron(fleet, paths, report)

        floor = [c for c in report.checks if c.name.startswith("claudron-floor:")]
        assert len(floor) == len(COMPAT_FLOOR)

        parked = [c for c in floor if "parked" in c.detail]
        assert parked, [c.detail for c in floor]
        for check in parked:
            assert "decision C" in check.detail
            assert "unmet" not in check.detail
            assert check.status == "pass"
        # No row for a deliberately-unshipped surface may read "unmet".
        for check in floor:
            if "unmet" in check.detail:
                assert "parked" not in check.detail

        engine = [c for c in report.checks if c.name == "claudron-engine"][0]
        assert engine.status == "pass"
        assert "engine 0.3.0" in engine.detail

    def test_probe_reports_exit_3_as_no_vault(
        self, doctor_fleet, tmp_path, monkeypatch
    ):
        root, _fleet, paths = doctor_fleet
        fleet = self._wire(root, self._vault(tmp_path))
        self._stub_cli(
            tmp_path,
            monkeypatch,
            body='#!/bin/sh\n[ "$1" = "status" ] && exit 3\nexit 0\n',
        )
        report = DoctorReport()
        check_claudron(fleet, paths, report)
        engine = [c for c in report.checks if c.name == "claudron-engine"][0]
        assert engine.status == "warn"
        assert "no vault resolved" in engine.detail

    def test_loop_evidence_surfaces_recent_hook_degradation(
        self, doctor_fleet, tmp_path, monkeypatch
    ):
        root, _fleet, paths = doctor_fleet
        stamp = datetime.now().isoformat(timespec="seconds")
        vault = self._vault(
            tmp_path,
            git=True,
            hooks_log=f"{stamp} [session-end] sync --push degraded: offline\n",
        )
        fleet = self._wire(root, vault)
        self._stub_cli(tmp_path, monkeypatch, body="#!/bin/sh\nexit 0\n")
        report = DoctorReport()
        check_claudron(fleet, paths, report)
        loop = [c for c in report.checks if c.name == "claudron-loop"]
        assert len(loop) == 1
        assert loop[0].status == "warn"
        assert "sync --push degraded" in loop[0].detail
        assert "last commit" in loop[0].detail

    def test_loop_evidence_passes_on_a_quiet_healthy_vault(
        self, doctor_fleet, tmp_path, monkeypatch
    ):
        root, _fleet, paths = doctor_fleet
        fleet = self._wire(root, self._vault(tmp_path, git=True))
        self._stub_cli(tmp_path, monkeypatch, body="#!/bin/sh\nexit 0\n")
        report = DoctorReport()
        check_claudron(fleet, paths, report)
        loop = [c for c in report.checks if c.name == "claudron-loop"][0]
        assert loop.status == "pass"
        assert "no hook degradation logged" in loop.detail

    def test_loop_evidence_warns_when_the_vault_is_absent(
        self, doctor_fleet, tmp_path, monkeypatch
    ):
        root, _fleet, paths = doctor_fleet
        fleet = self._wire(root, tmp_path / "nowhere")
        self._stub_cli(tmp_path, monkeypatch, body="#!/bin/sh\nexit 0\n")
        report = DoctorReport()
        check_claudron(fleet, paths, report)
        loop = [c for c in report.checks if c.name == "claudron-loop"][0]
        assert loop.status == "warn"
        assert "not present on this host" in loop.detail
