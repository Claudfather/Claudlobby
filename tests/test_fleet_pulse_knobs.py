"""Tests for the fleet-pulse escalation knobs (#1120).

Both documented ways to set these were non-functional: the timer unit sources no
`.env` at any tier, and the unit is generated so a hand-edit does not survive
`generate`. Neither failed loudly, so an operator setting an alert-volume knob
observed no change and could not tell "ignored" from "applied, and the condition
is genuinely still firing".

Covers the config block, emission into BOTH scheduler formats (a knob that
composed on one platform only would be the same silent failure on the other),
the scoping to the one job that reads them, and the freshbox rung that makes the
old placement audible.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from claudlobby.composer import compose_fleet_timers
from claudlobby.config import (
    FLEET_PULSE_ENV_KEYS,
    FleetPulseConfig,
    _coerce_fleet_pulse,
    load_fleet,
)
from claudlobby.paths import Paths

_FLEET = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      bots:
        astrid:
          expertise: [eng]
      fleet_pulse:
        escalation_threshold: 3
        escalation_window: 15
        escalation_chat_id: "-1001234567890"
        renotify_after_s: 0
"""

_NO_BLOCK = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      bots:
        astrid:
          expertise: [eng]
"""


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root)


def _write(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fleet.yaml").write_text(dedent(body))
    return root / "fleet.yaml"


def _compose(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "f"
    fleet, md = load_fleet(_write(root, body))
    paths = _make_paths(root)
    compose_fleet_timers(fleet, paths, md)
    return paths.runtime_fleet / "timers"


class TestFleetPulseConfigCoercion:
    def test_none_when_absent(self):
        assert _coerce_fleet_pulse(None) is None
        assert _coerce_fleet_pulse("nope") is None

    def test_load_fleet_parses_block(self, tmp_path):
        fleet, _md = load_fleet(_write(tmp_path / "f", _FLEET))
        fp = fleet.fleet_pulse
        assert fp is not None
        assert fp.escalation_threshold == 3
        assert fp.escalation_window == 15
        assert fp.escalation_chat_id == "-1001234567890"

    def test_load_fleet_none_when_absent(self, tmp_path):
        fleet, _md = load_fleet(_write(tmp_path / "f", _NO_BLOCK))
        assert fleet.fleet_pulse is None

    def test_env_omits_unset_so_defaults_live_in_one_place(self):
        assert FleetPulseConfig().env() == {}
        env = FleetPulseConfig(escalation_threshold=3).env()
        assert env == {"FLEET_PULSE_ESCALATION_THRESHOLD": "3"}

    def test_env_emits_zero(self):
        """0 is a value, not absence — it disables the re-fire and the re-arm
        bound. Truthiness here would silently drop the only way to turn them
        off, which is the same silent-no-op this issue is about."""
        env = FleetPulseConfig(renotify_after_s=0, rearm_window_s=0).env()
        assert env["FLEET_PULSE_RENOTIFY_AFTER_S"] == "0"
        assert env["FLEET_PULSE_REARM_WINDOW_S"] == "0"


class TestEmissionIntoTheUnit:
    def test_systemd_unit_carries_the_knobs(self, tmp_path):
        svc = (_compose(tmp_path, _FLEET) / "com.test.fleet-pulse.service").read_text()
        assert "Environment=FLEET_PULSE_ESCALATION_THRESHOLD=3" in svc
        assert "Environment=FLEET_PULSE_ESCALATION_WINDOW=15" in svc
        assert "Environment=FLEET_PULSE_ESCALATION_CHAT_ID=-1001234567890" in svc
        assert "Environment=FLEET_PULSE_RENOTIFY_AFTER_S=0" in svc

    def test_launchd_plist_carries_the_same_knobs(self, tmp_path):
        """Parity. A knob composed on one platform only is the same silent
        failure for every operator on the other."""
        plist = (_compose(tmp_path, _FLEET) / "com.test.fleet-pulse.plist").read_text()
        for var, value in (
            ("FLEET_PULSE_ESCALATION_THRESHOLD", "3"),
            ("FLEET_PULSE_ESCALATION_WINDOW", "15"),
            ("FLEET_PULSE_ESCALATION_CHAT_ID", "-1001234567890"),
            ("FLEET_PULSE_RENOTIFY_AFTER_S", "0"),
        ):
            assert f"<key>{var}</key>" in plist
            assert f"<string>{value}</string>" in plist

    def test_no_block_emits_no_knobs(self, tmp_path):
        svc = (
            _compose(tmp_path, _NO_BLOCK) / "com.test.fleet-pulse.service"
        ).read_text()
        assert "FLEET_PULSE_" not in svc

    def test_unset_key_is_not_emitted(self, tmp_path):
        """rearm_window_s is absent from the fixture; emitting an empty value
        would override the script default with the empty string."""
        svc = (_compose(tmp_path, _FLEET) / "com.test.fleet-pulse.service").read_text()
        assert "FLEET_PULSE_REARM_WINDOW_S" not in svc

    def test_scoped_to_the_job_that_reads_them(self, tmp_path):
        """Only fleet-pulse consumes these; a wider grant would buy nothing."""
        timers = _compose(tmp_path, _FLEET)
        others = [p for p in timers.glob("*.service") if "fleet-pulse" not in p.name]
        assert others, "expected other fleet timers to exist to make this meaningful"
        for p in others:
            assert "FLEET_PULSE_" not in p.read_text(), p.name

    def test_every_declared_knob_can_reach_the_unit(self, tmp_path):
        """The SSOT check: whatever FLEET_PULSE_ENV_KEYS declares must actually
        compose. A knob added to the mapping and forgotten in the emitter would
        be a new instance of exactly this bug."""
        body = dedent(
            """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                astrid:
                  expertise: [eng]
              fleet_pulse:
                escalation_threshold: 1
                escalation_window: 2
                escalation_chat_id: "-100999"
                renotify_after_s: 3
                rearm_window_s: 4
            """
        )
        svc = (_compose(tmp_path, body) / "com.test.fleet-pulse.service").read_text()
        for var in FLEET_PULSE_ENV_KEYS.values():
            assert f"Environment={var}=" in svc, var


class TestMisplacedEnvIsLoud:
    def _fleet(self, tmp_path):
        fleet, _md = load_fleet(_write(tmp_path / "f", _NO_BLOCK))
        return fleet, _make_paths(tmp_path / "f")

    def test_knob_in_fleet_env_is_a_failure(self, tmp_path):
        from claudlobby.freshbox import FAIL, _fleet_pulse_env_findings

        fleet, paths = self._fleet(tmp_path)
        paths.fleet_config_dir.mkdir(parents=True, exist_ok=True)
        (paths.fleet_config_dir / ".env").write_text(
            "FLEET_PULSE_ESCALATION_THRESHOLD=5\n"
        )
        findings = _fleet_pulse_env_findings(fleet, paths)
        assert findings, "a knob in a .env reaches nothing and must be reported"
        assert all(f.severity == FAIL for f in findings)
        assert "fleet_pulse_env_inert" in {f.kind for f in findings}
        assert "fleet_pulse:" in findings[0].detail

    def test_clean_env_is_silent(self, tmp_path):
        """The rung must be able to say nothing, or it reports on every fleet
        and stops carrying information."""
        from claudlobby.freshbox import _fleet_pulse_env_findings

        fleet, paths = self._fleet(tmp_path)
        paths.fleet_config_dir.mkdir(parents=True, exist_ok=True)
        (paths.fleet_config_dir / ".env").write_text("SOMETHING_ELSE=1\n")
        assert _fleet_pulse_env_findings(fleet, paths) == []

    def test_every_declared_knob_is_watched(self, tmp_path):
        """Same SSOT check from the other side: the guard and the emitter read
        one mapping, so neither can drift into ignoring a knob."""
        from claudlobby.freshbox import _fleet_pulse_env_findings

        fleet, paths = self._fleet(tmp_path)
        paths.fleet_config_dir.mkdir(parents=True, exist_ok=True)
        for var in FLEET_PULSE_ENV_KEYS.values():
            (paths.fleet_config_dir / ".env").write_text(f"{var}=1\n")
            assert _fleet_pulse_env_findings(fleet, paths), var
