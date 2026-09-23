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
    check_ignition,
    check_mcp_configs,
    check_npx_cache,
    check_services,
    format_report,
    run_doctor,
)
from claudlobby.paths import Paths

REPO = Path(__file__).resolve().parent.parent


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


class TestCheckWorkstreamResidual:
    """#1635: a registry file with rows the plane does not hold — the "two
    stores, one reader" state the F18 closure existed to end."""

    @staticmethod
    def _fleet_and_paths(tmp_path: Path):
        from tests.plane_fixtures import F, _paths, plane_root

        root = plane_root(tmp_path)
        paths = _paths(root)
        fleet, _md = load_fleet(root / "local" / F / "fleet.yaml")
        return root, fleet, paths

    def test_silent_when_no_residual_file_exists(self, tmp_path):
        from claudlobby.doctor import check_workstream_residual

        _root, fleet, paths = self._fleet_and_paths(tmp_path)
        report = DoctorReport()
        check_workstream_residual(fleet, paths, report)
        assert report.checks == []

    def test_fails_while_the_plane_lacks_rows_the_file_has(self, tmp_path):
        from tests.plane_fixtures import F
        from claudlobby.doctor import check_workstream_residual
        from claudlobby.plane.emit_api import emit_batch

        root, fleet, paths = self._fleet_and_paths(tmp_path)
        # anchor real identity for the fleet -- plane_workstreams refuses a
        # fleet the plane holds no bot of, same as any other plane door
        emit_batch(
            root,
            [
                {
                    "event_type": "system",
                    "emitter": "test-setup",
                    "fleet": F,
                    "occurred_at": "2026-09-01T00:00:00Z",
                    "payload": {
                        "event": "heartbeat",
                        "subject_kind": "actor",
                        "subject": f"bot:{F}/w1",
                        "data": {},
                    },
                }
            ],
        )
        paths.fleet_state.mkdir(parents=True, exist_ok=True)
        (paths.fleet_state / "workstreams.json").write_text(
            json.dumps(
                {
                    "updated": "2026-09-01T00:00:00Z",
                    "workstreams": {
                        "ws-one": {"id": "ws-one"},
                        "ws-two": {"id": "ws-two"},
                    },
                }
            )
        )

        report = DoctorReport()
        check_workstream_residual(fleet, paths, report)
        check = [c for c in report.checks if c.name == "workstream registry"][0]
        assert check.status == "fail"
        assert "2 row(s)" in check.detail
        assert "ws-one" in check.detail and "ws-two" in check.detail
        assert "plane import-workstreams" in check.detail

    def test_passes_once_every_row_is_on_the_plane(self, tmp_path):
        from tests.plane_fixtures import F
        from claudlobby.doctor import check_workstream_residual
        from claudlobby.plane.emit_api import emit_batch

        root, fleet, paths = self._fleet_and_paths(tmp_path)
        emit_batch(
            root,
            [
                {
                    "event_type": "workstream",
                    "emitter": "test-setup",
                    "fleet": F,
                    "occurred_at": "2026-08-01T00:00:00Z",
                    "payload": {
                        "workstream_id": "ws-one",
                        "title": "t",
                        "opened_by": f"bot:{F}/w1",
                    },
                }
            ],
        )
        paths.fleet_state.mkdir(parents=True, exist_ok=True)
        (paths.fleet_state / "workstreams.json").write_text(
            json.dumps(
                {
                    "updated": "2026-08-01T00:00:00Z",
                    "workstreams": {"ws-one": {"id": "ws-one"}},
                }
            )
        )

        report = DoctorReport()
        check_workstream_residual(fleet, paths, report)
        check = [c for c in report.checks if c.name == "workstream registry"][0]
        assert check.status == "pass"
        assert "1 row(s)" in check.detail

    def test_unreachable_plane_warns_rather_than_asserting_a_residual(self, tmp_path):
        """No plane db at all: cannot tell a residual from an imported row —
        WARN, never FAIL (that would assert a defect this rung cannot see)
        and never silent (that would be the same collapse one layer over)."""
        from claudlobby.doctor import check_workstream_residual

        _root, fleet, paths = self._fleet_and_paths(tmp_path)
        paths.fleet_state.mkdir(parents=True, exist_ok=True)
        (paths.fleet_state / "workstreams.json").write_text(
            json.dumps(
                {
                    "updated": "2026-08-01T00:00:00Z",
                    "workstreams": {"ws-one": {"id": "ws-one"}},
                }
            )
        )

        report = DoctorReport()
        check_workstream_residual(fleet, paths, report)
        check = [c for c in report.checks if c.name == "workstream registry"][0]
        assert check.status == "warn"
        assert "cannot tell a residual from an imported row" in check.detail

    def test_unreadable_residual_file_warns_not_silent(self, tmp_path):
        """UNLIKE absent, an unreadable file is not nothing to diagnose --
        collapsing the two would be the unreachable-vs-empty confusion
        source_state.py exists to prevent, one layer over."""
        from claudlobby.doctor import check_workstream_residual

        _root, fleet, paths = self._fleet_and_paths(tmp_path)
        paths.fleet_state.mkdir(parents=True, exist_ok=True)
        resid = paths.fleet_state / "workstreams.json"
        resid.write_text("{}")
        resid.chmod(0o000)
        try:
            report = DoctorReport()
            check_workstream_residual(fleet, paths, report)
        finally:
            resid.chmod(0o644)
        check = [c for c in report.checks if c.name == "workstream registry"][0]
        assert check.status == "warn"
        assert "could not be opened" in check.detail


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
        # supervisor.sh is a third required sibling: lib-common.sh unconditionally
        # sources it from its own directory (#1573 task 6).
        for f in ("lib-common.sh", "env-tiers.sh", "supervisor.sh"):
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


REPO_ROOT = Path(__file__).resolve().parent.parent


def _declare_railway(root: Path) -> "FleetConfig":  # noqa: F821
    """Give the fixture fleet the REAL Railway integration doc, and declare it.

    The real doc rather than a stub on purpose: the defect this class guards is
    `doctor`'s table disagreeing with the declared contract, so a test carrying
    its own private copy of the contract could never see it.
    """
    (root / "library" / "integrations" / "railway.md").write_bytes(
        (REPO_ROOT / "library" / "integrations" / "railway.md").read_bytes()
    )
    (root / "fleet.yaml").write_text(
        dedent("""\
        fleet:
          name: test-fleet
          service_prefix: com.test
          bots:
            worker:
              expertise: [eng]
              mcp: [github]
              integrations: [railway]
              telegram:
                handle: w_bot
    """)
    )
    fleet, _md = load_fleet(root / "fleet.yaml")
    return fleet


def _declared_railway_vars() -> set[str]:
    """The Railway vars the shipped integration contract declares."""
    import yaml

    text = (REPO_ROOT / "library" / "integrations" / "railway.md").read_text()
    front = text.split("---", 2)[1]
    return set(yaml.safe_load(front)["env_contract"])


class TestRailwayProbesMatchTheDeclaredContract:
    """The part that EXECUTES, rather than restating the rule in prose.

    `doctor` probed a retired variable for months because its table was a copy
    of `creds-check.sh`'s kept in sync by hand. Fixing that once is not enough:
    the defect came back through a REFACTOR — #1377 rebuilt this block around a
    declaration-keyed probe registry and carried `RAILWAY_API_TOKEN` forward
    into it, so a fleet declaring the two live tokens would have had Railway
    silently drop out of the intersection.

    A comment saying "change one, change both" does not survive that. This does.
    """

    def test_the_probe_table_names_exactly_the_declared_railway_vars(self):
        from claudlobby.doctor import _CREDENTIAL_PROBES

        probed = {v for v, (kind, _host) in _CREDENTIAL_PROBES.items() if kind == "railway"}
        assert probed == _declared_railway_vars(), (
            "doctor's Railway probes and library/integrations/railway.md have "
            "diverged. A declared var with no probe drops out of the probe "
            "intersection; a probe for an undeclared var can never fire."
        )

    def test_every_probed_railway_var_has_a_scope_matched_query(self):
        from claudlobby.doctor import _CREDENTIAL_PROBES, _RAILWAY_QUERIES

        probed = {v for v, (kind, _host) in _CREDENTIAL_PROBES.items() if kind == "railway"}
        assert probed == set(_RAILWAY_QUERIES), (
            "a Railway var reachable by the probe registry with no entry here "
            "would raise KeyError mid-diagnostic"
        )


class TestEachRailwayTokenIsProbedWithAQueryItCanAnswer:
    """ONE PROBE FOR ALL TOKENS IS THE BUG.

    A workspace-scoped token is not bound to an account, so it cannot answer
    `me` BY CONSTRUCTION. Probing it that way reports a working credential as
    dead — which is what made this fleet's credential alert fire daily against
    two working tokens until the operator learned to ignore it.
    """

    @staticmethod
    def _recorder(monkeypatch) -> list:
        calls: list = []

        class _R:
            stdout = '{"data":{}}\n200'
            returncode = 0

        def _fake(headers, extra_args):
            calls.append(extra_args)
            return _R()

        monkeypatch.setattr("claudlobby.doctor._curl_with_config", _fake)
        return calls

    @staticmethod
    def _railway_payloads(calls) -> str:
        return "\n".join(
            " ".join(c) for c in calls if any("backboard.railway" in a for a in c)
        )

    def _run(self, doctor_fleet, monkeypatch, env_line: str) -> str:
        root, _fleet, paths = doctor_fleet
        fleet = _declare_railway(root)
        TestCheckCredentialsScoping._stage_cascade(paths, monkeypatch)
        for var in _declared_railway_vars():
            monkeypatch.delenv(var, raising=False)
        (paths.root / ".env").write_text(env_line)
        calls = self._recorder(monkeypatch)

        from claudlobby.doctor import check_credentials

        check_credentials(fleet, paths, DoctorReport())
        return self._railway_payloads(calls)

    def test_a_workspace_token_is_probed_with_projects_and_never_with_me(
        self, doctor_fleet, monkeypatch
    ):
        probes = self._run(
            doctor_fleet, monkeypatch, "RAILWAY_PERSONAL_PROJECT_TOKEN=t\n"
        )
        assert "projects" in probes, "the workspace token was not probed at all"
        assert "me{" not in probes, (
            "a workspace-scoped token cannot answer `me` by construction; "
            "probing it that way reports a working credential as dead"
        )

    def test_an_account_token_is_probed_with_me(self, doctor_fleet, monkeypatch):
        """The positive control. A test that only ever sees `projects` cannot
        tell per-token probing from `projects`-for-everything."""
        probes = self._run(doctor_fleet, monkeypatch, "RAILWAY_PERSONAL_TOKEN=t\n")
        assert "me{" in probes, "the account token was not probed with `me`"

    def test_both_declared_tokens_get_their_own_probe(self, doctor_fleet, monkeypatch):
        probes = self._run(
            doctor_fleet,
            monkeypatch,
            "RAILWAY_PERSONAL_TOKEN=t\nRAILWAY_PERSONAL_PROJECT_TOKEN=t2\n",
        )
        assert "me{" in probes and "projects" in probes, (
            "one dead token among several is not `Railway is broken`; each "
            "declared token gets its own probe"
        )


class TestGoalBindingCheck:
    """Claudfather/Claudlobby#1634 — the goal-binding finding gets its own
    named rung rather than being one of `fleet-yaml`'s `N warning(s)`. A count
    is not something an operator can act on, and each of these findings stops
    the check-in beat from producing work."""

    def _paths(self, fleet_dir: Path) -> Paths:
        return Paths(root=fleet_dir, fleet_dir=fleet_dir)

    def _scope(self, fleet_dir: Path, org: str, repos: list[str]) -> None:
        import re as _re

        text = (fleet_dir / "fleet.yaml").read_text()
        m = _re.search(r"^(\s+)lead:\n(\s+)expertise:.*\n", text, _re.M)
        inner = m.group(2)
        block = f"{inner}scope:\n{inner}  org: {org}\n{inner}  repos: [{', '.join(repos)}]\n"
        (fleet_dir / "fleet.yaml").write_text(text[: m.end()] + block + text[m.end():])

    def test_no_projects_at_all_warns_on_its_own_line(self, fleet_dir):
        from claudlobby.doctor import check_goal_binding

        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = DoctorReport()
        check_goal_binding(fleet, self._paths(fleet_dir), report)
        assert [c for c in report.checks if c.name == "goal-binding"]
        check = report.checks[0]
        assert check.status == "warn"
        assert "check-in-equipped" in check.detail or "no projects" in check.detail

    def test_derived_projects_pass_and_the_line_says_derived(self, fleet_dir):
        from claudlobby.doctor import check_goal_binding

        self._scope(fleet_dir, "acme", ["storefront"])
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = DoctorReport()
        check_goal_binding(fleet, self._paths(fleet_dir), report)
        check = report.checks[0]
        assert check.name == "goal-binding"
        assert check.status == "pass", check.detail
        assert "derived from scope.repos" in check.detail
        assert "1 project" in check.detail

    def test_declared_projects_pass_and_the_line_says_projects_yaml(self, fleet_dir):
        from claudlobby.doctor import check_goal_binding

        (fleet_dir / "projects.yaml").write_text(
            "projects:\n  shop:\n    title: Shop\n    repos: [acme/storefront]\n"
        )
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = DoctorReport()
        check_goal_binding(fleet, self._paths(fleet_dir), report)
        check = report.checks[0]
        assert check.status == "pass", check.detail
        assert "projects.yaml" in check.detail
        assert "derived" not in check.detail

    def test_run_doctor_includes_the_rung(self, fleet_dir, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        report = run_doctor(fleet, self._paths(fleet_dir))
        names = [c.name for c in report.checks]
        assert "goal-binding" in names, names
# --- shared doctor-fleet scaffolding (#1680) ---------------------------------
# Module-level functions rather than methods reached by instantiating a sibling
# test class: three classes below share this scaffold, and cross-class
# instantiation makes a signature change reach them through call sites nobody
# greps for. `tests/conftest.py` already houses exactly this pattern as plain
# functions.

_BRIEFING_SLOT = (
    "      briefing:\n"
    "        slots:\n"
    '          morning: "*-*-* 08:30:00"\n'
)

#: Drops the leaf-manager role's default `checkin` protocol, which is what
#: decides WHICH no-projects warning an operator sees.
_NO_CHECKIN = "  system_defaults:\n    protocols: false\n"


def _fleet_yaml(*, manager: bool = True, armed: bool = False, equipped: bool = True) -> str:
    """A fleet manifest varying only the three facts #1680's rungs read.

    Written at zero indent: these strings are assembled by concatenation and
    a dedent-relative literal makes every slot depend on the literal's own
    leading whitespace.
    """
    briefing = _BRIEFING_SLOT if armed else ""
    if not manager:
        return (
            "fleet:\n"
            "  name: solo-fleet\n"
            "  service_prefix: com.solo\n"
            "  bots:\n"
            "    worker:\n"
            "      expertise: [software-engineering]\n" + briefing
        )
    return (
        "fleet:\n"
        "  name: mgr-fleet\n"
        "  service_prefix: com.mgr\n"
        + ("" if equipped else _NO_CHECKIN)
        + "  teams:\n"
        "    eng:\n"
        "      manager: lead\n"
        "      workers: [worker]\n"
        "  bots:\n"
        "    lead:\n"
        "      expertise: [orchestration]\n"
        "      manages: [worker]\n"
        + briefing
        + "    worker:\n"
        "      expertise: [software-engineering]\n"
    )


def _doctor_root(tmp_path: Path, fleet_yaml: str) -> Path:
    """A throwaway fleet root complete enough for the WHOLE `run_doctor`.

    Wires the repo's real `lib/` rather than stubbing the resolver: task-recheck
    ships opt-out (on by default), so a resolver-unavailable fallback reads it
    as ARMED and every disarmed scenario in this file silently collapses to
    PASS. The `.env` then disarms it so "no door armed" is reachable at all.
    """
    from claudlobby.config import DEFAULT_GUARDRAILS

    root = tmp_path / "r"
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
        (root / "library" / kind).mkdir(parents=True, exist_ok=True)
    (root / "templates").mkdir(exist_ok=True)
    (root / "runtime" / "bots").mkdir(parents=True, exist_ok=True)
    # Wire whatever is MISSING rather than keying on the directory's existence
    # (origin/main's fix for the same #1588 class, adopted here). `root`
    # now ships a real `mcp-package-grammar.py`, so `lib/` EXISTS without being
    # wired, and an existence check skips the wiring silently: the switch
    # resolver then cannot read its doors, `task-recheck` falls back to ARMED
    # regardless of `.env`, and `_validate_ignition`'s early return makes every
    # scenario below pass vacuously.
    #
    # Per-entry links, never a whole-dir symlink: fixtures delete files under
    # `lib/`, and through a directory symlink those unlinks reach the repo's
    # own copies.
    lib = root / "lib"
    lib.mkdir(exist_ok=True)
    for real in (REPO / "lib").iterdir():
        link = lib / real.name
        if not link.exists():
            link.symlink_to(real)
    # Repairing is not the same as having repaired: assert the wiring is LIVE
    # (#1689). Testing for the FILE the resolver needs tests the proposition;
    # testing that a directory exists is the proxy that failed (#1588).
    assert (lib / "env-tiers.sh").is_file(), (
        f"{lib} exists but does not carry the real lib/ — the switch resolver "
        f"cannot run, so TASK_RECHECK_ENABLED=0 never lands and task-recheck "
        f"reads ARMED. Every disarmed case here would measure the wrong state."
    )
    (root / "library" / "expertise" / "orchestration.md").write_text("# Mgr\n")
    (root / "library" / "expertise" / "software-engineering.md").write_text("# Eng\n")
    for name in DEFAULT_GUARDRAILS:
        (root / "library" / "guardrails" / f"{name}.md").write_text(
            f"---\ntitle: {name}\n---\n\nDefault guardrail.\n"
        )
    (root / "templates" / "claude.md.j2").write_text("# {{ bot.name }}\n")
    (root / "fleet.yaml").write_text(dedent(fleet_yaml))
    (root / ".env").write_text("TASK_RECHECK_ENABLED=0\n")
    return root


def _declare_projects(root: Path) -> None:
    (root / "projects.yaml").write_text(
        "projects:\n  shop:\n    title: Shop\n    repos: [acme/storefront]\n"
    )


def _pin_plugin_manifest(tmp_path: Path, monkeypatch, fleet) -> None:
    """Make `Path.home()` a FIXTURE fact rather than a HOST fact.

    `run_doctor`'s first rung runs the whole validator, whose plugins check
    resolves `Path.home() / ".claude" / "plugins" / "installed_plugins.json"`
    — so on a box that has never installed a plugin, `fleet-yaml` warns about
    the developer's own machine. `fleet-yaml` is a COUNT rung: it aggregates
    every `validate()` warning and cannot say what any of them is about, so
    that host fact is indistinguishable from a real finding and lands in the
    allowlist test below as a phantom. Green here, red on a fresh box or a
    runner — which is the whole failure this file's tripwire exists to catch,
    turned on the tripwire itself.

    Mirrors `tests/test_validator.py::_fake_installed`, the convention this
    repo already has for exactly this boundary. The installed set is DERIVED
    from what the fleet actually requires rather than hardcoded, so a change
    to the default plugin set cannot silently reopen this.
    """
    import json

    fake_home = tmp_path / "fakehome"
    plugins_dir = fake_home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps(
            {"plugins": {name: {"version": "0.0.0"} for name in fleet.plugins.required}}
        )
    )
    monkeypatch.setenv("HOME", str(fake_home))


def _doctor_rungs(tmp_path, monkeypatch, fleet_yaml: str, *, projects: bool = False):
    """Run the WHOLE `run_doctor` and return its checks keyed by rung name."""
    root = _doctor_root(tmp_path, fleet_yaml)
    if projects:
        _declare_projects(root)
    monkeypatch.delenv("FLEET_NAME", raising=False)
    fleet, _md = load_fleet(root / "fleet.yaml")
    _pin_plugin_manifest(tmp_path, monkeypatch, fleet)
    report = run_doctor(fleet, Paths(root=root, fleet_dir=root))
    return {c.name: c for c in report.checks}


class TestCheckIgnition:
    """#1633: does anything give an idle bot on this fleet a turn?

    Uses a real env-tiers resolver (the repo's own lib/, symlinked — the
    test_switches.py pattern) rather than stubbing it: task-recheck ships
    opt-out (on by default), so a resolver-unavailable fallback would read it
    as armed regardless of the scenario under test and every case here would
    silently collapse to PASS.
    """

    _FLEET_NO_DOOR = """\
        fleet:
          name: ign-fleet
          service_prefix: com.ign
          bots:
            mgr:
              expertise: [orchestration]
              manages: [worker]
            worker:
              expertise: [software-engineering]
    """

    _FLEET_BRIEFING_ARMED = """\
        fleet:
          name: ign-fleet
          service_prefix: com.ign
          bots:
            mgr:
              expertise: [orchestration]
              manages: [worker]
              briefing:
                slots:
                  morning: "*-*-* 08:30:00"
            worker:
              expertise: [software-engineering]
    """

    def _check(self, tmp_path, fleet_yaml: str):
        root = _doctor_root(tmp_path, fleet_yaml)
        fleet, _md = load_fleet(root / "fleet.yaml")
        paths = Paths(root=root, fleet_dir=root)
        report = DoctorReport()
        check_ignition(fleet, paths, report)
        assert len(report.checks) == 1
        return report.checks[0]

    def test_ignition_warns_when_a_leaf_manager_fleet_has_no_armed_door(self, tmp_path):
        check = self._check(tmp_path, self._FLEET_NO_DOOR)
        assert check.name == "ignition"
        assert check.status == "warn"
        assert "0/" in check.detail
        assert "briefing.slots" in check.detail

    def test_ignition_passes_and_names_the_armed_door_when_briefing_slots_exist(
        self, tmp_path
    ):
        check = self._check(tmp_path, self._FLEET_BRIEFING_ARMED)
        assert check.status == "pass"
        assert "briefing.slots" in check.detail

    def test_ignition_rung_states_it_reads_declared_not_enrolled_state(self, tmp_path):
        check = self._check(tmp_path, self._FLEET_NO_DOOR)
        assert "declared" in check.detail.lower()
        assert "839" in check.detail or "1040" in check.detail



class TestRungAgreementOnAManagerLessFleet:
    """#1680: `goal-binding` and `ignition` are two halves of one question —
    will this fleet ever do any work? — and they were built in parallel
    without seeing each other. On a fleet with no leaf manager they printed a
    WARN and a PASS about it: the ignition rung gated on
    `fleet.leaf_manager_bots()` and the goal-binding rung did not.

    Everything here runs the WHOLE `run_doctor`, never the two checks in
    isolation. The divergence survived review precisely because each function
    was internally consistent — what has to be pinned is the report an
    operator actually reads, so that a rung added later lands inside the
    test's field of view rather than beside it.
    """

    def test_both_rungs_pass_and_give_the_same_not_applicable_reason(
        self, tmp_path, monkeypatch
    ):
        by_name = _doctor_rungs(tmp_path, monkeypatch, _fleet_yaml(manager=False))
        # Fail closed: a renamed or dropped rung must break this test rather
        # than make it vacuously true.
        assert {"goal-binding", "ignition"} <= set(by_name), sorted(by_name)
        for name in ("goal-binding", "ignition"):
            assert by_name[name].status == "pass", by_name[name].detail
            assert "no leaf manager" in by_name[name].detail, by_name[name].detail

    def test_no_new_rung_warns_about_work_this_fleet_cannot_dispatch(
        self, tmp_path, monkeypatch
    ):
        """The tripwire the acceptance criterion asks for.

        A fleet with no leaf manager has no dispatcher, so no rung may report
        a *work-dispatch* gap on it as a finding. `services` is the one
        legitimate warning on this fixture — bots exist and are not enrolled,
        which is true and unrelated. Any OTHER rung warning here is either
        the #1680 divergence rebuilt or a deliberate new finding; both need a
        human to look, which is what an allowlist that must be edited buys.
        """
        by_name = _doctor_rungs(tmp_path, monkeypatch, _fleet_yaml(manager=False))
        warned = {n for n, c in by_name.items() if c.status in ("warn", "fail")}
        assert warned <= {"services"}, {n: by_name[n].detail for n in warned}

    def test_a_manager_less_fleet_with_projects_still_reports_them(
        self, tmp_path, monkeypatch
    ):
        """The gate replaces the no-projects WARN only. A manager-less fleet
        that HAS declared projects keeps its informative PASS — the gate is
        an applicability test, not a mute button."""
        by_name = _doctor_rungs(
            tmp_path, monkeypatch, _fleet_yaml(manager=False), projects=True
        )
        goal = by_name["goal-binding"]
        assert goal.status == "pass", goal.detail
        assert "1 project(s)" in goal.detail


class TestCoRequisiteCrossReference:
    """#1680 finding 2: on a fleet with a leaf manager, no projects and no
    armed door, BOTH rungs fire and they are not redundant — one says there
    is nothing to dispatch AT, the other that there is nothing to dispatch
    WITH. Arming a door on a fleet with no project registry still cannot
    dispatch, and declaring projects with no door armed still never fires.

    Two warnings is therefore the correct output; what was missing is that
    neither said so. A first-timer who fixes one still sees the other and may
    reasonably conclude their first fix did not work — and the cheapest wrong
    response to that is to undo it.
    """

    # --- both conditions hold: each names the other -------------------------

    def test_both_warn_and_each_names_the_other_as_a_co_requisite(
        self, tmp_path, monkeypatch
    ):
        by_name = _doctor_rungs(tmp_path, monkeypatch, _fleet_yaml())
        goal, ign = by_name["goal-binding"], by_name["ignition"]
        assert goal.status == "warn", goal.detail
        assert ign.status == "warn", ign.detail
        assert "ignition warning beside this one" in goal.detail, goal.detail
        assert "goal-binding warning beside this one" in ign.detail, ign.detail
        assert "co-requisite, not a duplicate" in goal.detail
        assert "co-requisite, not a duplicate" in ign.detail

    def test_the_cross_reference_reaches_the_un_equipped_manager_wording_too(
        self, tmp_path, monkeypatch
    ):
        """Two different warnings carry the no-projects finding — the
        validator's check-in-equipped one (which doctor renders verbatim) and
        doctor's own plain `no projects` line, reached when the leaf manager
        is NOT check-in-equipped. Both need the clause, or which wording an
        operator happens to land on decides whether they are told."""
        by_name = _doctor_rungs(tmp_path, monkeypatch, _fleet_yaml(equipped=False))
        goal = by_name["goal-binding"]
        assert goal.status == "warn", goal.detail
        assert "no projects: neither a projects.yaml" in goal.detail, goal.detail
        assert "check-in-equipped" not in goal.detail, (
            "precondition: this fleet must reach doctor's OWN no-projects "
            "line, or the test measures the validator's wording twice"
        )
        assert "ignition warning beside this one" in goal.detail, goal.detail

    # --- only one condition holds: no clause --------------------------------

    def test_no_clause_on_goal_binding_when_a_door_is_armed(
        self, tmp_path, monkeypatch
    ):
        by_name = _doctor_rungs(tmp_path, monkeypatch, _fleet_yaml(armed=True))
        goal, ign = by_name["goal-binding"], by_name["ignition"]
        assert ign.status == "pass", ign.detail
        assert goal.status == "warn", goal.detail
        assert "ignition warning beside this one" not in goal.detail, goal.detail

    def test_no_clause_on_doctor_s_own_no_projects_line_when_a_door_is_armed(
        self, tmp_path, monkeypatch
    ):
        """The sibling of the test above, on the OTHER goal-binding wording.
        Without it, making doctor's own clause unconditional is a mutation the
        suite does not catch: the equipped fixture never reaches that line, so
        every negative was being measured on the validator's text."""
        by_name = _doctor_rungs(
            tmp_path, monkeypatch, _fleet_yaml(armed=True, equipped=False)
        )
        goal, ign = by_name["goal-binding"], by_name["ignition"]
        assert ign.status == "pass", ign.detail
        assert goal.status == "warn", goal.detail
        assert "no projects: neither a projects.yaml" in goal.detail, goal.detail
        assert "ignition warning beside this one" not in goal.detail, goal.detail

    def test_no_clause_on_ignition_when_projects_are_declared(
        self, tmp_path, monkeypatch
    ):
        by_name = _doctor_rungs(
            tmp_path, monkeypatch, _fleet_yaml(), projects=True
        )
        goal, ign = by_name["goal-binding"], by_name["ignition"]
        assert goal.status == "pass", goal.detail
        assert ign.status == "warn", ign.detail
        assert "goal-binding warning beside this one" not in ign.detail, ign.detail

    def test_the_ignition_clause_does_not_swallow_the_arm_line(
        self, tmp_path, monkeypatch
    ):
        """The clause is inserted BEFORE `Cheapest to arm:`, not appended
        after it — a sentence trailing a copy-pasteable config line is the one
        place it gets read as part of the line. `ignition_warning_tail` owns
        that order for both surfaces."""
        detail = _doctor_rungs(tmp_path, monkeypatch, _fleet_yaml())["ignition"].detail
        assert detail.index("co-requisite") < detail.index("Cheapest to arm:")
        assert detail.rstrip().endswith("generate + lib/setup-fleet"), detail


class TestIgnitionGapIsTheRungsOwnPredicate:
    """`ignition_gap` is what the goal-binding warnings ask before claiming
    the ignition warning is also speaking. A clause asserting that when it is
    not speaking is worse than no clause, so the predicate and the rung are
    pinned equal across the shapes that separate them — otherwise the two
    drift and the first sign is an operator chasing a warning that is not
    there."""

    @pytest.mark.parametrize(
        "armed,manager",
        [(False, True), (True, True), (False, False), (True, False)],
    )
    def test_the_predicate_agrees_with_the_rung(
        self, tmp_path, monkeypatch, armed, manager
    ):
        from claudlobby.ignition import ignition_gap

        root = _doctor_root(tmp_path, _fleet_yaml(manager=manager, armed=armed))
        monkeypatch.delenv("FLEET_NAME", raising=False)
        fleet, _md = load_fleet(root / "fleet.yaml")
        paths = Paths(root=root, fleet_dir=root)
        report = DoctorReport()
        check_ignition(fleet, paths, report)
        rung_warns = report.checks[0].status == "warn"
        assert rung_warns is (not armed and manager), report.checks[0].detail
        assert ignition_gap(fleet, paths) is rung_warns, report.checks[0].detail


class TestTheFixtureRefusesDeadWiring:
    """#1689 positive control: the live-wiring assertion in `_doctor_root`
    must actually FIRE. An assertion nobody has watched fail is not a check —
    it is a comment that raises.

    Reproduces the #1588 mechanism verbatim: a `lib/` that exists as a plain
    DIRECTORY satisfies the `if not ... .exists()` guard, so the symlink is
    skipped, the switch resolver cannot run, task-recheck falls back to ARMED,
    and the disarmed scenarios in this file silently measure the opposite of
    what they intend.

    Why this class earns its place rather than being a comment: MEASURED on a
    deliberately degraded arm, 6 of the 13 cases in this file stay silent
    while 7 fail. A reading pass over the same call paths predicted the
    opposite split and named the wrong tests, so the suite cannot be trusted
    to distinguish "works" from "never ran" by inspection — which is exactly
    why the fixture refuses rather than the reader classifying.

    No shortcut for WHICH tests stay silent has survived: neither assertion
    shape (a presence assertion,
    `test_a_manager_less_fleet_with_projects_still_reports_them`, is silent)
    nor asserts-why-not-what. The only property that held is the near-tautology
    that a test is silent exactly when its expected outcome is identical under
    both wiring states. Hence a structural refusal, which needs no
    classification to be correct. See Claudfather/Claudlobby#1689.
    """

    def test_a_pre_created_lib_directory_is_REPAIRED_not_skipped(self, tmp_path):
        """The #1588 arming, verbatim: something creates `lib/` first. The old
        guard skipped the wiring and the suite went quiet; the helper now wires
        whatever is missing, per entry, and the resolver is live afterwards."""
        (tmp_path / "r" / "lib").mkdir(parents=True)
        root = _doctor_root(tmp_path, _fleet_yaml())
        assert (root / "lib" / "env-tiers.sh").is_file()

    def test_the_assertion_FIRES_when_repair_is_impossible(self, tmp_path):
        """An assertion nobody has watched fail is not a check, it is a comment
        that raises. `env-tiers.sh` pre-created as a DIRECTORY cannot be
        repaired by a per-entry symlink — `link.exists()` is true, so nothing
        is wired — and `.is_file()` is the predicate that still catches it."""
        (tmp_path / "r" / "lib" / "env-tiers.sh").mkdir(parents=True)
        with pytest.raises(AssertionError, match="does not carry the real lib"):
            _doctor_root(tmp_path, _fleet_yaml())

    def test_a_clean_build_is_wired_so_the_controls_are_not_vacuous(
        self, tmp_path
    ):
        root = _doctor_root(tmp_path, _fleet_yaml())
        assert (root / "lib" / "env-tiers.sh").is_file()
