"""Each copy of an interval timer is anchored to its own second on the host (#1654).

Identical interval timers on one host fire in the same second. A timer that counts
from its last start (OnUnitActiveSec=) cannot keep copies apart: every start pulled
early by a wake of the user manager, or delayed by load, moves all its later ticks.
So an interval that divides an hour or a day is anchored to the clock (OnCalendar=,
UTC). The minute is split into one band of seconds per slot, fleets in the sorted
overlay order the boot ladder uses and the host's jobs last, and each anchored job
takes its own second in its owner's band: no two units on the host are due in the same
second. Any other interval keeps OnUnitActiveSec= with only a first-run offset.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
from textwrap import dedent

import pytest

from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from tests.package_fixtures import source_package

FLEETS = (
    "fleet-a",
    "fleet-b",
    "fleet-c",
    "fleet-d",
)  # sorted, so slots 0-3; host jobs take slot 4
ACCURACY_S = 1  # every anchored timer's AccuracySec=
# job: (interval, OnCalendar= of fleet-a .. fleet-d) as composed on this four-fleet host. A band
# is 12 s; in each fleet's band keepalive takes +0 s, the pulse +2, check-in +4, recheck +6 and
# log rotation +8. A job longer than a minute runs one minute after the previous fleet's copy.
EXPECTED = {
    "keepalive": (
        60,
        [
            "*-*-* *:*:00 UTC",
            "*-*-* *:*:12 UTC",
            "*-*-* *:*:24 UTC",
            "*-*-* *:*:36 UTC",
        ],
    ),
    "fleet-pulse": (
        300,
        [
            "*-*-* *:00/5:02 UTC",
            "*-*-* *:01/5:14 UTC",
            "*-*-* *:02/5:26 UTC",
            "*-*-* *:03/5:38 UTC",
        ],
    ),
    "manager-checkin": (
        900,
        [
            "*-*-* *:10/15:04 UTC",
            "*-*-* *:11/15:16 UTC",
            "*-*-* *:12/15:28 UTC",
            "*-*-* *:13/15:40 UTC",
        ],
    ),
    "task-recheck": (
        21600,
        [
            "*-*-* 00/6:15:06 UTC",
            "*-*-* 00/6:16:18 UTC",
            "*-*-* 00/6:17:30 UTC",
            "*-*-* 00/6:18:42 UTC",
        ],
    ),
    "log-rotation": (
        86400,
        [
            "*-*-* 00:20:08 UTC",
            "*-*-* 00:21:20 UTC",
            "*-*-* 00:22:32 UTC",
            "*-*-* 00:23:44 UTC",
        ],
    ),
}
HOST_PROBE = "claudlobby-plane-host-probe"
ANCHORED = "# Anchored to the clock at this unit's own second on the host."


def _fleet_yaml(name: str, extra: str = "") -> str:
    return (
        dedent(f"""\
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
        + extra
    )


def _write_fleet(fleet_dir: Path, name: str, extra: str = "") -> None:
    (fleet_dir / "library" / "expertise").mkdir(parents=True, exist_ok=True)
    (fleet_dir / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    (fleet_dir / "lib").mkdir(exist_ok=True)
    (fleet_dir / "fleet.yaml").write_text(_fleet_yaml(name, extra))


def _host_root(base: Path, names=FLEETS) -> Path:
    """A data root with *names* as fleets under local/, beside two dirs that are not fleets."""
    root = base / "claudlobby"
    (root / "lib").mkdir(parents=True)
    for name in names:
        _write_fleet(root / "local" / "home" / name, name)
    (root / "local" / "home" / "shared").mkdir()  # no manifest, so no slot
    (root / "local" / "projects" / "spec").mkdir(
        parents=True
    )  # nested, no manifest, so no slot
    return root


def _compose_fleet(root: Path, fleet_dir: Path | None, out: Path) -> Path:
    from claudlobby.composer import compose_fleet_timers

    fleet, merged = load_fleet((fleet_dir or root) / "fleet.yaml")
    paths = Paths(root=root, fleet_dir=fleet_dir, package=source_package())
    return compose_fleet_timers(fleet, paths, merged, output_dir=out)


def _compose_host(root: Path, out: Path) -> Path:
    from claudlobby.composer import compose_host_timers

    return compose_host_timers(
        Paths(root=root, package=source_package()), output_dir=out
    )


def _timer(timers_dir: Path, unit: str) -> list[str]:
    """The [Timer] section of *unit*'s .timer file, one line per item."""
    text = (timers_dir / f"{unit}.timer").read_text()
    return text.split("[Timer]\n", 1)[1].split("\n\n", 1)[0].splitlines()


def _value(lines: list[str], key: str) -> str:
    (value,) = [line.split("=", 1)[1] for line in lines if line.startswith(f"{key}=")]
    return value


def _interval_units(timers_dir: Path):
    """(unit, [Timer] lines) for each interval timer composed in *timers_dir*."""
    for path in sorted(timers_dir.glob("*.timer")):
        if "-- tick every" in path.read_text():
            yield path.stem, _timer(timers_dir, path.stem)


def _seconds_of_minute(expression: str) -> set[int]:
    """The seconds of the minute an OnCalendar= expression of the anchored form names."""
    second = expression.split()[1].split(":")[2]
    start, _, step = second.partition("/")
    return (
        {int(start) + k * int(step) for k in range(60 // int(step))}
        if step
        else {int(start)}
    )


def _elapses(out: str) -> list[datetime]:
    """The elapses `systemd-analyze calendar` printed under TZ=UTC: every line whose
    value is a UTC timestamp. The labels are not read, because systemd versions name
    the later iterations differently ("Iter. #2:", "Iteration #2:")."""
    found = []
    for line in out.splitlines():
        _, sep, value = line.strip().partition(": ")
        if sep:
            try:
                found.append(datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S UTC"))
            except ValueError:
                pass
    return found


# `systemd-analyze calendar --iterations=3 "*-*-* *:*:00 UTC"` under TZ=UTC, as printed
# by systemd 252 (Debian 12) and by the CI runner's (Ubuntu 24.04).
CALENDAR_OUTPUTS = {
    "systemd-252": dedent(
        """\
        Normalized form: *-*-* *:*:00 UTC
            Next elapse: Wed 2026-10-07 19:06:00 UTC
               From now: 51s left
               Iter. #2: Wed 2026-10-07 19:07:00 UTC
               From now: 1min 51s left
               Iter. #3: Wed 2026-10-07 19:08:00 UTC
               From now: 2min 51s left
        """
    ),
    "ubuntu-24.04": dedent(
        """\
        Normalized form: *-*-* *:*:00 UTC
            Next elapse: Wed 2026-10-07 19:01:00 UTC
               From now: 41s left
           Iteration #2: Wed 2026-10-07 19:02:00 UTC
               From now: 1min 41s left
           Iteration #3: Wed 2026-10-07 19:03:00 UTC
               From now: 2min 41s left
        """
    ),
}


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    """Four fleets and the host's own jobs, composed once on one data root."""
    base = tmp_path_factory.mktemp("timer-phase")
    root = _host_root(base)
    timers = {
        name: _compose_fleet(root, root / "local" / "home" / name, base / "out" / name)
        for name in FLEETS
    }
    timers["host"] = _compose_host(root, base / "out" / "host")
    return root, timers


def test_two_fleets_anchor_one_job_at_different_seconds_and_one_cadence(host):
    _, timers = host
    a, b = (
        _timer(timers["fleet-a"], "com.fleet-a.keepalive"),
        _timer(timers["fleet-b"], "com.fleet-b.keepalive"),
    )
    assert _value(a, "OnCalendar") != _value(b, "OnCalendar")
    assert int(_value(a, "AccuracySec")) == int(_value(b, "AccuracySec")) == ACCURACY_S
    assert not any(
        line.startswith(("OnActiveSec=", "OnUnitActiveSec=", "Persistent="))
        for line in a + b
    )
    assert (
        "-- tick every 60s"
        in (timers["fleet-b"] / "com.fleet-b.keepalive.timer").read_text()
    )


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_each_fleet_s_copy_of_a_job_runs_in_its_own_slot(host, job):
    _, timers = host
    _, expressions = EXPECTED[job]
    assert [
        _value(_timer(timers[name], f"com.{name}.{job}"), "OnCalendar")
        for name in FLEETS
    ] == expressions


def test_no_two_interval_units_on_the_host_start_in_the_same_second(host):
    _, timers = host
    owners: dict[int, list[str]] = {}
    units = [
        (unit, lines)
        for directory in timers.values()
        for unit, lines in _interval_units(directory)
    ]
    assert len(units) == 4 * len(EXPECTED) + 1, [unit for unit, _ in units]
    for unit, lines in units:
        for second in _seconds_of_minute(_value(lines, "OnCalendar")):
            owners.setdefault(second, []).append(unit)
    assert all(len(names) == 1 for names in owners.values()), {
        s: n for s, n in owners.items() if len(n) > 1
    }


def test_the_host_probe_takes_the_band_after_the_last_fleet_s(host):
    _, timers = host
    probe = _timer(timers["host"], HOST_PROBE)
    assert _value(probe, "OnCalendar") == "*-*-* *:*:48 UTC"
    # The five share a minute, so they must stay apart all the way round it.
    starts = sorted(
        second
        for expression in EXPECTED["keepalive"][1] + ["*-*-* *:*:48 UTC"]
        for second in _seconds_of_minute(expression)
    )
    gaps = [b - a for a, b in zip(starts, starts[1:])] + [starts[0] + 60 - starts[-1]]
    assert min(gaps) > 10, gaps


def test_an_anchored_unit_says_so(host):
    _, timers = host
    assert _timer(timers["fleet-c"], "com.fleet-c.fleet-pulse")[0] == ANCHORED
    assert _timer(timers["host"], HOST_PROBE)[0] == ANCHORED


def test_calendar_jobs_are_left_as_declared(host):
    _, timers = host
    for job in ("creds-check", "reload-fleet", "data-sweep", "weekly-worker-restart"):
        sections = {tuple(_timer(timers[name], f"com.{name}.{job}")) for name in FLEETS}
        assert len(sections) == 1, (job, sections)
        (section,) = sections
        assert any(line.startswith("OnCalendar=") for line in section), section
        assert not any(line.startswith(("OnActiveSec=", "#")) for line in section), (
            section
        )


def test_launchd_keeps_its_interval_and_has_no_slot(host):
    """A known asymmetry: StartCalendarInterval has no seconds, so plists keep StartInterval."""
    _, timers = host
    for name in FLEETS:
        plist = (timers[name] / f"com.{name}.keepalive.plist").read_text()
        assert "<key>StartInterval</key>\n  <integer>60</integer>" in plist
        assert "RunAtLoad" not in plist and "StartCalendarInterval" not in plist


def test_a_fleet_composes_the_same_units_alone_and_again(host, tmp_path):
    root, timers = host
    again = _compose_fleet(
        root, root / "local" / "home" / "fleet-c", tmp_path / "again"
    )
    names = sorted(path.name for path in timers["fleet-c"].glob("*.timer"))
    assert names == sorted(path.name for path in again.glob("*.timer")) and names
    for name in names:
        assert (again / name).read_text() == (timers["fleet-c"] / name).read_text(), (
            name
        )


@pytest.mark.parametrize("where", ["root mode", "an overlay outside local/"])
def test_a_fleet_with_no_place_under_local_takes_slot_0(tmp_path, where):
    root = _host_root(tmp_path)
    if where == "root mode":
        _write_fleet(root, "outside")
        fleet_dir = None
    else:
        fleet_dir = tmp_path / "elsewhere"
        _write_fleet(fleet_dir, "outside")
    timers = _compose_fleet(root, fleet_dir, tmp_path / "out")
    assert _timer(timers, "com.outside.keepalive") == [
        ANCHORED,
        "OnCalendar=*-*-* *:*:00 UTC",
        "AccuracySec=1",
    ]
    assert _timer(timers, "com.outside.task-recheck") == [
        ANCHORED,
        "OnCalendar=*-*-* 00/6:15:06 UTC",
        "AccuracySec=1",
    ]


def test_a_fleet_overlay_reached_through_a_symlink_keeps_its_slot(tmp_path):
    """The fleet list and the fleet's own path both resolve, so a linked overlay counts once, in its place."""
    root = _host_root(tmp_path, names=("fleet-a", "fleet-b", "fleet-d"))
    target = tmp_path / "elsewhere" / "fleet-c"
    _write_fleet(target, "fleet-c")
    (root / "local" / "home" / "fleet-c").symlink_to(target, target_is_directory=True)
    timers = _compose_fleet(root, root / "local" / "home" / "fleet-c", tmp_path / "out")
    assert (
        _value(_timer(timers, "com.fleet-c.keepalive"), "OnCalendar")
        == EXPECTED["keepalive"][1][2]
    )


def test_a_one_fleet_host_splits_the_minute_between_its_fleet_and_the_host(tmp_path):
    root = _host_root(tmp_path, names=("solo",))
    timers = _compose_fleet(root, root / "local" / "home" / "solo", tmp_path / "out")
    assert (
        _value(_timer(timers, "com.solo.keepalive"), "OnCalendar") == "*-*-* *:*:00 UTC"
    )
    probe = _timer(_compose_host(root, tmp_path / "host"), HOST_PROBE)
    assert _value(probe, "OnCalendar") == "*-*-* *:*:30 UTC"


def test_an_interval_that_does_not_divide_an_hour_keeps_the_interval_form(tmp_path):
    """No clock points repeat every 7 minutes, so this pulse keeps OnUnitActiveSec= and only a first-run slot."""
    root = _host_root(tmp_path, names=("fleet-a",))
    _write_fleet(
        root / "local" / "home" / "fleet-b",
        "fleet-b",
        "  defaults:\n    observability:\n      pulse_interval: 420\n",
    )
    timers = _compose_fleet(root, root / "local" / "home" / "fleet-b", tmp_path / "out")
    # Three slots (two fleets and the host): a 150 s spread, 50 s a slot.
    assert _timer(timers, "com.fleet-b.fleet-pulse") == [
        "# First run: a 330 s startup delay, plus 50 s for this unit's slot on the host.",
        "OnActiveSec=380",
        "OnUnitActiveSec=420",
        "AccuracySec=10",
    ]
    assert (
        _value(_timer(timers, "com.fleet-b.keepalive"), "OnCalendar")
        == "*-*-* *:*:20 UTC"
    )


def test_an_anchored_job_never_catches_up_a_missed_run(tmp_path):
    root = _host_root(tmp_path, names=("fleet-a",))
    _write_fleet(
        root / "local" / "home" / "fleet-b",
        "fleet-b",
        "  defaults:\n    jobs:\n      keepalive:\n        persistent: true\n",
    )
    timers = _compose_fleet(root, root / "local" / "home" / "fleet-b", tmp_path / "out")
    assert "Persistent=true" not in _timer(timers, "com.fleet-b.keepalive")


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 6, 8])
def test_n_fleets_keepalives_and_the_probe_start_60_over_n_plus_1_s_apart(
    tmp_path, count
):
    """12 s apart for four fleets, 10 s for five, 8 s for six: the band shrinks as fleets are added."""
    from claudlobby.composer import _calendar_seconds, _host_timer_slot

    names = [f"fleet-{index}" for index in range(count)]
    root = _host_root(tmp_path, names=names)
    jobs = {"keepalive": {"type": "interval", "seconds": 60, "startup": 60}}
    slots = [
        _host_timer_slot(
            Paths(
                root=root,
                fleet_dir=root / "local" / "home" / name,
                package=source_package(),
            )
        )
        for name in names
    ]
    slots.append(
        _host_timer_slot(Paths(root=root, package=source_package()), host=True)
    )
    starts = [_calendar_seconds(jobs, slot)["keepalive"] for slot in slots]
    assert starts == sorted(starts) and starts[0] == 0
    gaps = [b - a for a, b in zip(starts, starts[1:])] + [starts[0] + 60 - starts[-1]]
    assert min(gaps) == 60 // (count + 1), gaps


@pytest.mark.parametrize(
    "interval, position, expression",
    [
        (60, 12, "*-*-* *:*:12 UTC"),
        (120, 74, "*-*-* *:01/2:14 UTC"),
        (300, 62, "*-*-* *:01/5:02 UTC"),
        (900, 604, "*-*-* *:10/15:04 UTC"),
        (3600, 1206, "*-*-* *:20:06 UTC"),
        (7200, 3906, "*-*-* 01/2:05:06 UTC"),
        (21600, 906, "*-*-* 00/6:15:06 UTC"),
        (86400, 1208, "*-*-* 00:20:08 UTC"),
    ],
)
def test_an_anchored_interval_names_its_point_in_each_cycle(
    interval, position, expression
):
    from claudlobby.composer import _anchored, _interval_calendar

    assert _anchored(interval) and _interval_calendar(interval, position) == expression


@pytest.mark.parametrize("interval", [1, 30, 45, 90, 420, 5400, 172800])
def test_an_interval_with_no_clock_points_is_not_anchored(interval):
    from claudlobby.composer import _anchored

    assert not _anchored(interval)


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="needs systemd-analyze"
)
@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_systemd_reads_each_anchored_expression_as_one_run_per_interval(job):
    interval, expressions = EXPECTED[job]
    for expression in expressions:
        # systemd-analyze prints each elapse in the local zone, with an extra
        # "(in UTC)" line only when that zone is not UTC: pin the zone.
        out = subprocess.run(
            ["systemd-analyze", "calendar", "--iterations=3", expression],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "TZ": "UTC"},
        ).stdout
        elapses = _elapses(out)
        assert len(elapses) == 3, out
        assert [(b - a).total_seconds() for a, b in zip(elapses, elapses[1:])] == [
            interval,
            interval,
        ], out
        assert all(at.second in _seconds_of_minute(expression) for at in elapses), out


@pytest.mark.parametrize(
    ("version", "minutes"), [("systemd-252", [6, 7, 8]), ("ubuntu-24.04", [1, 2, 3])]
)
def test_the_elapses_are_read_whatever_systemd_labels_them(version, minutes):
    assert _elapses(CALENDAR_OUTPUTS[version]) == [
        datetime(2026, 10, 7, 19, minute) for minute in minutes
    ]


@pytest.mark.parametrize("slots", [2, 5, 12])
@pytest.mark.parametrize("interval", [1, 10, 30, 60, 300, 900, 21600, 86400])
def test_an_offset_stays_inside_its_interval_and_moves_a_first_run_by_under_150_s(
    interval, slots
):
    from claudlobby.composer import _interval_phase_s

    offsets = [_interval_phase_s(interval, (index, slots)) for index in range(slots)]
    assert offsets[0] == 0 and offsets == sorted(offsets)
    assert all(0 <= offset < interval for offset in offsets), offsets
    assert offsets[-1] < 150, offsets
