"""Tests for the P5 workstream registry config surface: fleet.workstreams
parsing (config.py), validation (validator.py), and WORKSTREAM_* composition
into bot.conf (composer.py)."""

from __future__ import annotations

from pathlib import Path

from claudlobby.composer import compose_bot_conf
from claudlobby.config import (
    FleetConfig,
    WorkstreamsConfig,
    _coerce_workstreams,
    load_fleet,
)
from claudlobby.paths import Paths
from claudlobby.validator import ValidationReport, _validate_workstreams


def _fleet_yaml(root: Path, workstreams_block: str = "") -> Path:
    """Write a minimal fleet.yaml, optionally with a 2-space-indented
    `workstreams:` block inserted under `fleet:`. Built as an explicit string
    to avoid textwrap.dedent recomputing the common prefix over the block."""
    (root / "library" / "expertise").mkdir(parents=True, exist_ok=True)
    (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    content = (
        "fleet:\n"
        "  name: test-fleet\n"
        "  service_prefix: com.test\n"
        f"{workstreams_block}"
        "  bots:\n"
        "    worker:\n"
        "      expertise: [eng]\n"
        "      telegram:\n"
        "        handle: w_bot\n"
    )
    (root / "fleet.yaml").write_text(content)
    return root / "fleet.yaml"


class TestCoerce:
    def test_defaults_when_absent(self):
        ws = _coerce_workstreams(None)
        assert (ws.max_active, ws.lease_days) == (12, 14)

    def test_parses_values(self):
        ws = _coerce_workstreams({"max_active": 5, "lease_days": 7})
        assert (ws.max_active, ws.lease_days) == (5, 7)

    def test_bad_values_fall_back_but_raw_preserved(self):
        ws = _coerce_workstreams({"max_active": "five", "lease_days": 0})
        assert (ws.max_active, ws.lease_days) == (12, 14)  # tolerant load
        assert ws.raw["max_active"] == "five"  # kept so the validator can flag it

    def test_bool_is_not_a_valid_int(self):
        # bool is an int subclass in Python — must be rejected explicitly.
        assert _coerce_workstreams({"max_active": True}).max_active == 12


class TestLoadFleet:
    def test_workstreams_block_parsed(self, tmp_path: Path):
        yaml = _fleet_yaml(
            tmp_path / "cl",
            "  workstreams:\n    max_active: 6\n    lease_days: 21\n",
        )
        fleet, _ = load_fleet(yaml)
        assert fleet.workstreams.max_active == 6
        assert fleet.workstreams.lease_days == 21

    def test_absent_block_uses_defaults(self, tmp_path: Path):
        fleet, _ = load_fleet(_fleet_yaml(tmp_path / "cl"))
        assert fleet.workstreams.max_active == 12
        assert fleet.workstreams.lease_days == 14


class TestValidator:
    def _fleet(self, raw: dict) -> FleetConfig:
        return FleetConfig(
            name="f", service_prefix="p", workstreams=WorkstreamsConfig(raw=raw)
        )

    def test_bad_max_active_errors(self):
        report = ValidationReport()
        _validate_workstreams(self._fleet({"max_active": 0}), report)
        assert any("max_active" in e for e in report.errors)

    def test_non_int_lease_days_errors(self):
        report = ValidationReport()
        _validate_workstreams(self._fleet({"lease_days": "two weeks"}), report)
        assert any("lease_days" in e for e in report.errors)

    def test_unknown_key_warns(self):
        report = ValidationReport()
        _validate_workstreams(self._fleet({"max_atcive": 5}), report)
        assert any("unknown key" in w for w in report.warnings)

    def test_valid_block_is_clean(self):
        report = ValidationReport()
        _validate_workstreams(
            self._fleet({"max_active": 5, "lease_days": 7}), report
        )
        assert not report.errors

    def test_empty_block_is_noop(self):
        report = ValidationReport()
        _validate_workstreams(self._fleet({}), report)
        assert not report.errors and not report.warnings


class TestComposerEmit:
    def _paths(self, root: Path) -> Paths:
        return Paths(root=root, fleet_dir=root)

    def test_bot_conf_carries_configured_workstream_env(self, tmp_path: Path):
        root = tmp_path / "cl"
        yaml = _fleet_yaml(
            root, "  workstreams:\n    max_active: 9\n    lease_days: 30\n"
        )
        fleet, _ = load_fleet(yaml)
        conf = compose_bot_conf(fleet.bots["worker"], fleet, self._paths(root))
        assert "export WORKSTREAM_MAX_ACTIVE=9" in conf
        assert "export WORKSTREAM_LEASE_DAYS=30" in conf

    def test_defaults_emitted_when_block_absent(self, tmp_path: Path):
        root = tmp_path / "cl"
        fleet, _ = load_fleet(_fleet_yaml(root))
        conf = compose_bot_conf(fleet.bots["worker"], fleet, self._paths(root))
        assert "export WORKSTREAM_MAX_ACTIVE=12" in conf
        assert "export WORKSTREAM_LEASE_DAYS=14" in conf
