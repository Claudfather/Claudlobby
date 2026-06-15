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
        assert "hooks" in raw
        assert "observability" in raw
        assert "fleet_timers" in raw
        assert "fleet-pulse" in raw["fleet_timers"]
        assert "keepalive" in raw["fleet_timers"]
        assert "log-rotation" in raw["fleet_timers"]
        assert "creds-check" in raw["fleet_timers"]


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
        merged = {"observability": {"pulse_interval": 300}}

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

        timers_dir = compose_fleet_timers(fleet, paths, {})

        timer = timers_dir / "com.test.creds-check.timer"
        assert timer.is_file()
        timer_text = timer.read_text()
        assert "OnCalendar=" in timer_text

    def test_all_default_timers_generated(self, tmp_path):
        from claudlobby.composer import compose_fleet_timers

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        fleet = FleetConfig(name="test-fleet", service_prefix="com.test")

        timers_dir = compose_fleet_timers(fleet, paths, {})

        for name in [
            "fleet-pulse",
            "keepalive",
            "log-rotation",
            "creds-check",
            "reload-fleet",
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

        timers_dir = compose_fleet_timers(fleet, paths, {})

        svc = timers_dir / "com.test.reload-fleet.service"
        timer = timers_dir / "com.test.reload-fleet.timer"
        assert svc.is_file()
        assert timer.is_file()
        assert "reload-fleet.sh" in svc.read_text()
        # Daily cadence is a systemd OnCalendar expression, not an interval.
        assert "OnCalendar=" in timer.read_text()


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
