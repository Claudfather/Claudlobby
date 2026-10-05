"""The fleet pulse's time cap (#2059, a stopgap for #907's sweep cost).

The cap is fleet.yaml's ``fleet_pulse.timeout_s``, clamped to its validated range,
else 120 s scaled by load per CPU up to 240 s, under the default 300 s pulse
cadence so an activation finds the shared lock free between ticks. A sweep that
reaches it gets SIGTERM, then SIGKILL, and leaves a summary that says so, with the
last complete one kept beside it.
"""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from claudlobby import fleet_pulse
from claudlobby.config import FleetPulseConfig, _coerce_fleet_pulse


@pytest.mark.parametrize(
    "configured,load1,cpus,cap",
    [
        (None, 0.5, 4, 120),  # an idle host keeps the old cap
        (None, 6.0, 4, 180),  # scaled by load per CPU
        (None, 74.0, 4, 240),  # the heaviest measured load, at the ceiling
        (450, 74.0, 4, 450),  # fleet.yaml wins over the load
        (5, 0.5, 4, 30),  # and is clamped to its validated range
        (7200, 0.5, 4, 3600),
    ],
)
def test_the_cap_is_configured_or_scaled_with_load(configured, load1, cpus, cap):
    got, why = fleet_pulse.sweep_cap(configured, load1, cpus)
    assert got == cap
    assert ("fleet_pulse.timeout_s" in why) == (configured is not None), why


def test_the_scaled_cap_and_its_grace_fit_under_the_default_cadence():
    assert fleet_pulse.MAX_CAP_S + fleet_pulse.TERM_GRACE_S < 300


def _time_out(summary, monkeypatch):
    monkeypatch.setattr(
        fleet_pulse, "sweep_cap", lambda configured, load1, cpus: (1, "a test cap")
    )
    with pytest.raises(fleet_pulse.FleetPulseError) as failure:
        fleet_pulse._sweep(
            ["/bin/sh", "-c", "echo watchdog dark >&2; sleep 30"],
            dict(os.environ),
            summary_path=summary,
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
