"""Tests for the fleet-job half of the timers-dir reconcile (#1764).

`compose_fleet_timers` only ever WROTE: a job removed from system.yaml left its
composed units on disk, and the setup backbone enrolls what it finds there. The
live case was `plane-shadow` — deleted from system.yaml at the F18 R2a closure
along with the `lib/plane-shadow.sh` it execs, still being re-created as a
LaunchAgent by the nightly reload eighteen days later, exiting 78 (EX_CONFIG)
every night.

The load-bearing property is the CARVE-OUT: the per-(bot,slot) briefing family
is owned by `_reconcile_briefing_units`, which guards its prune with its own
independent count. A sweep that also deleted those would route them past that
guard on a count that knows nothing about them.

Mirrors tests/test_briefing.py — one feature, one cohesive test module.
"""

from __future__ import annotations

import logging
from pathlib import Path
from textwrap import dedent

from claudlobby.composer import _reconcile_fleet_job_units, compose_fleet_timers
from claudlobby.config import load_fleet
from claudlobby.paths import Paths


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root)


def _write(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fleet.yaml").write_text(dedent(body))
    return root / "fleet.yaml"


def _seed(timers_dir: Path, *basenames: str) -> None:
    timers_dir.mkdir(parents=True, exist_ok=True)
    for base in basenames:
        for ext in ("service", "timer", "plist"):
            (timers_dir / f"{base}.{ext}").write_text("# seed\n")


# A fleet that composes the real system.yaml default jobs, plus one briefing
# bot so every test exercises both families in the same directory.
_FLEET = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      bots:
        kev:
          expertise: [eng]
          manages: [mason]
          briefing:
            slots:
              morning: "*-*-* 08:30:00"
        mason:
          expertise: [eng]
"""

# Same fleet with every source of fleet timers switched off — the early-return
# path, where a fleet that turns default timers off must still shed its units.
_FLEET_NO_TIMERS = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      system_defaults: false
      bots:
        solo:
          expertise: [eng]
"""


# ---------------------------------------------------------------------------
# Component A — the reconciler in isolation
# ---------------------------------------------------------------------------
class TestFleetJobReconcile:
    def test_prunes_undeclared_job_keeps_composed(self, tmp_path):
        _seed(tmp_path, "com.test.keepalive", "com.test.plane-shadow")
        pruned = _reconcile_fleet_job_units(
            tmp_path, "com.test", {"com.test.keepalive"}, 1
        )
        assert pruned == ["com.test.plane-shadow"]
        assert (tmp_path / "com.test.keepalive.timer").exists()
        for ext in ("service", "timer", "plist"):
            assert not (tmp_path / f"com.test.plane-shadow.{ext}").exists()

    def test_never_touches_the_briefing_family(self, tmp_path):
        # THE TRAP. Briefing basenames cannot be enumerated in advance and
        # carry their own guarded prune; a job reconcile that swept them would
        # bypass BRIEFING_EXPECTED entirely.
        _seed(
            tmp_path,
            "com.test.plane-shadow",
            "com.test.briefing-kev-morning",
            "com.test.briefing-kev-evening",
        )
        pruned = _reconcile_fleet_job_units(tmp_path, "com.test", set(), 0)
        assert pruned == ["com.test.plane-shadow"]
        assert (tmp_path / "com.test.briefing-kev-morning.timer").exists()
        assert (tmp_path / "com.test.briefing-kev-evening.plist").exists()

    def test_zero_declared_is_a_full_removal(self, tmp_path):
        _seed(tmp_path, "com.test.keepalive", "com.test.fleet-pulse")
        pruned = _reconcile_fleet_job_units(tmp_path, "com.test", set(), 0)
        assert pruned == ["com.test.fleet-pulse", "com.test.keepalive"]
        assert not list(tmp_path.glob("com.test.*"))

    def test_partial_compose_refuses_the_prune(self, tmp_path, caplog):
        # A shortfall is a torn generate, not a teardown: leave everything.
        _seed(tmp_path, "com.test.keepalive", "com.test.fleet-pulse")
        with caplog.at_level(logging.WARNING):
            pruned = _reconcile_fleet_job_units(
                tmp_path, "com.test", {"com.test.keepalive"}, 5
            )
        assert pruned == []
        assert (tmp_path / "com.test.fleet-pulse.timer").exists()
        assert any(
            "partial" in r.message.lower() for r in caplog.records
        ), "a refused prune must say so"

    def test_ignores_manifests_and_foreign_prefixes(self, tmp_path):
        _seed(tmp_path, "com.test.plane-shadow", "com.other.keepalive")
        (tmp_path / "DORMANT").write_text("# manifest\n")
        (tmp_path / "BRIEFING_EXPECTED").write_text("# manifest\n")
        _reconcile_fleet_job_units(tmp_path, "com.test", set(), 0)
        assert (tmp_path / "DORMANT").exists()
        assert (tmp_path / "BRIEFING_EXPECTED").exists()
        assert (tmp_path / "com.other.keepalive.timer").exists()

    def test_missing_dir_is_a_noop(self, tmp_path):
        assert _reconcile_fleet_job_units(tmp_path / "nope", "com.test", set(), 0) == []


# ---------------------------------------------------------------------------
# Component B — end to end through compose_fleet_timers
# ---------------------------------------------------------------------------
class TestGenerateShedsUndeclaredJobs:
    def test_generate_prunes_the_retired_job(self, tmp_path):
        fleet, md = load_fleet(_write(tmp_path / "f", _FLEET))
        paths = _make_paths(tmp_path / "f")
        timers = paths.runtime_fleet / "timers"
        _seed(timers, "com.test.plane-shadow")
        compose_fleet_timers(fleet, paths, md)
        for ext in ("service", "timer", "plist"):
            assert not (timers / f"com.test.plane-shadow.{ext}").exists()
        # ...and every job the config still declares is untouched.
        assert (timers / "com.test.keepalive.timer").exists()
        assert (timers / "com.test.fleet-pulse.timer").exists()

    def test_generate_keeps_briefing_units(self, tmp_path):
        # The carve-out, end to end: a generate that sheds a retired job must
        # leave the briefing family to its own reconciler.
        fleet, md = load_fleet(_write(tmp_path / "f", _FLEET))
        paths = _make_paths(tmp_path / "f")
        timers = paths.runtime_fleet / "timers"
        _seed(timers, "com.test.plane-shadow")
        compose_fleet_timers(fleet, paths, md)
        assert not (timers / "com.test.plane-shadow.timer").exists()
        assert (timers / "com.test.briefing-kev-morning.timer").exists()

    def test_early_return_path_still_sheds(self, tmp_path):
        # system_defaults off, no sweep, no briefing: compose_fleet_timers
        # returns early, and that is exactly the fleet whose stale job units
        # would otherwise never be revisited.
        fleet, md = load_fleet(_write(tmp_path / "f", _FLEET_NO_TIMERS))
        paths = _make_paths(tmp_path / "f")
        timers = paths.runtime_fleet / "timers"
        _seed(timers, "com.test.plane-shadow", "com.test.keepalive")
        compose_fleet_timers(fleet, paths, md)
        assert not list(timers.glob("com.test.*"))

    def test_generate_is_idempotent(self, tmp_path):
        fleet, md = load_fleet(_write(tmp_path / "f", _FLEET))
        paths = _make_paths(tmp_path / "f")
        timers = paths.runtime_fleet / "timers"
        compose_fleet_timers(fleet, paths, md)
        first = sorted(p.name for p in timers.iterdir())
        compose_fleet_timers(fleet, paths, md)
        assert sorted(p.name for p in timers.iterdir()) == first
