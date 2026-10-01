"""Public status routes retain native observations and Plane uncertainty."""

from types import SimpleNamespace

import pytest

from claudlobby.command_result import CommandFailure
from claudlobby.commands import status_read
from claudlobby.status import BotStatus


def _context(tmp_path):
    return SimpleNamespace(paths=SimpleNamespace(root=tmp_path, runtime_bots=tmp_path / "runtime" / "bots"),
                           fleet=SimpleNamespace(name="test", bots={"worker": object()}))


def test_fleet_and_bot_status_keep_native_and_plane_facts_separate(monkeypatch, tmp_path):
    from claudlobby.commands import orientation
    from claudlobby import activation_state, status, switches

    monkeypatch.setattr(orientation, "_context", lambda args: _context(tmp_path))
    monkeypatch.setattr(activation_state, "read_selection", lambda root: None)
    monkeypatch.setattr(switches, "resolve", lambda paths, fleet: [])
    monkeypatch.setattr(status, "collect_fleet_status", lambda fleet, paths: [
        BotStatus(name="worker", service_active=True, service_sub="running",
                  tmux_alive=True, plane_unreachable="Plane unavailable")])

    fleet = status_read.dispatch(SimpleNamespace(public_command="fleet.status"))
    bot = status_read.dispatch(SimpleNamespace(public_command="bot.status", bot_id="worker"))
    for row in (fleet.data["bots"][0], bot.data["bot"]):
        assert row["service_active"] is True and row["tmux_alive"] is True
        assert row["plane_unreachable"] == "Plane unavailable"
        assert row["pane_state"] is None and row["state"] == "unknown"
    assert fleet.release_id is None


def test_uptime_refuses_unavailable_plane_before_reporting_zero(monkeypatch, tmp_path):
    from claudlobby.commands import orientation
    from claudlobby import activation_state, brief, source_state

    monkeypatch.setattr(orientation, "_context", lambda args: _context(tmp_path))
    monkeypatch.setattr(activation_state, "read_selection", lambda root: None)
    monkeypatch.setattr(source_state, "scan_dir", lambda path: (SimpleNamespace(reachable=True), []))
    monkeypatch.setattr(brief, "plane_session", lambda paths, fleet: (None, "database unavailable"))
    with pytest.raises(CommandFailure) as raised:
        status_read.dispatch(SimpleNamespace(public_command="fleet.uptime", bot=None, window="24h"))
    assert raised.value.error.code == "unavailable"
