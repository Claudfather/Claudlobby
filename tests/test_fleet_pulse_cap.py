"""The fleet pulse's time cap (#2059, a stopgap for #907's sweep cost).

The cap is fleet.yaml's ``fleet_pulse.timeout_s``, clamped to its validated range,
else 120 s scaled by load per CPU up to 233 s, under the default 300 s pulse
cadence so an activation finds the shared lock free between ticks. A scheduled run
ends by the largest cap, counted from its unit's start (#2347), which the native unit
entry passes to the command it becomes; that path's own test runs through the entry
(tests/test_git_native_entry.py). A sweep that reaches its cap gets SIGTERM, then
SIGKILL, and leaves a summary that says so, with the last complete one kept beside
it. The unit's own limit stops a run that cannot stop itself.
"""

from __future__ import annotations

import contextlib
import os
import signal
import stat
import sys
import time
from types import SimpleNamespace

import pytest

from claudlobby import fleet_pulse
from claudlobby.config import FleetPulseConfig, _coerce_fleet_pulse


@pytest.mark.parametrize(
    "configured,load1,cpus,cap",
    [
        (None, 0.5, 4, 120),  # an idle host keeps the old cap
        (None, 6.0, 4, 180),  # scaled by load per CPU
        (None, 74.0, 4, 233),  # the heaviest measured load, at the ceiling
        (450, 74.0, 4, 450),  # fleet.yaml wins over the load
        (5, 0.5, 4, 30),  # and is clamped to its validated range
        (7200, 0.5, 4, 3600),
    ],
)
def test_the_cap_is_configured_or_scaled_with_load(configured, load1, cpus, cap):
    got, why = fleet_pulse.sweep_cap(configured, load1, cpus)
    assert got == cap
    assert ("fleet_pulse.timeout_s" in why) == (configured is not None), why


def test_the_units_limit_keeps_every_run_inside_the_sealed_bound():
    """The unit's own limit (TimeoutStartSec=) is the run's ceiling plus the stop's
    budget: the grace, the reap, and EXIT_S for the summary, the CLI's stop line and
    its interpreter's exit, which on a loaded host takes seconds after that line.
    Past the limit systemd waits UNIT_STOP_S in each of at most four kill phases,
    and each of its five timers can fire up to 0.25 s late (sd-event's default
    accuracy), so a run still ends by 260 s. The default limit stays 254 s.
    """
    limit = fleet_pulse.unit_time_limit(None)
    assert limit == fleet_pulse.UNIT_LIMIT_S == 254  # the composed unit keeps its limit
    assert fleet_pulse.UNIT_MARGIN_S == (
        fleet_pulse.TERM_GRACE_S + fleet_pulse.REAP_WAIT_S + fleet_pulse.EXIT_S
    )
    # EXIT_S leaves room for the main process's exit after the stop line.
    assert fleet_pulse.EXIT_S > 8
    assert limit + 4 * fleet_pulse.UNIT_STOP_S + 5 * 0.25 <= 260
    assert fleet_pulse.unit_time_limit(600) == 600 + fleet_pulse.UNIT_MARGIN_S
    assert fleet_pulse.unit_time_limit(5) == 30 + fleet_pulse.UNIT_MARGIN_S


def _cap(monkeypatch, cap, ceiling=None):
    """Stub the cap; the run's ceiling is the cap at unbounded load."""

    def stub(configured, load1, cpus):
        return (ceiling if ceiling and load1 == float("inf") else cap), "a test cap"

    monkeypatch.setattr(fleet_pulse, "sweep_cap", stub)


def _unit_started_at(monkeypatch, seconds_ago):
    """Stamp this process as the native unit entry does, for a unit that started
    seconds_ago, and return that start on the monotonic clock."""
    started = time.monotonic() - seconds_ago
    monkeypatch.setenv("CLAUDLOBBY_UNIT_STARTED", f"{os.getpid()} {started:.3f}")
    return started


def _time_out(
    summary,
    monkeypatch,
    *,
    cap=1,
    ceiling=None,
    script="echo watchdog dark >&2; sleep 30",
    env=None,
):
    _cap(monkeypatch, cap, ceiling)
    with pytest.raises(fleet_pulse.FleetPulseError) as failure:
        fleet_pulse._sweep(
            ["/bin/sh", "-c", script], env or dict(os.environ), summary_path=summary
        )
    assert failure.value.code == "timeout" and failure.value.effect_attempted
    return failure.value


def test_a_timed_out_sweep_says_so_and_keeps_the_last_complete_summary(
    tmp_path, monkeypatch
):
    summary = tmp_path / "state/pulse/example.pulse-summary.txt"
    kept = tmp_path / "state/pulse/example.pulse-summary.last-complete.txt"
    summary.parent.mkdir(parents=True)
    summary.write_text("all healthy\n")
    summary.chmod(0o600)

    error = _time_out(summary, monkeypatch)
    assert str(summary) in str(error)
    text = summary.read_text()
    assert text.startswith("TIMED OUT"), text
    for needle in ("1 s", "a test cap", "fleet escalation", str(kept), "watchdog dark"):
        assert needle in text, (needle, text)
    assert stat.S_IMODE(summary.stat().st_mode) == 0o600
    assert kept.read_text() == "all healthy\n"

    _time_out(summary, monkeypatch)  # a second timeout keeps the last COMPLETE summary
    assert kept.read_text() == "all healthy\n"


def test_a_summary_that_cannot_be_written_still_reports_a_timeout(
    tmp_path, monkeypatch
):
    def refuse(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fleet_pulse, "_record_timeout", refuse)
    error = _time_out(tmp_path / "pulse-summary.txt", monkeypatch)
    assert "not written" in str(error) and "No space left" in str(error)


def test_the_cap_counts_from_the_units_start(tmp_path, monkeypatch):
    """Time the run spent before its sweep (starting two interpreters and the CLI on
    a stalled host) counts against its ceiling: the ceiling bounds the unit's run,
    which is what the cadence and `host activate` see."""
    started = _unit_started_at(monkeypatch, 2)
    error = _time_out(tmp_path / "pulse-summary.txt", monkeypatch, cap=3)
    assert time.monotonic() - started < 3 + 1, "the sweep got its whole cap"
    assert "the sweep began 2 s into a run capped at 3 s" in str(error)


def test_a_sweep_keeps_its_own_cap_while_the_run_has_room(tmp_path, monkeypatch):
    _unit_started_at(monkeypatch, 5)
    error = _time_out(tmp_path / "pulse-summary.txt", monkeypatch, cap=1, ceiling=60)
    assert "after 1 s of its 1 s cap" in str(error), error
    assert "began 5 s into a run capped at 60 s" in str(error), error


def test_a_run_with_no_time_left_starts_no_sweep(tmp_path, monkeypatch):
    _cap(monkeypatch, 1, ceiling=3)
    _unit_started_at(monkeypatch, 5)
    swept = tmp_path / "swept"
    with pytest.raises(fleet_pulse.FleetPulseError) as failure:
        fleet_pulse._sweep(
            ["/bin/sh", "-c", f"touch '{swept}'"],
            dict(os.environ),
            summary_path=tmp_path / "pulse-summary.txt",
        )
    assert failure.value.code == "timeout" and not failure.value.effect_attempted
    assert not swept.exists()


def test_the_stop_is_bounded_when_output_is_held_outside_the_sweeps_group(
    tmp_path, monkeypatch
):
    """SIGKILL reaches only the sweep's own process group, and a descendant that
    started its own session keeps the output pipe open for as long as it runs. The
    stop waits for that output only through the grace, then for the sweep to exit."""
    monkeypatch.setattr(fleet_pulse, "TERM_GRACE_S", 0.5)
    pid_file = tmp_path / "escaped.pid"
    escape = (
        "import os, sys, time; os.setsid(); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(8)"
    )
    script = f'"$PULSE_TEST_PYTHON" -c "{escape}" "$PULSE_TEST_PID_FILE" & sleep 30'
    env = {
        **os.environ,
        "PULSE_TEST_PYTHON": sys.executable,
        "PULSE_TEST_PID_FILE": str(pid_file),
    }
    began = time.monotonic()
    try:
        _time_out(tmp_path / "summary.txt", monkeypatch, script=script, env=env)
        assert time.monotonic() - began < 4, "the stop waited for the escaped output"
    finally:
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pid_file.read_text()), signal.SIGKILL)


@pytest.mark.parametrize(
    "stamp,counts",
    [
        ("{own} {started}", True),  # stamped for this process: the entry exec'd into it
        ("{parent} {started}", False),  # inherited from the parent: another run's
        ("{own} {future}", False),
        ("{own} soon", False),
        ("", False),
    ],
)
def test_only_the_stamp_for_this_process_counts(stamp, counts, monkeypatch):
    now = time.monotonic()
    monkeypatch.setenv(
        "CLAUDLOBBY_UNIT_STARTED",
        stamp.format(
            own=os.getpid(),
            parent=os.getppid(),
            started=f"{now - 5:.3f}",
            future=f"{now + 60:.3f}",
        ),
    )
    assert (fleet_pulse._run_started() < now - 4) == counts


def test_the_stop_writes_its_summary_before_the_journals_copy(tmp_path, monkeypatch):
    """The sweep's warnings go on to the unit's journal after the stop, and a slow
    journal can hold that copy past the unit's limit; the summary that records the
    stop is already written by then."""
    summary = tmp_path / "pulse-summary.txt"
    seen = []
    monkeypatch.setattr(
        fleet_pulse, "_forward", lambda warnings: seen.append(summary.read_text())
    )
    _time_out(summary, monkeypatch)
    assert len(seen) == 1 and seen[0].startswith("TIMED OUT"), seen


def test_fleet_yaml_carries_timeout_s_and_keeps_it_off_the_unit_environment():
    pulse = _coerce_fleet_pulse({"timeout_s": "600"})
    assert pulse.timeout_s == 600
    assert pulse.env() == {}  # read from the fleet config, so a hand run gets it too


@pytest.mark.parametrize("value,warns", [(600, False), (10, True), (7200, True)])
def test_the_validator_bounds_timeout_s(value, warns):
    from claudlobby.validator import ValidationReport, _validate_fleet_pulse_cap

    report = ValidationReport()
    _validate_fleet_pulse_cap(
        SimpleNamespace(fleet_pulse=FleetPulseConfig(timeout_s=value)), report
    )
    assert bool(report.warnings) == warns, report.warnings
