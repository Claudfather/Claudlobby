"""Tests for the opt-in platform self-deploy feature.

Covers config coercion (fleet.auto_deploy block), opt-in timer emission via
compose_fleet_timers (including a fleet that enables ONLY auto_deploy, exercising
the early-return guard), opt-out (no block ⇒ nothing emitted), and validator
schedule sanity. The script behavior itself (health gates, ff-only pull, reload,
rollback, loud failure) is exercised end-to-end by lib/validate-bot-change.sh,
gated under tests/test_validate_harness.py.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from claudlobby.config import AutoDeployConfig, _coerce_auto_deploy, load_fleet
from claudlobby.composer import compose_fleet_timers
from claudlobby.paths import Paths
from claudlobby.validator import validate


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root)


def _write(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fleet.yaml").write_text(dedent(body))
    return root / "fleet.yaml"


# A fleet with an enabled auto_deploy block (system_defaults off so the only
# emitted timer is auto-deploy).
_DEPLOY_FLEET = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      system_defaults: false
      bots:
        astrid:
          expertise: [eng]
      auto_deploy:
        schedule: "*-*-* 02:45:00"
"""

# A fleet with no auto_deploy block at all (opt-out).
_NO_DEPLOY_FLEET = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      system_defaults: false
      bots:
        astrid:
          expertise: [eng]
"""


class TestAutoDeployConfigCoercion:
    def test_coerce_none_when_absent(self):
        assert _coerce_auto_deploy(None) is None
        assert _coerce_auto_deploy({}) is None

    def test_present_block_is_opt_in(self):
        cfg = _coerce_auto_deploy({})  # empty dict ⇒ None (no block)
        assert cfg is None
        cfg = _coerce_auto_deploy({"schedule": "*-*-* 04:00:00"})
        assert isinstance(cfg, AutoDeployConfig)
        assert cfg.enabled is True  # presence of the block = opt-in
        assert cfg.schedule == "*-*-* 04:00:00"

    def test_default_schedule(self):
        cfg = _coerce_auto_deploy({"enabled": True})
        assert cfg is not None and cfg.schedule == "*-*-* 03:15:00"

    def test_enabled_false_respected(self):
        cfg = _coerce_auto_deploy({"enabled": False})
        assert cfg is not None and cfg.enabled is False

    def test_load_fleet_parses_block(self, tmp_path):
        fleet, _md = load_fleet(_write(tmp_path / "f", _DEPLOY_FLEET))
        assert fleet.auto_deploy is not None
        assert fleet.auto_deploy_enabled() is True
        assert fleet.auto_deploy.schedule == "*-*-* 02:45:00"

    def test_load_fleet_none_when_absent(self, tmp_path):
        fleet, _md = load_fleet(_write(tmp_path / "f", _NO_DEPLOY_FLEET))
        assert fleet.auto_deploy is None
        assert fleet.auto_deploy_enabled() is False


class TestAutoDeployTimerEmission:
    def test_timer_emitted_when_enabled(self, tmp_path):
        root = tmp_path / "f"
        fleet, md = load_fleet(_write(root, _DEPLOY_FLEET))
        paths = _make_paths(root)
        compose_fleet_timers(fleet, paths, md)

        timers = paths.runtime_fleet / "timers"
        svc = timers / "com.test.auto-deploy.service"
        timer = timers / "com.test.auto-deploy.timer"
        plist = timers / "com.test.auto-deploy.plist"
        assert svc.is_file() and timer.is_file() and plist.is_file()
        # schedule flows through to OnCalendar
        assert "OnCalendar=*-*-* 02:45:00" in timer.read_text()
        # the unit runs the deploy script
        assert "lib/auto-deploy.sh" in svc.read_text()

    def test_no_units_when_no_block(self, tmp_path):
        root = tmp_path / "f"
        fleet, md = load_fleet(_write(root, _NO_DEPLOY_FLEET))
        paths = _make_paths(root)
        compose_fleet_timers(fleet, paths, md)

        timers = paths.runtime_fleet / "timers"
        assert not (timers / "com.test.auto-deploy.timer").exists()

    def test_only_auto_deploy_still_emits(self, tmp_path):
        """system_defaults off + no sweep + auto_deploy on ⇒ the timer must still
        emit (the early-return guard in compose_fleet_timers honors deploy_on)."""
        root = tmp_path / "f"
        fleet, md = load_fleet(_write(root, _DEPLOY_FLEET))
        paths = _make_paths(root)
        compose_fleet_timers(fleet, paths, md)

        timers = paths.runtime_fleet / "timers"
        # no system_defaults timers, no sweep timer — but auto-deploy present
        assert (timers / "com.test.auto-deploy.timer").is_file()
        assert not (timers / "com.test.fleet-pulse.timer").exists()
        assert not (timers / "com.test.code-audit-sweep.timer").exists()


class TestAutoDeployValidation:
    def test_bad_schedule_warns(self, tmp_path):
        bad = _DEPLOY_FLEET.replace('"*-*-* 02:45:00"', '"nightly"')
        fleet, _md = load_fleet(_write(tmp_path / "f", bad))
        report = validate(fleet, _make_paths(tmp_path / "f"))
        assert any("auto_deploy.schedule" in w for w in report.warnings)

    def test_good_schedule_no_warning(self, tmp_path):
        fleet, _md = load_fleet(_write(tmp_path / "f", _DEPLOY_FLEET))
        report = validate(fleet, _make_paths(tmp_path / "f"))
        assert not any("auto_deploy.schedule" in w for w in report.warnings)
