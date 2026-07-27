"""Tests for the system defaults tier.

Covers:
- Three-layer merge (system < fleet-defaults < bot-stanza)
- Hook dedup by (command, matcher)
- Opt-out via system_defaults: false and per-category
- Timer generation
- Backwards compat: existing fleet.yaml with manual hooks/observability
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby.config import (
    FleetConfig,
    SystemDefaultsConfig,
    _hook_key,
    _load_system_defaults,
    _merge_hooks_dedup,
    _merge_system_into_defaults,
    load_fleet,
)
from claudlobby.paths import Paths


def _write_fleet(root: Path, fleet_yaml: str) -> Path:
    (root / "library" / "expertise").mkdir(parents=True, exist_ok=True)
    (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    (root / "lib").mkdir(exist_ok=True)
    fleet_path = root / "fleet.yaml"
    fleet_path.write_text(dedent(fleet_yaml))
    return fleet_path


# ---------------------------------------------------------------------------
# _hook_key
# ---------------------------------------------------------------------------


class TestHookKey:
    def test_command_only(self):
        assert _hook_key({"command": "foo.sh"}) == ("foo.sh", "")

    def test_command_and_matcher(self):
        assert _hook_key({"command": "foo.sh", "matcher": "Bash"}) == ("foo.sh", "Bash")

    def test_empty_dict(self):
        assert _hook_key({}) == ("", "")


# ---------------------------------------------------------------------------
# _merge_hooks_dedup
# ---------------------------------------------------------------------------


class TestMergeHooksDedup:
    def test_base_first_ordering(self):
        base = {"PreToolUse": [{"command": "a.sh"}, {"command": "b.sh"}]}
        override = {"PreToolUse": [{"command": "c.sh"}]}
        result = _merge_hooks_dedup(base, override)
        cmds = [e["command"] for e in result["PreToolUse"]]
        assert cmds == ["a.sh", "b.sh", "c.sh"]

    def test_override_wins_on_collision(self):
        base = {"PreToolUse": [{"command": "vitals.sh"}]}
        override = {"PreToolUse": [{"command": "vitals.sh", "timeout": 10}]}
        result = _merge_hooks_dedup(base, override)
        assert len(result["PreToolUse"]) == 1
        assert result["PreToolUse"][0]["timeout"] == 10

    def test_different_matchers_kept(self):
        base = {"PreToolUse": [{"command": "log.sh"}]}
        override = {"PreToolUse": [{"command": "log.sh", "matcher": "Bash"}]}
        result = _merge_hooks_dedup(base, override)
        assert len(result["PreToolUse"]) == 2

    def test_empty_base(self):
        result = _merge_hooks_dedup({}, {"PreToolUse": [{"command": "a.sh"}]})
        assert len(result["PreToolUse"]) == 1

    def test_empty_override(self):
        result = _merge_hooks_dedup({"PreToolUse": [{"command": "a.sh"}]}, {})
        assert len(result["PreToolUse"]) == 1

    def test_events_merged(self):
        base = {"PreToolUse": [{"command": "a.sh"}]}
        override = {"PostToolUse": [{"command": "b.sh"}]}
        result = _merge_hooks_dedup(base, override)
        assert "PreToolUse" in result
        assert "PostToolUse" in result


# ---------------------------------------------------------------------------
# _merge_system_into_defaults
# ---------------------------------------------------------------------------


class TestMergeSystemIntoDefaults:
    def test_system_provides_hooks(self):
        system = {"hooks": {"PreToolUse": [{"command": "sys.sh"}]}}
        result = _merge_system_into_defaults(system, {})
        assert result["hooks"]["PreToolUse"][0]["command"] == "sys.sh"

    def test_fleet_overrides_observability(self):
        system = {"observability": {"pulse_interval": 300, "reap_days": 7}}
        defaults = {"observability": {"pulse_interval": 600}}
        result = _merge_system_into_defaults(system, defaults)
        assert result["observability"]["pulse_interval"] == 600
        assert result["observability"]["reap_days"] == 7

    def test_fleet_non_hook_keys_win(self):
        system = {"model": "sonnet"}
        defaults = {"model": "opus"}
        result = _merge_system_into_defaults(system, defaults)
        assert result["model"] == "opus"

    def test_system_fills_missing_keys(self):
        system = {"observability": {"pulse_interval": 300}}
        result = _merge_system_into_defaults(system, {})
        assert result["observability"]["pulse_interval"] == 300

    def test_hooks_deduped_during_merge(self):
        system = {"hooks": {"PreToolUse": [{"command": "vitals.sh"}]}}
        defaults = {"hooks": {"PreToolUse": [{"command": "vitals.sh", "timeout": 5}]}}
        result = _merge_system_into_defaults(system, defaults)
        # Fleet version wins (override), but only one entry
        assert len(result["hooks"]["PreToolUse"]) == 1
        assert result["hooks"]["PreToolUse"][0].get("timeout") == 5


# ---------------------------------------------------------------------------
# SystemDefaultsConfig parsing
# ---------------------------------------------------------------------------


class TestSystemDefaultsConfig:
    def test_default_all_enabled(self):
        cfg = SystemDefaultsConfig()
        assert cfg.enabled is True
        assert cfg.hooks is True
        assert cfg.timers is True
        assert cfg.observability is True

    def test_false_disables_all(self):
        from claudlobby.config import _coerce_system_defaults

        cfg = _coerce_system_defaults(False)
        assert cfg.enabled is False

    def test_per_category_disable(self):
        from claudlobby.config import _coerce_system_defaults

        cfg = _coerce_system_defaults({"hooks": False, "timers": False})
        assert cfg.enabled is True
        assert cfg.hooks is False
        assert cfg.timers is False
        assert cfg.observability is True

    def test_none_returns_default(self):
        from claudlobby.config import _coerce_system_defaults

        cfg = _coerce_system_defaults(None)
        assert cfg.enabled is True


# ---------------------------------------------------------------------------
# load_fleet integration
# ---------------------------------------------------------------------------


class TestLoadFleetSystemDefaults:
    def test_system_defaults_inject_hooks(self, tmp_path):
        root = tmp_path / "claudlobby"
        fleet_path = _write_fleet(
            root,
            """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                worker:
                  expertise: [eng]
            """,
        )
        fleet, merged = load_fleet(fleet_path)
        bot = fleet.bots["worker"]
        # System hooks should be present
        assert "PreToolUse" in bot.hooks
        cmds = [h["command"] for h in bot.hooks["PreToolUse"]]
        assert "$CLAUDLOBBY_ROOT/lib/bot-vitals.sh" in cmds

    def test_system_defaults_inject_observability(self, tmp_path):
        root = tmp_path / "claudlobby"
        fleet_path = _write_fleet(
            root,
            """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                worker:
                  expertise: [eng]
            """,
        )
        fleet, merged = load_fleet(fleet_path)
        bot = fleet.bots["worker"]
        assert bot.observability.pulse_interval == 300
        assert bot.observability.reap_days == 7
        assert bot.observability.activity_stuck_threshold == 1800
        assert bot.observability.dispatch_deadline == 1800

    def test_fleet_hooks_dedup_with_system(self, tmp_path):
        root = tmp_path / "claudlobby"
        fleet_path = _write_fleet(
            root,
            """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              defaults:
                hooks:
                  PreToolUse:
                    - command: "$CLAUDLOBBY_ROOT/lib/bot-vitals.sh"
                      timeout: 10
              bots:
                worker:
                  expertise: [eng]
            """,
        )
        fleet, merged = load_fleet(fleet_path)
        bot = fleet.bots["worker"]
        pre = bot.hooks["PreToolUse"]
        vitals = [h for h in pre if "bot-vitals.sh" in h.get("command", "")]
        # Deduped: fleet version wins with timeout=10
        assert len(vitals) == 1
        assert vitals[0].get("timeout") == 10

    def test_system_defaults_false_no_injection(self, tmp_path):
        root = tmp_path / "claudlobby"
        fleet_path = _write_fleet(
            root,
            """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              system_defaults: false
              bots:
                worker:
                  expertise: [eng]
            """,
        )
        fleet, merged = load_fleet(fleet_path)
        bot = fleet.bots["worker"]
        assert bot.hooks == {}
        assert bot.observability.pulse_interval is None

    def test_per_category_disable_hooks(self, tmp_path):
        root = tmp_path / "claudlobby"
        fleet_path = _write_fleet(
            root,
            """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              system_defaults:
                hooks: false
                observability: true
              bots:
                worker:
                  expertise: [eng]
            """,
        )
        fleet, merged = load_fleet(fleet_path)
        bot = fleet.bots["worker"]
        # No system hooks injected
        assert bot.hooks == {}
        # But observability is still populated
        assert bot.observability.pulse_interval == 300

    def test_merged_defaults_returned(self, tmp_path):
        root = tmp_path / "claudlobby"
        fleet_path = _write_fleet(
            root,
            """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              defaults:
                observability:
                  pulse_interval: 600
              bots:
                worker:
                  expertise: [eng]
            """,
        )
        fleet, merged = load_fleet(fleet_path)
        # Fleet override wins
        assert merged["observability"]["pulse_interval"] == 600
        # System default fills in missing
        assert merged["observability"]["reap_days"] == 7

    def test_fleet_config_has_system_defaults(self, tmp_path):
        root = tmp_path / "claudlobby"
        fleet_path = _write_fleet(
            root,
            """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              system_defaults:
                timers: false
              bots:
                worker:
                  expertise: [eng]
            """,
        )
        fleet, merged = load_fleet(fleet_path)
        assert fleet.system_defaults.timers is False
        assert fleet.system_defaults.hooks is True


# ---------------------------------------------------------------------------
# system_defaults.yaml file
# ---------------------------------------------------------------------------


class TestSystemDefaultsFile:
    def test_loads_from_package(self):
        raw = _load_system_defaults()
        assert "host" in raw
        d = raw["defaults"]
        assert "hooks" in d
        assert "observability" in d
        assert "jobs" in d
        assert "fleet-pulse" in d["jobs"]
        assert "keepalive" in d["jobs"]
        assert "log-rotation" in d["jobs"]
        assert "creds-check" in d["jobs"]
        assert "weekly-worker-restart" in d["jobs"]


# ---------------------------------------------------------------------------
# Fleet timer generation
# ---------------------------------------------------------------------------


class TestComposeFleetTimers:
    def test_generates_timer_units(self, tmp_path):
        from claudlobby.composer import compose_fleet_timers

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        fleet = FleetConfig(name="test-fleet", service_prefix="com.test")
        merged = _default_merged()

        timers_dir = compose_fleet_timers(fleet, paths, merged)
        assert timers_dir.is_dir()

        # Check fleet-pulse timer
        svc = timers_dir / "com.test.fleet-pulse.service"
        timer = timers_dir / "com.test.fleet-pulse.timer"
        plist = timers_dir / "com.test.fleet-pulse.plist"
        assert svc.is_file()
        assert timer.is_file()
        assert plist.is_file()

        svc_text = svc.read_text()
        assert "Type=oneshot" in svc_text
        assert "fleet-pulse.sh" in svc_text

        timer_text = timer.read_text()
        assert "OnBootSec=300" in timer_text
        assert "OnUnitActiveSec=300" in timer_text

    def test_timer_units_carry_telegram_group_chat_id(self, tmp_path):
        """Fleet timers must carry the fleet Telegram group so scheduled jobs
        (e.g. creds-check) can deliver alerts via tg-post from their minimal
        scheduler env (#542). Both the systemd .service and launchd .plist."""
        from claudlobby.composer import compose_fleet_timers

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        fleet = FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            telegram_group_chat_id="-1009999999999",
        )
        timers_dir = compose_fleet_timers(fleet, paths, _default_merged())

        svc = (timers_dir / "com.test.creds-check.service").read_text()
        assert "Environment=TELEGRAM_GROUP_CHAT_ID=-1009999999999" in svc

        plist = (timers_dir / "com.test.creds-check.plist").read_text()
        assert "<key>TELEGRAM_GROUP_CHAT_ID</key>" in plist
        assert "<string>-1009999999999</string>" in plist

    def test_timer_units_omit_chat_id_when_absent(self, tmp_path):
        """No fleet chat id -> no TELEGRAM_GROUP_CHAT_ID env (chat-less fleets,
        host jobs). The var is only emitted when the fleet declares one."""
        from claudlobby.composer import compose_fleet_timers

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        fleet = FleetConfig(name="test-fleet", service_prefix="com.test")
        timers_dir = compose_fleet_timers(fleet, paths, _default_merged())

        svc = (timers_dir / "com.test.creds-check.service").read_text()
        assert "TELEGRAM_GROUP_CHAT_ID" not in svc

    def test_interval_from_resolves(self, tmp_path):
        from claudlobby.composer import _resolve_timer_schedule

        sched = _resolve_timer_schedule(
            {"interval_from": "observability.pulse_interval"},
            {"observability": {"pulse_interval": 600}},
        )
        assert sched == {"type": "interval", "seconds": 600}

    def test_calendar_schedule(self, tmp_path):
        from claudlobby.composer import _resolve_timer_schedule

        sched = _resolve_timer_schedule(
            {"schedule": "*-*-* 06:00:00"},
            {},
        )
        assert sched == {"type": "calendar", "expression": "*-*-* 06:00:00"}

    def test_static_interval(self, tmp_path):
        from claudlobby.composer import _resolve_timer_schedule

        sched = _resolve_timer_schedule({"interval": 60}, {})
        assert sched == {"type": "interval", "seconds": 60}

    def test_calendar_timer_uses_oncalendar(self, tmp_path):
        from claudlobby.composer import compose_fleet_timers

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        fleet = FleetConfig(name="test-fleet", service_prefix="com.test")

        timers_dir = compose_fleet_timers(fleet, paths, _default_merged())

        timer = timers_dir / "com.test.creds-check.timer"
        assert timer.is_file()
        timer_text = timer.read_text()
        assert "OnCalendar=" in timer_text

        # A daily timer (no weekday in its OnCalendar) gets no launchd Weekday.
        plist_text = (timers_dir / "com.test.creds-check.plist").read_text()
        assert "<key>Weekday</key>" not in plist_text

    def test_all_default_timers_generated(self, tmp_path):
        from claudlobby.composer import compose_fleet_timers

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        fleet = FleetConfig(name="test-fleet", service_prefix="com.test")

        timers_dir = compose_fleet_timers(fleet, paths, _default_merged())

        for name in [
            "fleet-pulse",
            "keepalive",
            "log-rotation",
            "creds-check",
            "reload-fleet",
            "weekly-worker-restart",
        ]:
            assert (timers_dir / f"com.test.{name}.service").is_file()
            assert (timers_dir / f"com.test.{name}.timer").is_file()
            assert (timers_dir / f"com.test.{name}.plist").is_file()

    def test_reload_fleet_daily_timer(self, tmp_path):
        # Mechanism 1 of the fleet update lifecycle: a daily, calendar-scheduled
        # reload-fleet timer that refreshes plugins + composed skills live (no
        # restart). It must compose as a distinct timer alongside the others.
        from claudlobby.composer import compose_fleet_timers

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        fleet = FleetConfig(name="test-fleet", service_prefix="com.test")

        timers_dir = compose_fleet_timers(fleet, paths, _default_merged())

        svc = timers_dir / "com.test.reload-fleet.service"
        timer = timers_dir / "com.test.reload-fleet.timer"
        assert svc.is_file()
        assert timer.is_file()
        assert "reload-fleet.sh" in svc.read_text()
        # Daily cadence is a systemd OnCalendar expression, not an interval.
        assert "OnCalendar=" in timer.read_text()

    def test_timer_units_pin_working_directory_to_root(self, tmp_path):
        """Timer units pin WorkingDirectory=<root> (systemd + launchd twin) so
        jobs never depend on the supervisor's spawn cwd."""
        from claudlobby.composer import compose_fleet_timers

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        fleet = FleetConfig(name="test-fleet", service_prefix="com.test")

        timers_dir = compose_fleet_timers(fleet, paths, _default_merged())

        svc_text = (timers_dir / "com.test.reload-fleet.service").read_text()
        assert f"WorkingDirectory={root}" in svc_text
        plist_text = (timers_dir / "com.test.reload-fleet.plist").read_text()
        assert f"<key>WorkingDirectory</key>\n  <string>{root}</string>" in plist_text

    def test_weekly_worker_restart_timer(self, tmp_path):
        """The weekly worker-restart timer composes with a weekday OnCalendar and
        its script lands in the service ExecStart."""
        from claudlobby.composer import compose_fleet_timers

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        fleet = FleetConfig(name="test-fleet", service_prefix="com.test")

        timers_dir = compose_fleet_timers(fleet, paths, _default_merged())

        timer = timers_dir / "com.test.weekly-worker-restart.timer"
        svc = timers_dir / "com.test.weekly-worker-restart.service"
        assert timer.is_file()
        assert svc.is_file()
        assert "OnCalendar=Sun *-*-* 05:00:00" in timer.read_text()
        svc_text = svc.read_text()
        assert "weekly-worker-restart.sh" in svc_text
        assert "Type=oneshot" in svc_text

        # On macOS the launchd plist must also encode the weekday, else a weekly
        # schedule silently fires daily at 05:00. Sunday -> launchd Weekday 0.
        plist_text = (timers_dir / "com.test.weekly-worker-restart.plist").read_text()
        assert "<key>StartCalendarInterval</key>" in plist_text
        assert "<key>Weekday</key>\n    <integer>0</integer>" in plist_text
        assert "<key>Hour</key>\n    <integer>5</integer>" in plist_text


# ---------------------------------------------------------------------------
# Paths.runtime_fleet
# ---------------------------------------------------------------------------


class TestPathsRuntimeFleet:
    def test_runtime_fleet_property(self, tmp_path):
        paths = Paths(root=tmp_path)
        assert paths.runtime_fleet == tmp_path / "runtime" / "fleet"

    def test_runtime_fleet_with_overlay(self, tmp_path):
        fleet_dir = tmp_path / "local" / "myfleet"
        fleet_dir.mkdir(parents=True)
        paths = Paths(root=tmp_path, fleet_dir=fleet_dir)
        assert paths.runtime_fleet == fleet_dir / "runtime" / "fleet"


# ---------------------------------------------------------------------------
# Phase 1: system.yaml host:/defaults: structure + jobs 3-layer merge
# ---------------------------------------------------------------------------

_NO_OVERRIDE_FLEET = """
fleet:
  name: test-fleet
  service_prefix: com.test
  bots:
    worker:
      expertise: [eng]
"""

_ALL_JOB_NAMES = {
    "fleet-pulse",
    "keepalive",
    "log-rotation",
    "creds-check",
    "reload-fleet",
    "weekly-worker-restart",
    "data-sweep",
}


def _default_merged(**overrides):
    """merged_defaults carrying the system default jobs + observability, as
    ``load_fleet`` produces post-Phase-1 (``compose_fleet_timers`` reads
    ``merged['jobs']``). Test helper for the direct-compose unit tests."""
    section = _load_system_defaults().get("defaults", {})
    merged = {
        "jobs": section.get("jobs", {}),
        "observability": section.get("observability", {}),
    }
    merged.update(overrides)
    return merged


class TestSystemYamlStructure:
    def test_host_and_defaults_sections(self):
        raw = _load_system_defaults()
        assert "host" in raw, "system.yaml must declare a host: section"
        assert "defaults" in raw, "system.yaml must declare a defaults: section"
        d = raw["defaults"]
        assert "hooks" in d
        assert "observability" in d
        assert "jobs" in d
        for name in _ALL_JOB_NAMES:
            assert name in d["jobs"], f"{name} missing from defaults.jobs"

    # host.jobs are host-global singletons, enrolled once by setup-system under
    # a fixed claudlobby-<name> identity, all daily/persistent with 10-min
    # jitter. claude-update stages the Claude binary (semantics of the retired
    # self-generating installer); notify-behind is the F5 source-currency nudge
    # (read-only — the auto-pull is a decoupled opt-in follow-up).
    @pytest.mark.parametrize(
        "job,script,schedule",
        [
            ("claude-update", "update-claude-code.sh", "*-*-* 04:00:00"),
            ("notify-behind", "notify-behind.sh", "*-*-* 08:00:00"),
            ("disk-monitor", "disk-monitor.sh", "*-*-* 05:00:00"),
            ("fleet-memory-check", "fleet-memory-check.sh", "*-*-* 05:30:00"),
            (
                "orphan-browser-reaper",
                "orphan-browser-reaper.sh",
                "*-*-* 05:45:00",
            ),
        ],
    )
    def test_host_jobs_declared(self, job, script, schedule):
        from claudlobby.config import load_host_jobs

        jobs = load_host_jobs()
        assert job in jobs
        cfg = jobs[job]
        assert cfg["script"].endswith(script)
        assert cfg["schedule"] == schedule
        assert cfg["persistent"] is True
        assert cfg["randomized_delay"] == 600

    def test_data_sweep_declared_with_purge_args(self):
        # The default fleet job PURGES (30-day default); a fleet overrides
        # retention by overriding the job's script line (jobs merge by name).
        jobs = _load_system_defaults()["defaults"]["jobs"]
        ds = jobs["data-sweep"]
        assert ds["script"].endswith("data-sweep.sh --purge")
        assert ds["schedule"].startswith("Sat ")


class TestResolveSystemYaml:
    def test_returns_system_yaml_when_present(self, tmp_path):
        from claudlobby.config import _resolve_system_yaml

        (tmp_path / "system.yaml").write_text("defaults: {}\n")
        assert _resolve_system_yaml(tmp_path).name == "system.yaml"

    def test_none_when_absent(self, tmp_path):
        from claudlobby.config import _resolve_system_yaml

        assert _resolve_system_yaml(tmp_path) is None

    def test_fail_loud_on_stale_system_defaults_yaml(self, tmp_path):
        # A lingering system_defaults.yaml (the file was renamed to system.yaml)
        # must fail loud, not be silently ignored — silence would drop every
        # default hook / observability / job.
        from claudlobby.config import _resolve_system_yaml

        (tmp_path / "system_defaults.yaml").write_text("hooks: {}\n")
        with pytest.raises(RuntimeError, match="system.yaml"):
            _resolve_system_yaml(tmp_path)


class TestJobsThreeLayerMerge:
    def test_jobs_merge_is_passthrough_not_dedup(self):
        # jobs merge by name via passthrough/shallow-spread: a fleet entry
        # overrides the system entry of the same name; SIBLINGS are preserved
        # (NOT dropped, as a wholesale "fleet wins" or hook-style dedup would).
        system = {"jobs": {"a": {"interval": 1}, "b": {"interval": 2}}}
        defaults = {"jobs": {"b": {"interval": 20}}}
        merged = _merge_system_into_defaults(system, defaults)
        assert merged["jobs"] == {"a": {"interval": 1}, "b": {"interval": 20}}

    def test_job_entry_fields_spread_not_replaced(self):
        # A one-line fleet toggle must overlay the system entry's fields, not
        # replace the entry wholesale — `{enroll: true}` opts in WITHOUT
        # re-declaring script/schedule (else the composed unit loses its
        # ExecStart).
        system = {
            "jobs": {"w": {"enroll": False, "script": "w.sh", "schedule": "daily"}}
        }
        defaults = {"jobs": {"w": {"enroll": True}}}
        merged = _merge_system_into_defaults(system, defaults)
        assert merged["jobs"]["w"] == {
            "enroll": True,
            "script": "w.sh",
            "schedule": "daily",
        }

    def test_load_fleet_threads_system_jobs_into_merged(self, tmp_path):
        root = tmp_path / "claudlobby"
        _fleet, merged = load_fleet(_write_fleet(root, _NO_OVERRIDE_FLEET))
        assert set(merged["jobs"]) == _ALL_JOB_NAMES

    def test_partial_override_preserves_siblings(self, tmp_path):
        root = tmp_path / "claudlobby"
        _fleet, merged = load_fleet(
            _write_fleet(
                root,
                """
fleet:
  name: test-fleet
  service_prefix: com.test
  defaults:
    jobs:
      keepalive:
        script: "$CLAUDLOBBY_ROOT/lib/keepalive-all.sh"
        interval: 30
        type: oneshot
  bots:
    worker:
      expertise: [eng]
""",
            )
        )
        # keepalive overridden, all five system siblings preserved
        assert set(merged["jobs"]) == _ALL_JOB_NAMES
        assert merged["jobs"]["keepalive"]["interval"] == 30

    def test_timers_off_zeroes_jobs(self, tmp_path):
        root = tmp_path / "claudlobby"
        _fleet, merged = load_fleet(
            _write_fleet(
                root,
                """
fleet:
  name: test-fleet
  service_prefix: com.test
  system_defaults:
    timers: false
  bots:
    worker:
      expertise: [eng]
""",
            )
        )
        assert merged.get("jobs", {}) == {}


class TestJobsComposition:
    def _compose(self, tmp_path, fleet_yaml):
        from claudlobby.composer import compose_fleet_timers

        root = tmp_path / "claudlobby"
        fleet, merged = load_fleet(_write_fleet(root, fleet_yaml))
        paths = Paths(root=root, fleet_dir=root)
        return compose_fleet_timers(fleet, paths, merged)

    def test_byte_identical_units_no_override(self, tmp_path):
        # A fleet with NO jobs override composes exactly the system default job
        # units — same names + content as the pre-Phase-1 fleet_timers.
        timers_dir = self._compose(tmp_path, _NO_OVERRIDE_FLEET)
        for name in _ALL_JOB_NAMES:
            assert (timers_dir / f"com.test.{name}.service").is_file()
            assert (timers_dir / f"com.test.{name}.timer").is_file()
            assert (timers_dir / f"com.test.{name}.plist").is_file()
        # keepalive static interval unchanged
        assert "OnBootSec=60" in (timers_dir / "com.test.keepalive.timer").read_text()
        # interval_from resolves end-to-end (observability.pulse_interval = 300)
        assert (
            "OnBootSec=300" in (timers_dir / "com.test.fleet-pulse.timer").read_text()
        )
        # weekly calendar expression unchanged
        assert (
            "OnCalendar=Sun *-*-* 05:00:00"
            in (timers_dir / "com.test.weekly-worker-restart.timer").read_text()
        )

    def test_partial_override_still_emits_siblings(self, tmp_path):
        timers_dir = self._compose(
            tmp_path,
            """
fleet:
  name: test-fleet
  service_prefix: com.test
  defaults:
    jobs:
      keepalive:
        script: "$CLAUDLOBBY_ROOT/lib/keepalive-all.sh"
        interval: 30
        type: oneshot
  bots:
    worker:
      expertise: [eng]
""",
        )
        assert "OnBootSec=30" in (timers_dir / "com.test.keepalive.timer").read_text()
        assert (timers_dir / "com.test.fleet-pulse.service").is_file()
        assert (timers_dir / "com.test.reload-fleet.service").is_file()

    def test_generate_and_diff_compose_identical_units(self, tmp_path):
        # generate (default output_dir) and diff (output_dir redirect) must
        # compose byte-identical job units from the same merged['jobs'] source
        # — guards a bb02d56-class generate-vs-diff divergence.
        import tempfile

        from claudlobby.composer import compose_fleet_timers

        root = tmp_path / "claudlobby"
        fleet, merged = load_fleet(_write_fleet(root, _NO_OVERRIDE_FLEET))
        paths = Paths(root=root, fleet_dir=root)

        gen_dir = compose_fleet_timers(fleet, paths, merged)
        gen = {f.name: f.read_text() for f in sorted(gen_dir.iterdir())}

        with tempfile.TemporaryDirectory() as td:
            diff_dir = compose_fleet_timers(fleet, paths, merged, output_dir=Path(td))
            dd = {f.name: f.read_text() for f in sorted(diff_dir.iterdir())}

        assert gen == dd


# ---------------------------------------------------------------------------
# Host timer generation (system.yaml host.jobs)
# ---------------------------------------------------------------------------


class TestComposeHostTimers:
    def _paths(self, tmp_path):
        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        return Paths(root=root, fleet_dir=root)

    @pytest.mark.parametrize(
        "job,script,schedule",
        [
            ("claude-update", "update-claude-code.sh", "*-*-* 04:00:00"),
            ("notify-behind", "notify-behind.sh", "*-*-* 08:00:00"),
            ("disk-monitor", "disk-monitor.sh", "*-*-* 05:00:00"),
            ("fleet-memory-check", "fleet-memory-check.sh", "*-*-* 05:30:00"),
            (
                "orphan-browser-reaper",
                "orphan-browser-reaper.sh",
                "*-*-* 05:45:00",
            ),
        ],
    )
    def test_emits_host_job_units(self, tmp_path, job, script, schedule):
        from claudlobby.composer import compose_host_timers

        paths = self._paths(tmp_path)
        timers_dir = compose_host_timers(paths)
        assert timers_dir == paths.root / "runtime" / "_host" / "timers"

        svc = timers_dir / f"claudlobby-{job}.service"
        timer = timers_dir / f"claudlobby-{job}.timer"
        plist = timers_dir / f"claudlobby-{job}.plist"
        assert svc.is_file()
        assert timer.is_file()
        assert plist.is_file()

        svc_text = svc.read_text()
        # Host scope: no fleet arg on ExecStart, no CLAUDLOBBY_FLEET env.
        assert svc_text.rstrip().endswith(f"lib/{script}")
        assert "CLAUDLOBBY_FLEET" not in svc_text

        timer_text = timer.read_text()
        assert f"OnCalendar={schedule}" in timer_text
        assert "Persistent=true" in timer_text
        assert "RandomizedDelaySec=600" in timer_text

        plist_text = plist.read_text()
        assert f"<string>claudlobby-{job}</string>" in plist_text
        assert "CLAUDLOBBY_FLEET" not in plist_text
        # systemd-only knobs never leak into the plist.
        assert "Persistent" not in plist_text
        assert "RandomizedDelay" not in plist_text

    def test_no_jobs_no_dir(self, tmp_path, monkeypatch):
        import claudlobby.composer as composer_mod

        monkeypatch.setattr(composer_mod, "load_host_jobs", lambda: {})
        paths = self._paths(tmp_path)
        timers_dir = composer_mod.compose_host_timers(paths)
        assert not timers_dir.exists()

    def test_output_dir_redirect_matches_default(self, tmp_path):
        # host-timers must stay single-sourced: a redirected compose (the
        # diff-style path) emits byte-identical units to the default one.
        from claudlobby.composer import compose_host_timers

        paths = self._paths(tmp_path)
        default_dir = compose_host_timers(paths)
        redirect_dir = compose_host_timers(paths, output_dir=tmp_path / "redirect")
        default = {f.name: f.read_text() for f in sorted(default_dir.iterdir())}
        redirect = {f.name: f.read_text() for f in sorted(redirect_dir.iterdir())}
        assert default == redirect

    def test_data_sweep_execstart_args_then_fleet(self, tmp_path):
        # script-with-args composes as ExecStart=<script> --purge <fleet> —
        # the arg order the script's parser contract expects (flags first,
        # positional fleet name last, appended by the composer).
        from claudlobby.composer import compose_fleet_timers

        paths = self._paths(tmp_path)
        fleet = FleetConfig(name="test-fleet", service_prefix="com.test")
        timers_dir = compose_fleet_timers(fleet, paths, _default_merged())
        svc_text = (timers_dir / "com.test.data-sweep.service").read_text()
        assert "data-sweep.sh --purge test-fleet" in svc_text

    def test_fleet_units_knobs_match_declarations(self, tmp_path):
        # Guard: persistent/randomized_delay appear in a fleet unit IFF the
        # job declares them — undeclared jobs must not silently grow host
        # knobs, and declared ones (creds-check jitter) must not lose them.
        from claudlobby.composer import compose_fleet_timers

        paths = self._paths(tmp_path)
        fleet = FleetConfig(name="test-fleet", service_prefix="com.test")
        merged = _default_merged()
        timers_dir = compose_fleet_timers(fleet, paths, merged)
        units = list(timers_dir.glob("*.timer"))
        assert units
        for unit in units:
            job = unit.name.removeprefix("com.test.").removesuffix(".timer")
            cfg = merged["jobs"].get(job, {})
            text = unit.read_text()
            assert ("Persistent=" in text) == bool(cfg.get("persistent")), job
            assert ("RandomizedDelaySec=" in text) == bool(
                cfg.get("randomized_delay")
            ), job


# ---------------------------------------------------------------------------
# DORMANT manifest (F4: composed-but-dormant opt-in jobs)
# ---------------------------------------------------------------------------


class TestDormantManifest:
    def _compose(self, tmp_path, merged):
        from claudlobby.composer import compose_fleet_timers

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        fleet = FleetConfig(name="test-fleet", service_prefix="com.test")
        return compose_fleet_timers(fleet, paths, merged)

    def test_manifest_lists_enroll_false_jobs(self, tmp_path):
        timers_dir = self._compose(tmp_path, _default_merged())
        manifest = (timers_dir / "DORMANT").read_text()
        entries = [
            line for line in manifest.splitlines() if line and not line.startswith("#")
        ]
        assert entries == ["com.test.weekly-worker-restart"]
        # Composed-but-dormant: the units are still emitted (F4 lock).
        assert (timers_dir / "com.test.weekly-worker-restart.timer").is_file()
        assert (timers_dir / "com.test.weekly-worker-restart.service").is_file()

    def test_fleet_enroll_true_clears_dormant_entry(self, tmp_path):
        merged = _default_merged()
        jobs = dict(merged["jobs"])
        jobs["weekly-worker-restart"] = {
            **jobs["weekly-worker-restart"],
            "enroll": True,
        }
        merged = {**merged, "jobs": jobs}
        timers_dir = self._compose(tmp_path, merged)
        manifest = (timers_dir / "DORMANT").read_text()
        entries = [
            line for line in manifest.splitlines() if line and not line.startswith("#")
        ]
        assert entries == []
        # Still composed, of course.
        assert (timers_dir / "com.test.weekly-worker-restart.timer").is_file()
