"""The fleet pulse's time cap (#907 stopgap, with #2059).

A fixed 120 s cap read host load as a failed unit: 92 sweeps timed out between
2026-10-04 12Z and the outage report, every one at load1 14-74 on four cores, and
each left the previous tick's summary standing. The cap now comes from
fleet.yaml's ``fleet_pulse.timeout_s`` when set, else scales with load per CPU
(120-900 s); a sweep that times out is stopped with SIGTERM before SIGKILL and
leaves a summary that says so.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from types import SimpleNamespace

import pytest

from claudlobby import fleet_pulse
from claudlobby.config import FleetPulseConfig, _coerce_fleet_pulse


@pytest.mark.parametrize(
    "configured,load1,cpus,cap",
    [
        (None, 0.5, 4, 120),  # an idle host keeps the old cap
        (None, 14.0, 4, 420),  # the lightest load a timeout was measured at
        (None, 74.0, 4, 900),  # the heaviest, clamped
        (300, 74.0, 4, 300),  # fleet.yaml wins over the load
    ],
)
def test_the_cap_is_configured_or_scaled_with_load(
    monkeypatch, configured, load1, cpus, cap
):
    monkeypatch.setattr(fleet_pulse.os, "getloadavg", lambda: (load1, load1, load1))
    monkeypatch.setattr(fleet_pulse.os, "cpu_count", lambda: cpus)
    got, why = fleet_pulse.sweep_cap(configured)
    assert got == cap
    assert ("fleet_pulse.timeout_s" in why) == (configured is not None), why


def test_a_timed_out_sweep_records_a_summary_that_says_so(tmp_path):
    summary = tmp_path / "state/pulse/example.pulse-summary.txt"
    summary.parent.mkdir(parents=True)
    summary.write_text("all healthy\n")  # the previous tick's summary
    with pytest.raises(fleet_pulse.FleetPulseError) as failure:
        fleet_pulse._sweep(
            ["/bin/sh", "-c", "echo watchdog dark >&2; sleep 30"],
            dict(os.environ),
            cap=1,
            why="a test cap",
            summary_path=summary,
        )
    assert failure.value.code == "timeout" and failure.value.effect_attempted
    assert str(summary) in str(failure.value)
    text = summary.read_text()
    assert text.startswith("TIMED OUT"), text
    assert "1 s" in text and "a test cap" in text and "watchdog dark" in text
    assert "all healthy" not in text


def test_pulse_fleet_takes_the_cap_from_the_fleet(tmp_path, monkeypatch):
    native = tmp_path / "native"
    native.mkdir()
    (native / "fleet-pulse.sh").touch()
    release = SimpleNamespace(
        native_path=native,
        cli_path=tmp_path / "bin/claudlobby",
        release_id="selected-release",
    )
    fleet = SimpleNamespace(
        name="example", manager="manager", fleet_pulse=FleetPulseConfig(timeout_s=450)
    )
    destination = SimpleNamespace(
        fleet=fleet, paths=SimpleNamespace(root=tmp_path, lib=native)
    )

    @contextmanager
    def admitted(root, **kwargs):
        yield release

    monkeypatch.setattr(fleet_pulse, "mutation_admission", admitted)
    monkeypatch.setattr(fleet_pulse, "native_environment", lambda _paths: {})
    monkeypatch.setattr(
        fleet_pulse, "resolve_operation_scope", lambda **_kwargs: (destination, None)
    )
    seen = {}

    def run(command, env, **kwargs):
        seen.update(kwargs)
        return "ok\n", "", 0

    monkeypatch.setattr(fleet_pulse, "_sweep", run)
    fleet_pulse.pulse_fleet(root=tmp_path, fleet="example")
    assert seen["cap"] == 450
    assert seen["summary_path"] == tmp_path / "state/pulse/example.pulse-summary.txt"


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
