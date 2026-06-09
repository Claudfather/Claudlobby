"""Tests for claudlobby.status — fleet health dashboard."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from claudlobby.status import (
    BotStatus,
    _health_indicator,
    _heartbeat_display,
    _parse_keepalive_log,
    _state_display,
    collect_fleet_status,
    format_bot_detail,
    format_json,
    format_table,
)
from claudlobby.utilization import load_fleet_state


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture
def mock_paths(tmp_path):
    """Create a minimal Paths-like object."""
    from claudlobby.paths import Paths

    root = tmp_path / "claudlobby"
    root.mkdir()
    (root / "library").mkdir()
    (root / "lib").mkdir()
    fleet_dir = root / "local" / "test-fleet"
    fleet_dir.mkdir(parents=True)
    runtime = fleet_dir / "runtime" / "bots"
    runtime.mkdir(parents=True)
    return Paths(root=root, fleet_dir=fleet_dir)


@pytest.fixture
def mock_fleet():
    """Minimal FleetConfig with two bots."""
    from claudlobby.config import BotConfig, FleetConfig

    return FleetConfig(
        name="test-fleet",
        service_prefix="com.test",
        bots={
            "alice": BotConfig(bot_id="alice", name="alice", expertise=["eng"]),
            "bob": BotConfig(bot_id="bob", name="bob", expertise=["eng"]),
        },
    )


# -- load_fleet_state (now in utilization.py) --------------------------------


class TestLoadFleetState:
    @pytest.fixture(autouse=True)
    def _clear_state_env(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLEET_STATE_PATH", None)
            yield

    def test_missing_file(self, mock_paths):
        result = load_fleet_state(mock_paths)
        assert result == {}

    def test_valid_json(self, mock_paths):
        state_dir = mock_paths.runtime / "state"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "fleet-state.json"
        data = {
            "updated": "2026-01-01T00:00:00Z",
            "bots": {"alice": {"status": "idle"}},
        }
        state_file.write_text(json.dumps(data))
        result = load_fleet_state(mock_paths)
        assert result["bots"]["alice"]["status"] == "idle"

    def test_corrupt_json(self, mock_paths):
        state_dir = mock_paths.runtime / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "fleet-state.json").write_text("{bad json")
        result = load_fleet_state(mock_paths)
        assert result == {}

    def test_env_override(self, mock_paths, tmp_path):
        custom = tmp_path / "custom-state.json"
        data = {"bots": {"alice": {"status": "working"}}}
        custom.write_text(json.dumps(data))
        with patch.dict(os.environ, {"FLEET_STATE_PATH": str(custom)}):
            result = load_fleet_state(mock_paths)
        assert result["bots"]["alice"]["status"] == "working"


# -- _parse_keepalive_log ----------------------------------------------------


class TestParseKeepaliveLog:
    def test_missing_file(self, tmp_path):
        ts, pane = _parse_keepalive_log(tmp_path)
        assert ts is None
        assert pane == ""

    def test_valid_log(self, tmp_path):
        log = tmp_path / "keepalive.log"
        log.write_text("2026-05-16T23:06:47-04:00 IDLE \u2014 at prompt\n")
        ts, pane = _parse_keepalive_log(tmp_path)
        assert ts is not None
        assert ts.year == 2026
        assert pane == "IDLE"

    def test_busy_pane(self, tmp_path):
        log = tmp_path / "keepalive.log"
        log.write_text("2026-05-16T23:06:47-04:00 BUSY \u2014 working\n")
        _, pane = _parse_keepalive_log(tmp_path)
        assert pane == "BUSY"

    def test_empty_file(self, tmp_path):
        (tmp_path / "keepalive.log").write_text("")
        ts, pane = _parse_keepalive_log(tmp_path)
        assert ts is None

    def test_reads_last_line(self, tmp_path):
        log = tmp_path / "keepalive.log"
        log.write_text(
            "2026-05-16T23:00:00-04:00 IDLE \u2014 at prompt\n"
            "2026-05-16T23:01:00-04:00 BUSY \u2014 working\n"
        )
        _, pane = _parse_keepalive_log(tmp_path)
        assert pane == "BUSY"


# -- Health indicator --------------------------------------------------------


# Disable color for predictable assertions
@pytest.fixture(autouse=True)
def no_color():
    with patch("claudlobby.status._COLOR", False):
        yield


class TestHealthIndicator:
    def test_tmux_down(self):
        bs = BotStatus(name="x", tmux_alive=False, service_active=True)
        assert _health_indicator(bs) == "x"

    def test_service_down(self):
        bs = BotStatus(name="x", tmux_alive=True, service_active=False)
        assert _health_indicator(bs) == "x"

    def test_healthy(self):
        bs = BotStatus(name="x", tmux_alive=True, service_active=True, state="idle")
        assert _health_indicator(bs) == "o"

    def test_blocked(self):
        bs = BotStatus(name="x", tmux_alive=True, service_active=True, state="blocked")
        assert _health_indicator(bs) == "!"

    def test_stale_heartbeat(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=15)
        bs = BotStatus(
            name="x",
            tmux_alive=True,
            service_active=True,
            state="idle",
            last_heartbeat=old,
        )
        assert _health_indicator(bs) == "~"


# -- Display helpers ---------------------------------------------------------


class TestStateDisplay:
    def test_idle(self):
        bs = BotStatus(name="x", state="idle")
        assert _state_display(bs) == "idle"

    def test_working(self):
        bs = BotStatus(name="x", state="working")
        assert _state_display(bs) == "working"


class TestHeartbeatDisplay:
    def test_no_heartbeat(self):
        bs = BotStatus(name="x")
        assert _heartbeat_display(bs) == "--"

    def test_recent(self):
        bs = BotStatus(
            name="x",
            last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=30),
        )
        result = _heartbeat_display(bs)
        assert "s ago" in result


# -- Format functions --------------------------------------------------------


class TestFormatTable:
    def test_empty_fleet(self):
        result = format_table([], "test")
        assert "No bots defined" in result

    def test_renders_bot_names(self):
        statuses = [
            BotStatus(name="alice", state="idle", tmux_alive=True, service_active=True),
            BotStatus(
                name="bob", state="working", tmux_alive=True, service_active=True
            ),
        ]
        result = format_table(statuses, "test-fleet")
        assert "alice" in result
        assert "bob" in result
        assert "test-fleet" in result

    def test_summary_line(self):
        statuses = [
            BotStatus(name="a", tmux_alive=True, service_active=True),
            BotStatus(name="b", tmux_alive=False, service_active=True),
        ]
        result = format_table(statuses, "t")
        assert "1/2 up" in result


class TestFormatBotDetail:
    def test_includes_name(self):
        bs = BotStatus(name="alice", state="idle", last_completed="did a thing")
        result = format_bot_detail(bs)
        assert "alice" in result
        assert "did a thing" in result


class TestFormatJson:
    def test_valid_json(self):
        statuses = [
            BotStatus(name="alice", state="idle", tmux_alive=True),
        ]
        result = format_json(statuses, "test")
        parsed = json.loads(result)
        assert parsed["fleet"] == "test"
        assert len(parsed["bots"]) == 1
        assert parsed["bots"][0]["name"] == "alice"
        assert parsed["bots"][0]["state"] == "idle"
        assert parsed["bots"][0]["tmux_alive"] is True

    def test_heartbeat_iso(self):
        ts = datetime(2026, 5, 16, 23, 0, 0, tzinfo=timezone.utc)
        statuses = [BotStatus(name="x", last_heartbeat=ts)]
        result = json.loads(format_json(statuses, "t"))
        assert result["bots"][0]["last_heartbeat"] == "2026-05-16T23:00:00+00:00"


# -- collect_fleet_status (integration-ish, mocked externals) ----------------


class TestCollectFleetStatus:
    def test_basic_collection(self, mock_fleet, mock_paths):
        """Smoke test: collection runs without error and returns all bots."""
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch(
                "claudlobby.status._check_systemd_service",
                return_value=(True, "exited"),
            ),
            patch("claudlobby.utilization.load_fleet_state", return_value={"bots": {}}),
        ):
            results = collect_fleet_status(mock_fleet, mock_paths)
        assert len(results) == 2
        names = {bs.name for bs in results}
        assert names == {"alice", "bob"}
        # alice has tmux
        alice = next(bs for bs in results if bs.name == "alice")
        assert alice.tmux_alive is True
        # bob does not
        bob = next(bs for bs in results if bs.name == "bob")
        assert bob.tmux_alive is False
