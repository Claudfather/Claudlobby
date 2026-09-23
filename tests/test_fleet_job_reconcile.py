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

import pytest

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


# ---------------------------------------------------------------------------
# Component C — a TORN DECLARATION is not a teardown (#1765 review)
#
# The blocking finding: the guard inside `_prune_stale_units` compares
# `len(composed)` against `n_expected`, and both derive from the merged job
# set — so it catches a torn WRITE LOOP and is blind to a torn DECLARED COUNT.
# Empty the merged set and `n_expected` is 0, the documented signature of a
# legitimate full removal. Measured route: a built wheel shipped no
# system.yaml at all, so every non-editable install produced this state.
# ---------------------------------------------------------------------------
_FLEET_WANTS_TIMERS = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      bots:
        solo:
          expertise: [eng]
"""

# Same fleet, plus a briefing bot — briefing_on keeps compose_fleet_timers PAST
# the early-return branch, so this drives the OTHER call site. The reviewer's
# repro only reached the first one; both delete.
_FLEET_WANTS_TIMERS_AND_BRIEFS = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      bots:
        solo:
          expertise: [eng]
          briefing:
            slots:
              morning: "*-*-* 08:30:00"
"""


@pytest.fixture
def torn_system_yaml():
    """Make `_load_system_defaults` answer `{}` — what a missing, unreadable or
    empty package system.yaml produced before it learned to refuse.

    Pokes the module-level cache rather than the file, deliberately: that keeps
    this the COMPOSER's test. It proves the prune refuses however the data got
    torn, so it stays honest even though the loader now raises on the one route
    we know about. The loader's own refusal is tested separately.
    """
    import claudlobby.config as config_mod

    cache = config_mod._load_system_defaults.__defaults__[0]
    saved = dict(cache)
    cache.clear()
    cache["data"] = {}
    try:
        yield
    finally:
        cache.clear()
        cache.update(saved)


class TestTornDeclarationRefuses:
    def _seed_prior_generate(self, timers: Path) -> list[str]:
        _seed(timers, "com.test.keepalive", "com.test.fleet-pulse")
        return sorted(p.name for p in timers.iterdir())

    def test_early_return_path_refuses(self, tmp_path, torn_system_yaml, caplog):
        fleet, md = load_fleet(_write(tmp_path / "f", _FLEET_WANTS_TIMERS))
        paths = _make_paths(tmp_path / "f")
        timers = paths.runtime_fleet / "timers"
        timers.mkdir(parents=True)
        before = self._seed_prior_generate(timers)
        # The fleet still ASKS for its job timers; only the merged set is empty.
        assert fleet.system_defaults.timers is True
        assert md.get("jobs") == {}
        with caplog.at_level(logging.WARNING):
            compose_fleet_timers(fleet, paths, md)
        after = sorted(p.name for p in timers.iterdir())
        assert set(before) <= set(after), "a torn declaration deleted live units"
        assert any("torn declaration" in r.message for r in caplog.records)

    def test_write_path_refuses_too(self, tmp_path, torn_system_yaml):
        # briefing_on skips the early return, so this is the second call site.
        fleet, md = load_fleet(
            _write(tmp_path / "f", _FLEET_WANTS_TIMERS_AND_BRIEFS)
        )
        paths = _make_paths(tmp_path / "f")
        timers = paths.runtime_fleet / "timers"
        timers.mkdir(parents=True)
        before = self._seed_prior_generate(timers)
        compose_fleet_timers(fleet, paths, md)
        after = sorted(p.name for p in timers.iterdir())
        assert set(before) <= set(after), "a torn declaration deleted live units"

    @pytest.mark.parametrize(
        "body,off_field",
        [
            (_FLEET_NO_TIMERS, "enabled"),
            (
                """\
                fleet:
                  name: test-fleet
                  service_prefix: com.test
                  system_defaults:
                    timers: false
                  bots:
                    solo:
                      expertise: [eng]
                """,
                "timers",
            ),
        ],
        ids=["system_defaults-false", "timers-false"],
    )
    def test_a_declared_teardown_still_prunes(self, tmp_path, body, off_field):
        # THE POSITIVE CONTROL, and the reason the guard keys on the fleet's own
        # flag rather than on emptiness: without it the guard could "pass" by
        # refusing every prune, which would silently re-open #1764.
        #
        # Both spellings of a real teardown are driven, because they switch off
        # DIFFERENT fields: `system_defaults: false` clears `enabled` and leaves
        # `timers` at its default True, while `timers: false` clears only
        # `timers`. A guard reading either one alone passes one case and deletes
        # nothing in the other.
        fleet, md = load_fleet(_write(tmp_path / "f", body))
        paths = _make_paths(tmp_path / "f")
        timers = paths.runtime_fleet / "timers"
        timers.mkdir(parents=True)
        self._seed_prior_generate(timers)
        assert getattr(fleet.system_defaults, off_field) is False
        compose_fleet_timers(fleet, paths, md)
        assert not list(timers.glob("com.test.*"))


@pytest.fixture
def stub_pkg_dir(tmp_path, monkeypatch):
    """Point `config.__file__` at an empty dir and clear the defaults cache,
    restoring both afterwards — the save/clear/restore written ONCE, the way
    `torn_system_yaml` above does it, instead of a `finally` per test.
    """
    import claudlobby.config as config_mod

    monkeypatch.setattr(config_mod, "__file__", str(tmp_path / "config.py"))
    cache = config_mod._load_system_defaults.__defaults__[0]
    saved = dict(cache)
    cache.clear()
    try:
        yield tmp_path, config_mod, cache
    finally:
        cache.clear()
        cache.update(saved)


class TestLoaderRefusesATornRead:
    """The source-side half: the loader must not hand back a void as data."""

    def test_missing_system_yaml_raises(self, stub_pkg_dir):
        _, config_mod, _ = stub_pkg_dir
        with pytest.raises(RuntimeError, match="system.yaml is missing"):
            config_mod._load_system_defaults()

    def test_empty_system_yaml_raises(self, stub_pkg_dir):
        pkg, config_mod, _ = stub_pkg_dir
        (pkg / "system.yaml").write_text("")
        with pytest.raises(RuntimeError, match="empty or parses to nothing"):
            config_mod._load_system_defaults()

    def test_present_but_unreadable_raises_a_NAMED_error(self, stub_pkg_dir):
        # A bare `path.open()` would surface a raw OSError here — the one torn
        # read that escapes without the diagnosis the other two give.
        pkg, config_mod, _ = stub_pkg_dir
        bad = pkg / "system.yaml"
        bad.write_text("defaults: {}\n")
        bad.chmod(0o000)
        try:
            with pytest.raises(RuntimeError, match="could not be read"):
                config_mod._load_system_defaults()
        finally:
            bad.chmod(0o644)

    def test_unparseable_system_yaml_raises_a_NAMED_error(self, stub_pkg_dir):
        pkg, config_mod, _ = stub_pkg_dir
        (pkg / "system.yaml").write_text("defaults: {oh: [no\n")
        with pytest.raises(RuntimeError, match="could not be read"):
            config_mod._load_system_defaults()

    def test_refusal_caches_nothing(self, stub_pkg_dir):
        # A poisoned cache would make a repaired install keep failing.
        pkg, config_mod, cache = stub_pkg_dir
        with pytest.raises(RuntimeError):
            config_mod._load_system_defaults()
        assert "data" not in cache
        (pkg / "system.yaml").write_text("defaults: {jobs: {}}\n")
        assert config_mod._load_system_defaults() == {"defaults": {"jobs": {}}}


class TestAnOptedOutFleetIsNotCollateral:
    """A fleet that declared `system_defaults: false` wants NOTHING from the
    package file, so a torn read of it must not break that fleet.

    Refusing in the loader is right; calling the loader unconditionally was not.
    `load_fleet` runs for `status`, `validate` and `diff` as well as `generate`,
    so an unconditional refusal would take the diagnostic commands down on
    exactly the broken-install host an operator is running them to diagnose —
    turning a scoped defect into a host-wide outage. Host-scoped readers keep
    asking unconditionally, which is correct: no fleet flag opts a HOST out of
    its own platform equipment.
    """

    _OPTED_OUT = """\
        fleet:
          name: optout
          service_prefix: com.optout
          system_defaults: false
          bots:
            solo:
              expertise: [eng]
    """

    def test_load_fleet_survives_a_missing_system_yaml(self, tmp_path, stub_pkg_dir):
        fleet, md = load_fleet(_write(tmp_path / "f", self._OPTED_OUT))
        assert fleet.system_defaults.enabled is False
        assert md.get("jobs", {}) == {}

    def test_the_opt_out_still_sheds_its_units(self, tmp_path, stub_pkg_dir):
        # ...and the teardown it declared still happens. Scoping the loader call
        # must not smuggle the torn-input refusal onto a legitimate opt-out.
        fleet, md = load_fleet(_write(tmp_path / "f", self._OPTED_OUT))
        paths = _make_paths(tmp_path / "f")
        timers = paths.runtime_fleet / "timers"
        timers.mkdir(parents=True)
        _seed(timers, "com.optout.keepalive")
        compose_fleet_timers(fleet, paths, md)
        assert not list(timers.glob("com.optout.*"))

    def test_host_readers_still_refuse(self, stub_pkg_dir):
        # The other half of the scoping decision, pinned: host jobs and boot
        # policy are NOT fleet-optional, so their readers must stay fatal.
        _, config_mod, _ = stub_pkg_dir
        with pytest.raises(RuntimeError, match="system.yaml is missing"):
            config_mod.load_host_jobs()
        with pytest.raises(RuntimeError, match="system.yaml is missing"):
            config_mod.load_host_boot()


class TestSystemYamlIsPackaged:
    def test_package_data_declares_system_yaml(self):
        # The measured route into the torn state: system.yaml was absent from
        # package-data, so a built wheel shipped without it and every
        # non-editable install read empty system defaults.
        import tomllib

        root = Path(__file__).resolve().parent.parent
        data = tomllib.loads((root / "pyproject.toml").read_text())
        pkg_data = data["tool"]["setuptools"]["package-data"]
        assert "system.yaml" in pkg_data.get("claudlobby", [])


class TestDormantJobsSurviveThePrune:
    """A composed-but-dormant job (``enroll: false``) is COMPOSED, so the prune
    must leave it alone.

    The #1765 review verified this by reading the write loop and driving a real
    fleet, and found it correct — every job in ``timers`` is written AND added
    to ``composed_jobs`` regardless of its enroll flag, which only filters the
    separate DORMANT manifest. Nothing pinned it, so the obvious "tidy-up"
    (adding to ``composed_jobs`` only when enrolled) would delete exactly the
    units an operator staged to arm later, and every existing test would stay
    green. This is that pin.

    Mutation-checked, and the first attempt was a false pin worth recording:
    gating ONLY ``composed_jobs.add`` on the enroll flag does not delete
    anything, because ``n_expected_jobs`` still counts the dormant job, so
    ``len(composed) < n_expected`` fires and the partial-compose guard skips the
    whole prune. These tests passed under that mutant while two unrelated ones
    failed — the mutant's real effect is "the prune stops working", not "dormant
    units are deleted". The mutation that actually deletes them treats dormant
    as undeclared on BOTH sides (the consistent tidy-up), and only these two
    tests fail under it. So the property has two independent nets: the write
    loop counting dormant jobs as composed, and the partial guard catching any
    one-sided drift between the two.
    """

    _FLEET = """\
        fleet:
          name: test-fleet
          service_prefix: com.test
          defaults:
            jobs:
              keepalive:
                enroll: false
          bots:
            solo:
              expertise: [eng]
    """

    def test_dormant_job_units_are_not_pruned(self, tmp_path):
        fleet, md = load_fleet(_write(tmp_path / "f", self._FLEET))
        paths = _make_paths(tmp_path / "f")
        timers = compose_fleet_timers(fleet, paths, md)
        assert md["jobs"]["keepalive"].get("enroll") is False, "fixture drifted"
        for ext in ("service", "timer", "plist"):
            assert (timers / f"com.test.keepalive.{ext}").exists(), (
                "a dormant job is composed, so the prune must not delete it"
            )
        listed = (timers / "DORMANT").read_text().splitlines()
        assert "com.test.keepalive" in listed

    def test_dormant_job_survives_a_second_generate(self, tmp_path):
        # The live shape: units land on one generate, the next must not eat them.
        fleet, md = load_fleet(_write(tmp_path / "f", self._FLEET))
        paths = _make_paths(tmp_path / "f")
        compose_fleet_timers(fleet, paths, md)
        timers = compose_fleet_timers(fleet, paths, md)
        assert (timers / "com.test.keepalive.timer").exists()


class TestDiscriminatorReadsTheUnfilteredSet:
    """`jobs_declaration_torn` reads `merged_defaults["jobs"]`, NOT the local
    `timers` — which has already had `LEAF_MANAGER_GATED_JOBS` stripped.

    Swapping the two survives every other test in this file, because today's
    system.yaml ships nine default jobs and only `manager-checkin` is gated, so
    the filtered set cannot currently reach empty by that route. An inert mutant
    is not a harmless one: nothing would pin WHY the line reads the unfiltered
    set, and the obvious tidy-up ("`timers` is right there") reintroduces the
    bug the day the job roster shrinks. This builds the roster that makes it
    live, so the mutant dies.

    The misfire direction is the safe one — refusing a prune that was owed, not
    deleting — which is exactly why it would go unnoticed: #1764 simply comes
    back, silently, for a fleet with no leaf manager.
    """

    _ONLY_GATED_JOBS = (
        "defaults:\n"
        "  jobs:\n"
        "    manager-checkin:\n"
        "      script: $CLAUDLOBBY_ROOT/lib/manager-checkin.sh\n"
        "      interval: 900\n"
        "      type: oneshot\n"
    )

    # No `manages:` anywhere, so the fleet has no leaf manager and the gated
    # job is filtered out of `timers` — leaving it EMPTY while the merged set
    # it came from is not.
    _NO_LEAF_MANAGER = """\
        fleet:
          name: test-fleet
          service_prefix: com.test
          bots:
            solo:
              expertise: [eng]
    """

    def test_a_fully_gated_roster_is_not_a_torn_declaration(
        self, tmp_path, stub_pkg_dir
    ):
        pkg, _, _ = stub_pkg_dir
        (pkg / "system.yaml").write_text(self._ONLY_GATED_JOBS)
        fleet, md = load_fleet(_write(tmp_path / "f", self._NO_LEAF_MANAGER))
        # The precondition this test exists for: merged set non-empty, and the
        # composer's post-filter view of it empty.
        assert md["jobs"] == {"manager-checkin": md["jobs"]["manager-checkin"]}
        assert not fleet.leaf_manager_bots()
        assert fleet.system_defaults.enabled and fleet.system_defaults.timers

        paths = _make_paths(tmp_path / "f")
        timers = paths.runtime_fleet / "timers"
        timers.mkdir(parents=True)
        _seed(timers, "com.test.plane-shadow")
        compose_fleet_timers(fleet, paths, md)
        assert not (timers / "com.test.plane-shadow.timer").exists(), (
            "a fully leaf-manager-gated roster is a real empty compose, not a "
            "torn declaration — the prune is owed and must still run"
        )
