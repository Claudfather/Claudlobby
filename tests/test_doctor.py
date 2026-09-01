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
                    # `secret` required on every entry since #1214 Phase 1.
                    "GITHUB_PAT": {
                        "description": "GitHub PAT",
                        "default_tier": "fleet",
                        "secret": True,
                    },
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
                [
                    "git",
                    "-C",
                    str(vault),
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "seed",
                    "--no-gpg-sign",
                ],
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


class TestDoctorTimerScriptParity:
    """`claudlobby doctor` mirrors `generate` for the L1 deny-by-default timer
    rule: a fleet job whose ``script`` is a foreign absolute fails the rollout
    `generate` (compose_fleet_timers), so doctor's fleet-yaml check must fail too.
    validate reads the jobs off ``fleet.defaults``, so every surface that runs it —
    doctor included — catches the denial without any per-call-site threading."""

    def _fleet(self, doctor_fleet, monkeypatch):
        _, fleet, paths = doctor_fleet
        monkeypatch.setenv("GITHUB_PAT", "ghp_test123")
        monkeypatch.delenv("FLEET_NAME", raising=False)
        return fleet, paths

    def test_doctor_fails_on_foreign_absolute_timer_script(
        self, doctor_fleet, monkeypatch
    ):
        fleet, paths = self._fleet(doctor_fleet, monkeypatch)
        fleet.defaults["jobs"] = {
            "rogue": {"script": "/opt/rogue/job.sh", "schedule": "daily"}
        }
        report = run_doctor(fleet, paths)
        fleet_yaml = next(c for c in report.checks if c.name == "fleet-yaml")
        assert fleet_yaml.status == "fail"

    def test_doctor_passes_on_anchored_timer_script(self, doctor_fleet, monkeypatch):
        fleet, paths = self._fleet(doctor_fleet, monkeypatch)
        fleet.defaults["jobs"] = {
            "vitals": {"script": "$CLAUDLOBBY_ROOT/lib/x.sh", "schedule": "daily"}
        }
        report = run_doctor(fleet, paths)
        fleet_yaml = next(c for c in report.checks if c.name == "fleet-yaml")
        assert fleet_yaml.status != "fail"


class TestCheckCredentialsScoping:
    """#1377 — probe only what the fleet declares, resolved through the cascade.

    The load-bearing assertion in this class is `_curl_with_config` NEVER being
    called. Reading the report text proves the verdict changed; it does not
    prove the outbound call stopped, and the outbound call IS the defect. So the
    transport is monkeypatched with a recorder that fails the test if it fires.
    """

    @staticmethod
    def _stage_cascade(paths, monkeypatch):
        """Stage the REAL runtime resolver and an isolated HOST tier.

        Required by every test in this class that expects a value decision.
        `Paths.env_resolved` REFUSES rather than falling back when it cannot
        reach `lib/env-tiers.sh`, so without this the function short-circuits to
        its resolver-unavailable branch and an absence-assertion passes for the
        wrong reason — which is exactly what happened while writing these.
        A stub resolver is not an option: it would certify a cascade the runtime
        does not have (tests/test_credentials.py makes the same call).
        """
        repo = Path(__file__).resolve().parent.parent
        (paths.root / "lib").mkdir(parents=True, exist_ok=True)
        for f in ("lib-common.sh", "env-tiers.sh"):
            (paths.root / "lib" / f).write_bytes((repo / "lib" / f).read_bytes())
        fake_home = paths.root.parent / "home"
        fake_home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(fake_home))

    @staticmethod
    def _no_network(monkeypatch):
        """Replace the transport with a tripwire. Returns the call log."""
        calls: list = []

        def _boom(headers, extra_args):
            calls.append(extra_args)
            raise AssertionError(
                f"check_credentials made an outbound call it should not have: {extra_args}"
            )

        monkeypatch.setattr("claudlobby.doctor._curl_with_config", _boom)
        return calls

    def test_ambient_token_for_an_undeclared_integration_is_never_probed(
        self, doctor_fleet, monkeypatch
    ):
        """The #1377 reproduction: the fleet declares github, never railway."""
        _, fleet, paths = doctor_fleet
        self._no_network(monkeypatch)
        self._stage_cascade(paths, monkeypatch)
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_ambient_never_declared")
        monkeypatch.delenv("GITHUB_PAT", raising=False)

        report = DoctorReport()
        from claudlobby.doctor import check_credentials

        check_credentials(fleet, paths, report)

        # No call fired (the tripwire would have raised), and the fleet's
        # verdict no longer mentions a service it does not use.
        assert "RAILWAY" not in report.checks[0].detail.upper()
        assert report.checks[0].status != "fail"

    def test_a_declared_var_present_only_in_the_shell_is_named_not_probed(
        self, doctor_fleet, monkeypatch
    ):
        """A bot resolves from the cascade, not the operator's shell.

        Probing the shell value would report a health the fleet does not have.
        Dropping it silently would hide a state that genuinely confuses people.
        So it is reported and not probed.
        """
        _, fleet, paths = doctor_fleet
        self._no_network(monkeypatch)
        self._stage_cascade(paths, monkeypatch)
        monkeypatch.setenv("GITHUB_PAT", "ghp_only_in_my_shell")

        report = DoctorReport()
        from claudlobby.doctor import check_credentials

        check_credentials(fleet, paths, report)
        detail = report.checks[0].detail
        assert "GITHUB_PAT" in detail
        assert "shell" in detail and "not probed" in detail

    def test_declared_with_a_cascade_value_IS_probed(self, doctor_fleet, monkeypatch):
        """The positive control.

        Every other test here asserts an absence, and a function that had simply
        stopped working would pass all of them. This one proves the probe still
        fires for the case it is supposed to serve.
        """
        _, fleet, paths = doctor_fleet
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        self._stage_cascade(paths, monkeypatch)
        (paths.root / ".env").write_text("GITHUB_PAT=ghp_in_the_cascade\n")

        seen: list = []

        class _R:
            stdout = "200"
            returncode = 0

        def _fake(headers, extra_args):
            seen.append((headers, extra_args))
            return _R()

        monkeypatch.setattr("claudlobby.doctor._curl_with_config", _fake)

        report = DoctorReport()
        from claudlobby.doctor import check_credentials

        check_credentials(fleet, paths, report)
        assert len(seen) == 1, "the declared, resolvable credential was not probed"
        assert "api.github.com" in " ".join(seen[0][1])
        assert report.checks[0].status == "pass"
        assert "probed OK" in report.checks[0].detail

    def test_silence_states_its_scope_rather_than_implying_validity(
        self, doctor_fleet, monkeypatch
    ):
        """Coverage honesty: a pass must not read as "credentials are fine".

        "Nothing was probed" has causes with different remedies, and the old
        code collapsed all of them into one reassuring line.
        """
        _, fleet, paths = doctor_fleet
        self._no_network(monkeypatch)
        self._stage_cascade(paths, monkeypatch)
        monkeypatch.delenv("GITHUB_PAT", raising=False)

        report = DoctorReport()
        from claudlobby.doctor import check_credentials

        check_credentials(fleet, paths, report)
        detail = report.checks[0].detail
        # It names the var and points at the check that owns the missing value,
        # instead of "no credential env vars found to probe".
        assert "GITHUB_PAT" in detail
        assert "env-vars" in detail
        assert "no credential env vars found" not in detail

    def test_a_broken_manifest_warns_rather_than_crashing_doctor(
        self, doctor_fleet, monkeypatch
    ):
        _, fleet, paths = doctor_fleet
        self._no_network(monkeypatch)
        monkeypatch.setattr(
            "claudlobby.credentials.declared_for_fleet",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        report = DoctorReport()
        from claudlobby.doctor import check_credentials

        check_credentials(fleet, paths, report)
        assert report.checks[0].status == "warn"

    def test_an_unreachable_cascade_refuses_rather_than_reading_as_no_value(
        self, doctor_fleet, monkeypatch
    ):
        """`ResolverUnavailable` must not become "declared with no value".

        Those two have opposite remedies — install/repair the resolver, versus
        go and set a credential — and the runtime raises precisely so the
        distinction survives. Folding it into an empty mapping would recreate
        the unreachable-vs-empty defect inside a fix for its sibling. The
        fixture deliberately does NOT stage lib/env-tiers.sh.
        """
        _, fleet, paths = doctor_fleet
        self._no_network(monkeypatch)
        monkeypatch.setenv("GITHUB_PAT", "ghp_whatever")

        report = DoctorReport()
        from claudlobby.doctor import check_credentials

        check_credentials(fleet, paths, report)
        assert report.checks[0].status == "warn"
        assert "cannot read the .env cascade" in report.checks[0].detail
        assert "no value" not in report.checks[0].detail
