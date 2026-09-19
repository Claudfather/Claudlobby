"""Tests for the system defaults tier.

Covers:
- Three-layer merge (system < fleet-defaults < bot-stanza)
- Hook dedup by (command, matcher)
- Opt-out via system_defaults: false and per-category
- Timer generation
- Backwards compat: existing fleet.yaml with manual hooks/observability
"""

from __future__ import annotations

import json
import logging
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
from tests.conftest import install_real_template


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
        system = {"observability": {"pulse_interval": 300, "activity_stuck_threshold": 1800}}
        defaults = {"observability": {"pulse_interval": 600}}
        result = _merge_system_into_defaults(system, defaults)
        assert result["observability"]["pulse_interval"] == 600
        assert result["observability"]["activity_stuck_threshold"] == 1800

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
        assert cfg.guardrails is True

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
        assert cfg.guardrails is True

    def test_per_category_disable_guardrails(self):
        from claudlobby.config import _coerce_system_defaults

        cfg = _coerce_system_defaults({"guardrails": False})
        assert cfg.enabled is True
        assert cfg.guardrails is False
        assert cfg.hooks is True

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
        assert bot.observability.activity_stuck_threshold == 1800
        # the tier's default is a DAY since chunk M-A (#1481: a deadline by default
        # so the watchdog and the re-check have a clock); 1800 was the pre-M value
        assert bot.observability.dispatch_deadline == 86400

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
        assert merged["observability"]["activity_stuck_threshold"] == 1800

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


class TestDefaultGuardrails:
    """DEFAULT_GUARDRAILS compose onto every bot unless explicitly opted out."""

    def test_lands_on_every_bot_with_no_config(self, tmp_path):
        fleet, _ = load_fleet(
            _write_fleet(
                tmp_path / "claudlobby",
                """\
                fleet:
                  name: test-fleet
                  service_prefix: com.test
                  bots:
                    worker:
                      expertise: [eng]
                    second:
                      expertise: [eng]
                """,
            )
        )
        for name in ("worker", "second"):
            assert "claudlobby-dev-in-projects" in fleet.bots[name].guardrails

    def test_per_category_opt_out(self, tmp_path):
        fleet, _ = load_fleet(
            _write_fleet(
                tmp_path / "claudlobby",
                """\
                fleet:
                  name: test-fleet
                  service_prefix: com.test
                  system_defaults:
                    guardrails: false
                  bots:
                    worker:
                      expertise: [eng]
                """,
            )
        )
        assert "claudlobby-dev-in-projects" not in fleet.bots["worker"].guardrails

    def test_kill_switch_opts_out(self, tmp_path):
        fleet, _ = load_fleet(
            _write_fleet(
                tmp_path / "claudlobby",
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
        )
        assert "claudlobby-dev-in-projects" not in fleet.bots["worker"].guardrails

    def test_fleet_declaring_defaults_guardrails_does_not_drop_the_default(
        self, tmp_path
    ):
        """A fleet listing its own guardrails must not silently lose the default.

        Not hypothetical: fleet.yaml.seed ships a `defaults.guardrails` list, so
        EVERY newly seeded fleet declares one.  _merge_system_into_defaults
        replaces a fleet-set key rather than combining it, so applying the
        default through that merge would drop it for exactly the new fleets this
        change exists to protect — while every other test in the suite still
        passed.
        """
        fleet, _ = load_fleet(
            _write_fleet(
                tmp_path / "claudlobby",
                """\
                fleet:
                  name: test-fleet
                  service_prefix: com.test
                  defaults:
                    guardrails: [no-push-main, no-destructive-git, pii-protection]
                  bots:
                    worker:
                      expertise: [eng]
                """,
            )
        )
        guardrails = fleet.bots["worker"].guardrails
        assert "claudlobby-dev-in-projects" in guardrails
        # The fleet's own declarations survive alongside it.
        assert "no-push-main" in guardrails
        assert "pii-protection" in guardrails

    def test_not_duplicated_when_already_declared(self, tmp_path):
        fleet, _ = load_fleet(
            _write_fleet(
                tmp_path / "claudlobby",
                """\
                fleet:
                  name: test-fleet
                  service_prefix: com.test
                  bots:
                    worker:
                      expertise: [eng]
                      guardrails: [claudlobby-dev-in-projects]
                """,
            )
        )
        guardrails = fleet.bots["worker"].guardrails
        assert guardrails.count("claudlobby-dev-in-projects") == 1


class TestSystemDefaultsFile:
    def test_loads_from_package(self):
        raw = _load_system_defaults()
        assert "host" in raw
        d = raw["defaults"]
        assert "hooks" in d
        # the #1402 telegram-carrier hooks ship in the package defaults —
        # the composed-render probe was session-only, this pins the entries
        flat = json.dumps(d["hooks"])
        assert "plane-telegram-out.sh" in flat
        assert "plane-telegram-in.sh" in flat
        assert "mcp__plugin_telegram_telegram__reply" in flat
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
    "task-recheck",
    "manager-checkin",
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
        # units — same names + content as the pre-Phase-1 fleet_timers — WITH
        # ONE EXCEPTION: manager-checkin is leaf-manager-gated (PR4 task 3,
        # #1569), and _NO_OVERRIDE_FLEET is a single worker with no manager
        # at all, so it composes no unit for that job. See
        # TestManagerCheckinJobGate for the gate's own dedicated coverage.
        timers_dir = self._compose(tmp_path, _NO_OVERRIDE_FLEET)
        for name in _ALL_JOB_NAMES - {"manager-checkin"}:
            assert (timers_dir / f"com.test.{name}.service").is_file()
            assert (timers_dir / f"com.test.{name}.timer").is_file()
            assert (timers_dir / f"com.test.{name}.plist").is_file()
        for ext in ("service", "timer", "plist"):
            assert not (timers_dir / f"com.test.manager-checkin.{ext}").is_file()
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
        from claudlobby.config import BotConfig, TeamConfig

        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "lib").mkdir()
        paths = Paths(root=root, fleet_dir=root)
        # manager-checkin (PR4 task 3, #1569) is leaf-manager-gated: a fleet
        # with no leaf manager composes no unit for it at all, which would
        # make this class's dormancy-manifest assertions about that job
        # vacuous. lead/worker gives the fleet exactly one leaf manager so
        # manager-checkin keeps demonstrating composed-but-dormant, same as
        # weekly-worker-restart beside it — this class is about the DORMANT
        # manifest mechanism, not leaf-manager topology, so the shape is
        # chosen to keep that mechanism exercised.
        fleet = FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            bots={
                "lead": BotConfig(bot_id="lead", name="lead", expertise=["orchestration"]),
                "worker": BotConfig(bot_id="worker", name="worker", expertise=["eng"]),
            },
            teams={"eng": TeamConfig(name="eng", manager="lead", workers=["worker"])},
        )
        return compose_fleet_timers(fleet, paths, merged)

    def test_manifest_lists_enroll_false_jobs(self, tmp_path):
        timers_dir = self._compose(tmp_path, _default_merged())
        manifest = (timers_dir / "DORMANT").read_text()
        entries = [
            line for line in manifest.splitlines() if line and not line.startswith("#")
        ]
        # task-recheck left this list in chunk N — the reaction the target
        # workflow is for ships enrolled. weekly-worker-restart stays: it
        # bounces live worker sessions, and long-running context is the thing
        # this system exists to keep. manager-checkin (PR 2 chunk 2) joins it
        # for its own reason — model spend per beat, not doubt about the
        # behaviour. _write_timers_manifest sorts the manifest, so the order
        # here is alphabetical, not declaration order.
        assert entries == ["com.test.manager-checkin",
                           "com.test.weekly-worker-restart"]
        # Composed-but-dormant: the units are still emitted (F4 lock).
        assert (timers_dir / "com.test.weekly-worker-restart.timer").is_file()
        assert (timers_dir / "com.test.weekly-worker-restart.service").is_file()
        assert (timers_dir / "com.test.manager-checkin.timer").is_file()
        assert (timers_dir / "com.test.manager-checkin.service").is_file()

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
        # weekly-worker-restart is gone from the list; manager-checkin is
        # untouched here and stays at its own default (enroll: false), so
        # the list is not empty — only the one job this test armed left it.
        assert entries == ["com.test.manager-checkin"]
        # Still composed, of course.
        assert (timers_dir / "com.test.weekly-worker-restart.timer").is_file()


# ---------------------------------------------------------------------------
# manager-checkin's compose-time job gate (PR4 task 3, #1569, controller
# correction 5): the fleet-job emitter skips `manager-checkin` entirely for a
# fleet with NO leaf manager (`fleet.leaf_manager_bots()` empty) — not even a
# dormant unit, because such a fleet can never satisfy the job's own
# precondition (there is no manager for it to ever inject into). A fleet
# whose leaf manager disappears between generates must not leave a STALE
# unit on disk that a later `enroll: true` or a naive setup-fleet run would
# enroll for real.
# ---------------------------------------------------------------------------


class TestManagerCheckinJobGate:
    _NO_LEAF_MANAGER = """
fleet:
  name: test-fleet
  service_prefix: com.test
  bots:
    worker:
      expertise: [eng]
"""

    _WITH_LEAF_MANAGER = """
fleet:
  name: test-fleet
  service_prefix: com.test
  teams:
    eng:
      manager: lead
      workers: [worker]
  bots:
    lead:
      expertise: [eng]
    worker:
      expertise: [eng]
"""

    # The "worker removed" fleet, same bot id (`lead`), same fleet name +
    # service_prefix, no team any more — the exact "remove the worker" shape
    # correction 5 names, composed into the SAME output dir as
    # _WITH_LEAF_MANAGER above.
    _LEAF_MANAGER_WORKER_REMOVED = """
fleet:
  name: test-fleet
  service_prefix: com.test
  bots:
    lead:
      expertise: [eng]
"""

    # Same "worker removed" shape as above, plus system_defaults.timers:
    # false — so compose_fleet_timers's early return (`not emit_defaults and
    # not sweep_on and not briefing_on`) fires too. No sweep, no briefing
    # bot declared either: every OTHER reason to touch the timers dir is
    # off, which is exactly what item 1g needs to isolate the prune block.
    _NO_LEAF_MANAGER_TIMERS_OFF = """
fleet:
  name: test-fleet
  service_prefix: com.test
  system_defaults:
    timers: false
  bots:
    lead:
      expertise: [eng]
"""

    def _compose(self, tmp_path, fleet_yaml, *, root=None):
        from claudlobby.composer import compose_fleet_timers

        root = root if root is not None else tmp_path / "claudlobby"
        fleet, merged = load_fleet(_write_fleet(root, fleet_yaml))
        paths = Paths(root=root, fleet_dir=root)
        return compose_fleet_timers(fleet, paths, merged)

    def test_a_fleet_with_no_leaf_manager_composes_no_manager_checkin_unit(
        self, tmp_path
    ):
        timers_dir = self._compose(tmp_path, self._NO_LEAF_MANAGER)
        for ext in ("service", "timer", "plist"):
            assert not (timers_dir / f"com.test.manager-checkin.{ext}").is_file()
        dormant = (timers_dir / "DORMANT").read_text().splitlines()
        assert "com.test.manager-checkin" not in dormant
        # every OTHER default job composes exactly as it always has —
        # the gate is scoped to manager-checkin alone.
        assert (timers_dir / "com.test.task-recheck.timer").is_file()
        assert (timers_dir / "com.test.keepalive.timer").is_file()
        assert "com.test.weekly-worker-restart" in dormant

    def test_a_fleet_with_a_leaf_manager_composes_it_dormant(self, tmp_path):
        """Positive control for the test above: the SAME job, the ONLY
        difference being a fleet shape that has a leaf manager to receive
        it — composes exactly as it did before this task (dormant, per
        system.yaml's own enroll: false)."""
        timers_dir = self._compose(tmp_path, self._WITH_LEAF_MANAGER)
        for ext in ("service", "timer", "plist"):
            assert (timers_dir / f"com.test.manager-checkin.{ext}").is_file()
        assert (
            "com.test.manager-checkin"
            in (timers_dir / "DORMANT").read_text().splitlines()
        )

    def test_losing_the_last_leaf_manager_prunes_the_stale_unit(self, tmp_path):
        root = tmp_path / "claudlobby"
        timers_dir = self._compose(tmp_path, self._WITH_LEAF_MANAGER, root=root)
        for ext in ("service", "timer", "plist"):
            assert (timers_dir / f"com.test.manager-checkin.{ext}").is_file()
        assert (
            "com.test.manager-checkin"
            in (timers_dir / "DORMANT").read_text().splitlines()
        )
        # Every sibling unit's CONTENT before the re-compose, keyed by name —
        # the gate must not so much as rewrite a byte of another job's unit.
        siblings_before = {
            p.name: p.read_text()
            for p in timers_dir.iterdir()
            if p.name != "DORMANT" and not p.name.startswith("com.test.manager-checkin.")
        }
        assert siblings_before  # sanity: there ARE siblings to compare

        # Re-compose the SAME output dir after removing the worker (lead
        # keeps its bot id; only its team is gone) — no leaf manager remains.
        self._compose(tmp_path, self._LEAF_MANAGER_WORKER_REMOVED, root=root)

        for ext in ("service", "timer", "plist"):
            assert not (timers_dir / f"com.test.manager-checkin.{ext}").is_file()
        assert (
            "com.test.manager-checkin"
            not in (timers_dir / "DORMANT").read_text().splitlines()
        )
        siblings_after = {
            p.name: p.read_text()
            for p in timers_dir.iterdir()
            if p.name != "DORMANT" and not p.name.startswith("com.test.manager-checkin.")
        }
        assert siblings_after == siblings_before

    def test_losing_the_last_leaf_manager_logs_each_pruned_file(
        self, tmp_path, caplog
    ):
        """Fix round 1, item 1: the prune speaks. `_prune_host_units` (the
        composer's other prune) logs every unlink; this one silently
        discarded its own return value. One INFO line per removed file,
        naming the file and WHY (no leaf manager left to receive it)."""
        root = tmp_path / "claudlobby"
        self._compose(tmp_path, self._WITH_LEAF_MANAGER, root=root)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="claudlobby.composer"):
            self._compose(tmp_path, self._LEAF_MANAGER_WORKER_REMOVED, root=root)
        pruned = [
            r.message for r in caplog.records if "manager-checkin" in r.message
        ]
        assert len(pruned) == 3, pruned  # .service, .timer, .plist
        for ext in ("service", "timer", "plist"):
            assert any(
                f"com.test.manager-checkin.{ext}" in m for m in pruned
            ), pruned
        assert all("no leaf manager" in m for m in pruned), pruned

    def test_a_fleet_that_never_had_the_unit_prunes_silently(
        self, tmp_path, caplog
    ):
        """The other half: nothing to prune, nothing logged — nothing exists
        for this fleet to have ever composed manager-checkin's units."""
        with caplog.at_level(logging.INFO, logger="claudlobby.composer"):
            self._compose(tmp_path, self._NO_LEAF_MANAGER)
        pruned = [
            r.message for r in caplog.records if "manager-checkin" in r.message
        ]
        assert not pruned, pruned

    def test_the_prune_runs_even_when_every_other_reason_to_compose_is_off(
        self, tmp_path
    ):
        """Fix wave B item 1g — the named candidate mutant. The
        leaf-manager-gated prune (composer.py, `compose_fleet_timers`) sits
        BEFORE `if not emit_defaults and not sweep_on and not briefing_on:
        ... return timers_dir` — deliberately, so a fleet that loses its
        last leaf manager still gets its stale manager-checkin units
        removed even when every OTHER reason to touch the timers dir is
        off (system_defaults.timers: false here, plus no sweep and no
        briefing bot in either shape). Moving the prune block below that
        early return makes this fail: the return would fire first and the
        stale units would survive."""
        root = tmp_path / "claudlobby"
        timers_dir = self._compose(tmp_path, self._WITH_LEAF_MANAGER, root=root)
        for ext in ("service", "timer", "plist"):
            assert (timers_dir / f"com.test.manager-checkin.{ext}").is_file()

        self._compose(tmp_path, self._NO_LEAF_MANAGER_TIMERS_OFF, root=root)

        for ext in ("service", "timer", "plist"):
            assert not (timers_dir / f"com.test.manager-checkin.{ext}").is_file()


# ---------------------------------------------------------------------------
# The opt-out surface (PR4 task 3, #1569, controller correction 7):
# `fleet.system_defaults.protocols: false` must remove the leaf-manager
# default AND — through Task 1's requires: union, which is fed by the
# EFFECTIVE protocol set — the skill it brings and that skill's grants.
# Asserted at the COMPOSED ARTIFACT (CLAUDE.md, the skill symlink,
# settings.local.json), never only at the resolver: a resolver-level pass
# proves the union computes the right list, not that the composer actually
# withheld the file/symlink/grant it names.
# ---------------------------------------------------------------------------


def _write_checkin_library_for_opt_out_tests(fleet_dir: Path) -> None:
    """Copy the REAL checkin protocol + skill into the fixture's library —
    tests/test_checkin_library.py's precedent, repeated here rather than
    imported: this file's own convention is a local helper per file, not a
    cross-file shared one (test_leaf_manager_role.py, test_requires_linking.py
    each keep their own copy too)."""
    import shutil

    repo = Path(__file__).resolve().parent.parent
    shutil.copy(
        repo / "library" / "protocols" / "checkin.md",
        fleet_dir / "library" / "protocols" / "checkin.md",
    )
    dst = fleet_dir / "library" / "skills" / "checkin"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(repo / "library" / "skills" / "checkin", dst)


def _add_coordinator_for_opt_out_tests(fleet_dir: Path) -> None:
    """Add `coord`, managing a team of one (lead) — lead stays a leaf manager
    (it still manages eng/worker-1); coord is a coordinator, not a leaf."""
    text = (fleet_dir / "fleet.yaml").read_text()
    text = text.replace(
        "  teams:\n    eng:\n      manager: lead\n      workers: [worker-1]\n",
        "  teams:\n    eng:\n      manager: lead\n      workers: [worker-1]\n"
        "    top:\n      manager: coord\n      workers: [lead]\n",
    )
    text = text.replace(
        "  bots:\n    lead:\n",
        "  bots:\n    coord:\n      expertise: [orchestration]\n    lead:\n",
    )
    (fleet_dir / "fleet.yaml").write_text(text)


class TestLeafManagerCheckinOptOut:
    def _checkin_state(self, fleet, paths, bot_id: str) -> dict[str, bool]:
        """Compose *bot_id* for real and read back the three composed
        artifacts a leaf manager's check-in equipment touches."""
        from claudlobby.composer import compose_bot

        compose_bot(fleet.bots[bot_id], fleet, paths, log=lambda m: None)
        bot_dir = paths.bot_runtime(bot_id)
        md = (bot_dir / "CLAUDE.md").read_text()
        link = bot_dir / ".claude" / "skills" / "checkin"
        settings = json.loads(
            (bot_dir / ".claude" / "settings.local.json").read_text()
        )
        allow = settings["permissions"]["allow"]
        return {
            "section": "Silence is the default" in md,
            "symlink": link.is_symlink(),
            "grant": "Skill(checkin)" in allow,
        }

    def test_protocols_false_removes_the_leaf_manager_default(
        self, fleet_dir, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        install_real_template(fleet_dir)
        _write_checkin_library_for_opt_out_tests(fleet_dir)
        text = (fleet_dir / "fleet.yaml").read_text().replace(
            "fleet:\n  name: test-fleet\n",
            "fleet:\n  name: test-fleet\n  system_defaults:\n    protocols: false\n",
            1,
        )
        (fleet_dir / "fleet.yaml").write_text(text)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = Paths(root=fleet_dir, fleet_dir=fleet_dir)
        # the opt-out is the ONLY thing that changed — lead is still a leaf
        # manager; it is simply not EQUIPPED, because the default that would
        # equip it is switched off fleet-wide.
        assert "lead" in fleet.leaf_manager_bots()

        got = self._checkin_state(fleet, paths, "lead")
        assert got == {"section": False, "symlink": False, "grant": False}, got

    def test_the_same_fleet_with_no_opt_out_equips_only_the_leaf_manager(
        self, fleet_dir, monkeypatch
    ):
        """The contrasting positive, in the SAME file as the opt-out it is
        the baseline for: a leaf manager with no opt-out gets the section,
        the symlink AND the grant; a coordinator and a worker in the same
        fleet get none of the three."""
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        install_real_template(fleet_dir)
        _write_checkin_library_for_opt_out_tests(fleet_dir)
        _add_coordinator_for_opt_out_tests(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = Paths(root=fleet_dir, fleet_dir=fleet_dir)
        assert fleet.leaf_manager_bots() == {"lead"}

        lead_got = self._checkin_state(fleet, paths, "lead")
        assert lead_got == {"section": True, "symlink": True, "grant": True}, lead_got

        coord_got = self._checkin_state(fleet, paths, "coord")
        assert coord_got == {
            "section": False, "symlink": False, "grant": False,
        }, coord_got

        worker_got = self._checkin_state(fleet, paths, "worker-1")
        assert worker_got == {
            "section": False, "symlink": False, "grant": False,
        }, worker_got
