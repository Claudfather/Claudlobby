"""Each copy of an interval timer gets its own slot on the host (#1654).

Identical interval timers on one host fire in the same second. An activation
counts every timer's first run from its last daemon-reload, one instant for the
whole host, and each later tick counts from the job's last start. The composer
therefore adds an offset to each interval job's first-run delay (OnActiveSec=),
per fleet in the sorted overlay order the boot ladder uses, with host jobs in
the slot after the last fleet's.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from tests.package_fixtures import source_package

FLEETS = ("fleet-a", "fleet-b", "fleet-c", "fleet-d")  # sorted, so slots 0-3; host jobs take slot 4
ACCURACY_S = 10  # every interval timer's AccuracySec=
# job: (interval, first run of fleet-a .. fleet-d) as composed on this four-fleet host
EXPECTED = {
    "keepalive": (60, [60, 72, 84, 96]),
    "fleet-pulse": (300, [330, 360, 390, 420]),
    "manager-checkin": (900, [615, 645, 675, 705]),
    "task-recheck": (21600, [945, 975, 1005, 1035]),
    "log-rotation": (86400, [1230, 1260, 1290, 1320]),
}
HOST_PROBE = "claudlobby-plane-host-probe"


def _fleet_yaml(name: str) -> str:
    return dedent(f"""\
        fleet:
          name: {name}
          manager: lead
          service_prefix: com.{name}
          bots:
            lead:
              expertise: [eng]
            worker:
              expertise: [eng]
        """)


def _write_fleet(fleet_dir: Path, name: str) -> None:
    (fleet_dir / "library" / "expertise").mkdir(parents=True, exist_ok=True)
    (fleet_dir / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    (fleet_dir / "lib").mkdir(exist_ok=True)
    (fleet_dir / "fleet.yaml").write_text(_fleet_yaml(name))


def _host_root(base: Path, names=FLEETS) -> Path:
    """A data root with *names* as fleets under local/, beside two dirs that are not fleets."""
    root = base / "claudlobby"
    (root / "lib").mkdir(parents=True)
    for name in names:
        _write_fleet(root / "local" / "home" / name, name)
    (root / "local" / "home" / "shared").mkdir()  # no manifest, so no slot
    (root / "local" / "projects" / "spec").mkdir(parents=True)  # nested, no manifest, so no slot
    return root


def _compose_fleet(root: Path, fleet_dir: Path | None, out: Path) -> Path:
    from claudlobby.composer import compose_fleet_timers

    fleet, merged = load_fleet((fleet_dir or root) / "fleet.yaml")
    paths = Paths(root=root, fleet_dir=fleet_dir, package=source_package())
    return compose_fleet_timers(fleet, paths, merged, output_dir=out)


def _compose_host(root: Path, out: Path) -> Path:
    from claudlobby.composer import compose_host_timers

    return compose_host_timers(Paths(root=root, package=source_package()), output_dir=out)


def _timer(timers_dir: Path, unit: str) -> list[str]:
    """The [Timer] section of *unit*'s .timer file, one line per item."""
    text = (timers_dir / f"{unit}.timer").read_text()
    return text.split("[Timer]\n", 1)[1].split("\n\n", 1)[0].splitlines()


def _value(lines: list[str], key: str) -> int:
    (value,) = [line.split("=", 1)[1] for line in lines if line.startswith(f"{key}=")]
    return int(value)


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    """Four fleets and the host's own jobs, composed once on one data root."""
    base = tmp_path_factory.mktemp("timer-phase")
    root = _host_root(base)
    timers = {name: _compose_fleet(root, root / "local" / "home" / name, base / "out" / name)
              for name in FLEETS}
    timers["host"] = _compose_host(root, base / "out" / "host")
    return root, timers


def test_two_fleets_on_one_host_compose_different_first_runs_at_one_cadence(host):
    _, timers = host
    a, b = _timer(timers["fleet-a"], "com.fleet-a.keepalive"), _timer(timers["fleet-b"], "com.fleet-b.keepalive")
    assert _value(b, "OnActiveSec") - _value(a, "OnActiveSec") > ACCURACY_S
    assert _value(a, "OnUnitActiveSec") == _value(b, "OnUnitActiveSec") == 60
    assert _value(a, "AccuracySec") == _value(b, "AccuracySec") == ACCURACY_S


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_each_fleet_s_copy_of_a_job_first_runs_one_step_after_the_previous_fleet_s(host, job):
    _, timers = host
    interval, first_runs = EXPECTED[job]
    units = [_timer(timers[name], f"com.{name}.{job}") for name in FLEETS]
    assert [_value(lines, "OnActiveSec") for lines in units] == first_runs
    assert {_value(lines, "OnUnitActiveSec") for lines in units} == {interval}


def test_the_host_probe_takes_the_slot_after_the_last_fleet_s_keepalive(host):
    _, timers = host
    probe = _timer(timers["host"], HOST_PROBE)
    assert _value(probe, "OnActiveSec") == 108 and _value(probe, "OnUnitActiveSec") == 60
    # The five share keepalive's cadence, so they must stay apart all the way round the minute.
    first_runs = EXPECTED["keepalive"][1] + [108]
    for i, a in enumerate(first_runs):
        for b in first_runs[i + 1:]:
            gap = abs(a - b) % 60
            assert min(gap, 60 - gap) > ACCURACY_S, (a, b)


def test_a_unit_with_an_offset_says_where_its_first_run_comes_from(host):
    _, timers = host
    assert _timer(timers["fleet-c"], "com.fleet-c.fleet-pulse")[0] == (
        "# First run: a 330 s startup delay, plus 60 s for this unit's slot on the host.")
    assert not any(line.startswith("#") for line in _timer(timers["fleet-a"], "com.fleet-a.fleet-pulse"))


def test_calendar_jobs_carry_no_offset(host):
    _, timers = host
    for job in ("creds-check", "reload-fleet", "data-sweep", "weekly-worker-restart"):
        sections = {tuple(_timer(timers[name], f"com.{name}.{job}")) for name in FLEETS}
        assert len(sections) == 1, (job, sections)
        (section,) = sections
        assert any(line.startswith("OnCalendar=") for line in section), section
        assert not any(line.startswith(("OnActiveSec=", "#")) for line in section), section


def test_launchd_has_no_slot(host):
    """A known asymmetry: StartInterval has no first-run delay, so no offset either."""
    _, timers = host
    for name in FLEETS:
        plist = (timers[name] / f"com.{name}.keepalive.plist").read_text()
        assert "<key>StartInterval</key>\n  <integer>60</integer>" in plist
        assert "RunAtLoad" not in plist and "StartCalendarInterval" not in plist


def test_a_fleet_composes_the_same_units_alone_and_again(host, tmp_path):
    root, timers = host
    again = _compose_fleet(root, root / "local" / "home" / "fleet-c", tmp_path / "again")
    names = sorted(path.name for path in timers["fleet-c"].glob("*.timer"))
    assert names == sorted(path.name for path in again.glob("*.timer")) and names
    for name in names:
        assert (again / name).read_text() == (timers["fleet-c"] / name).read_text(), name


@pytest.mark.parametrize("where", ["root mode", "an overlay outside local/"])
def test_a_fleet_with_no_place_under_local_composes_slot_0(tmp_path, where):
    """Its units are the ones composed before slots existed, whatever fleets local/ holds."""
    root = _host_root(tmp_path)
    if where == "root mode":
        _write_fleet(root, "outside")
        fleet_dir = None
    else:
        fleet_dir = tmp_path / "elsewhere"
        _write_fleet(fleet_dir, "outside")
    timers = _compose_fleet(root, fleet_dir, tmp_path / "out")
    assert _timer(timers, "com.outside.keepalive") == ["OnActiveSec=60", "OnUnitActiveSec=60", "AccuracySec=10"]
    assert _timer(timers, "com.outside.task-recheck") == [
        "OnActiveSec=945", "OnUnitActiveSec=21600", "AccuracySec=10"]


def test_a_one_fleet_host_keeps_its_fleet_s_first_runs_and_moves_the_probe_off_them(tmp_path):
    root = _host_root(tmp_path, names=("solo",))
    timers = _compose_fleet(root, root / "local" / "home" / "solo", tmp_path / "out")
    assert _value(_timer(timers, "com.solo.keepalive"), "OnActiveSec") == 60
    probe = _timer(_compose_host(root, tmp_path / "host"), HOST_PROBE)
    assert _value(probe, "OnActiveSec") == 90


@pytest.mark.parametrize("slots", [2, 5, 12])
@pytest.mark.parametrize("interval", [1, 10, 30, 60, 300, 900, 21600, 86400])
def test_an_offset_stays_inside_its_interval_and_moves_a_first_run_by_under_150_s(interval, slots):
    from claudlobby.composer import _interval_phase_s

    offsets = [_interval_phase_s(interval, (index, slots)) for index in range(slots)]
    assert offsets[0] == 0 and offsets == sorted(offsets)
    assert all(0 <= offset < interval for offset in offsets), offsets
    assert offsets[-1] < 150, offsets
