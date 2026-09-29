"""#1965, the launchd half of #897: every composed launchd timer plist captures
its job's output and bounds its run.

A composed timer plist used to carry no StandardOutPath or StandardErrorPath, so
launchd sent the job's output to /dev/null, and nothing bounded the run, so
launchd skipped every later fire for as long as a wedged run lasted. Every plist
now runs its job through `lib/run-bounded.sh <budget> <job argv...>` and names
one log for both streams, `<root>/state/<label>.launchd.log`.

The round trip is the SupervisionSpec pattern (#1573,
tests/test_supervision_roundtrip.py): every plist a real compose writes is parsed
back into a `LaunchdTimerSpec` and rendered again, and the text must come back
byte for byte. A key the spec does not model, or a value the renderer drops or
changes, fails it.
"""

from __future__ import annotations

import plistlib
import re
import shlex
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby import composer
from claudlobby.composer import compose_fleet_timers, compose_host_timers
from claudlobby.config import load_fleet
from claudlobby.paths import Paths

REPO_ROOT = Path(__file__).resolve().parent.parent

# Generic placeholder fleet (public repo): one manager, four workers, a fleet
# override of one job's budget, and a briefing slot so the briefing family is
# composed too.
_FLEET = """\
    fleet:
      name: tf
      service_prefix: com.tf
      system_defaults: true
      defaults:
        jobs:
          fleet-pulse: { max_runtime: 1234 }
      teams:
        eng:
          manager: lead
          workers: [w1, w2, w3, w4]
      bots:
        lead:
          expertise: [orchestration]
        w1:
          expertise: [eng]
        w2:
          expertise: [eng]
        w3:
          expertise: [eng]
        w4:
          expertise: [eng]
          briefing:
            slots:
              morning: "*-*-* 08:30:00"
"""

_EXEC_START = re.compile(r"^ExecStart=(.+)$", re.M)


@pytest.fixture(scope="module")
def composed(tmp_path_factory):
    """Every timer plist one compose writes, fleet and host families alike:
    {label: (plist text, the matching systemd ExecStart argv)}, plus the fleet
    and the root they were composed for."""
    tmp = tmp_path_factory.mktemp("launchd-timers")
    fleet_dir = tmp / "root"
    fleet_dir.mkdir()
    (fleet_dir / "fleet.yaml").write_text(dedent(_FLEET))
    fleet, merged = load_fleet(fleet_dir / "fleet.yaml")
    paths = Paths(root=fleet_dir, fleet_dir=fleet_dir)
    units = {}
    for d in (
        compose_fleet_timers(fleet, paths, merged, output_dir=tmp / "fleet"),
        compose_host_timers(paths, output_dir=tmp / "host"),
    ):
        for plist in Path(d).rglob("*.plist"):
            service = plist.with_suffix(".service")
            argv = None
            if service.exists():
                m = _EXEC_START.search(service.read_text())
                argv = shlex.split(m.group(1)) if m else None
            units[plist.stem] = (plist.read_text(), argv)
    # Resident host services (plane-daemon, plane-view) are not timers: their own
    # emitter already captured output, and they run until stopped.
    timers = {
        label: unit
        for label, unit in units.items()
        if "<key>KeepAlive</key>" not in unit[0]
    }
    return {"timers": timers, "fleet": fleet, "root": fleet_dir}


def _plist(composed, label):
    return plistlib.loads(composed["timers"][label][0].encode())


def test_the_compose_is_not_vacuous(composed):
    labels = set(composed["timers"])
    for want in (
        "com.tf.keepalive",
        "com.tf.fleet-pulse",
        "com.tf.weekly-worker-restart",
        "com.tf.briefing-w4-morning",
        "claudlobby-disk-monitor",
        "claudlobby-plane-host-probe",
    ):
        assert want in labels, f"{want} not composed; got {sorted(labels)}"


class TestEveryTimerPlist:
    def test_runs_its_job_through_the_wrapper(self, composed):
        root = composed["root"]
        for label, (text, _) in composed["timers"].items():
            argv = plistlib.loads(text.encode())["ProgramArguments"]
            assert argv[0] == f"{root}/lib/run-bounded.sh", (label, argv)
            assert argv[1].isdigit() and int(argv[1]) > 0, (label, argv)

    def test_runs_the_same_job_argv_as_the_systemd_unit(self, composed):
        for label, (text, exec_argv) in composed["timers"].items():
            argv = plistlib.loads(text.encode())["ProgramArguments"]
            assert exec_argv is not None, f"{label} has no systemd ExecStart"
            assert argv[2:] == exec_argv, (label, argv, exec_argv)

    def test_sends_both_streams_to_its_own_log(self, composed):
        root = composed["root"]
        for label, (text, _) in composed["timers"].items():
            pl = plistlib.loads(text.encode())
            want = f"{root}/state/{label}.launchd.log"
            assert pl.get("StandardOutPath") == want, (label, pl.get("StandardOutPath"))
            assert pl.get("StandardErrorPath") == want, (
                label,
                pl.get("StandardErrorPath"),
            )
        # launchd creates the file, not its directory.
        assert (root / "state").is_dir()


class TestBudgets:
    """How long a job may run before run-bounded.sh stops it."""

    def _budget(self, composed, label):
        return int(_plist(composed, label)["ProgramArguments"][1])

    def test_an_interval_job_gets_three_intervals_but_at_least_ten_minutes(
        self, composed
    ):
        assert self._budget(composed, "com.tf.keepalive") == 600  # 60 s interval
        assert self._budget(composed, "claudlobby-plane-host-probe") == 600

    def test_a_calendar_job_gets_an_hour(self, composed):
        assert self._budget(composed, "claudlobby-disk-monitor") == 3600
        assert self._budget(composed, "com.tf.briefing-w4-morning") == 3600

    def test_an_explicit_max_runtime_wins(self, composed):
        assert self._budget(composed, "com.tf.fleet-pulse") == 1234

    def test_the_weekly_restart_budget_counts_each_worker(self, composed):
        fleet = composed["fleet"]
        managers = fleet.manager_bots()
        workers = [b for b_id, b in fleet.bots.items() if b_id not in managers]
        assert len(workers) == 4 and managers == {"lead"}
        want = sum(
            composer._bot_boot_policy(bot, fleet).ready_timeout_s + 150
            for bot in workers
        )
        assert want > 600, "fixture too small: the floor would hide the formula"
        assert self._budget(composed, "com.tf.weekly-worker-restart") == want


def test_the_wrapper_is_tracked_executable():
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", "lib/run-bounded.sh"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert out and out[0] == "100755", f"lib/run-bounded.sh mode: {out[:1]}"


# --- the round trip ----------------------------------------------------------

_MODELED = {
    "Label",
    "ProgramArguments",
    "EnvironmentVariables",
    "WorkingDirectory",
    "StandardOutPath",
    "StandardErrorPath",
    "StartInterval",
    "StartCalendarInterval",
    "AbandonProcessGroup",
}


def spec_from_plist(parsed: dict):
    """The spec a plist says, refusing what the spec cannot model."""
    from claudlobby.composer import LaunchdTimerSpec

    extra = set(parsed) - _MODELED
    if extra:
        raise ValueError(f"keys the spec does not model: {sorted(extra)}")
    if parsed.get("StandardOutPath") != parsed.get("StandardErrorPath"):
        raise ValueError("stdout and stderr name different logs")
    return LaunchdTimerSpec(
        label=parsed["Label"],
        program_arguments=tuple(parsed["ProgramArguments"]),
        environment=tuple(parsed["EnvironmentVariables"].items()),
        working_directory=parsed["WorkingDirectory"],
        log_path=parsed["StandardOutPath"],
        start_interval=parsed.get("StartInterval"),
        start_calendar=tuple(parsed.get("StartCalendarInterval", {}).items()),
        abandon_process_group=bool(parsed.get("AbandonProcessGroup", False)),
    )


class TestRoundTrip:
    def test_every_plist_renders_back_byte_for_byte(self, composed):
        from claudlobby.composer import render_launchd_timer_plist

        for label, (text, _) in composed["timers"].items():
            spec = spec_from_plist(plistlib.loads(text.encode()))
            assert render_launchd_timer_plist(spec) == text, label

    def test_a_dropped_key_breaks_the_round_trip(self, composed):
        from claudlobby.composer import render_launchd_timer_plist

        text = composed["timers"]["com.tf.keepalive"][0]
        lines = text.splitlines(keepends=True)
        i = lines.index("  <key>StandardErrorPath</key>\n")
        mutated = "".join(lines[:i] + lines[i + 2 :])
        spec = spec_from_plist(
            {
                **plistlib.loads(mutated.encode()),
                "StandardErrorPath": plistlib.loads(mutated.encode())[
                    "StandardOutPath"
                ],
            }
        )
        assert render_launchd_timer_plist(spec) != mutated

    def test_a_plist_whose_streams_differ_is_refused(self, composed):
        pl = _plist(composed, "com.tf.keepalive")
        pl["StandardErrorPath"] = pl["StandardOutPath"] + ".err"
        with pytest.raises(ValueError, match="different logs"):
            spec_from_plist(pl)
