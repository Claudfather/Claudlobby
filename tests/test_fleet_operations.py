"""Selected fleet sweeps retain serial outcomes and read native state without repair."""

from pathlib import Path
from types import SimpleNamespace
import os
import subprocess

import pytest

from claudlobby import fleet_operations as fleet
from claudlobby import fleet_pulse
from claudlobby.bot_operations import BotLifecycleError, BotLifecycleResult


def _scope(tmp_path, *, origin=None):
    selected_fleet = SimpleNamespace(name="example", manager="manager",
                                     bots={bot: object() for bot in ("manager", "worker-b", "worker-a")})
    package = SimpleNamespace(native=tmp_path / "native")
    destination = SimpleNamespace(fleet=selected_fleet,
                                  paths=SimpleNamespace(root=tmp_path, package=package))
    return destination, origin, {"release_id": "selected-release", "plan_id": "selected-plan"}


def test_fleet_stop_is_serial_and_retains_partial_persistent_outcomes(tmp_path, monkeypatch):
    destination, origin, selected = _scope(tmp_path)
    monkeypatch.setattr(fleet, "_scope", lambda *_a, **_k: (destination, origin, selected))
    calls = []
    def stop(**kwargs):
        assert kwargs["running"] is False and kwargs["restart"] is False
        calls.append(kwargs["bot"])
        if kwargs["bot"] == "worker-b":
            raise BotLifecycleError("private native failure", effect_attempted=True,
                                    release_id="selected-release", target="worker-b-unit")
        return BotLifecycleResult("example", kwargs["bot"], "selected-release",
                                  kwargs["bot"] + "-unit", "stopped", True, "not_running")
    monkeypatch.setattr(fleet, "set_bot_running", stop)
    with pytest.raises(fleet.FleetLifecycleError) as failed:
        fleet.set_fleet_running(root=tmp_path, fleet="example", action="stop")
    assert calls == ["worker-a", "worker-b"]  # manager is last and was not reached
    assert failed.value.bot == "worker-b" and failed.value.effect_attempted
    assert [(row.bot, row.state, row.changed) for row in failed.value.completed] == [
        ("worker-a", "stopped", True)]


def test_manager_origin_requires_workers_before_any_fleet_effect(tmp_path, monkeypatch):
    manager = SimpleNamespace(fleet=SimpleNamespace(name="example"), bot_id="manager")
    destination, _, selected = _scope(tmp_path)
    monkeypatch.setattr(fleet, "_scope", lambda *_a, **_k: (destination, manager, selected))
    calls = []
    def restart(**kwargs):
        calls.append(kwargs["bot"])
        assert kwargs["restart"] and kwargs["running"]
        return BotLifecycleResult("example", kwargs["bot"], "selected-release",
                                  kwargs["bot"] + "-unit", "running", True, "bridge_ready", "attempted")
    monkeypatch.setattr(fleet, "set_bot_running", restart)
    with pytest.raises(fleet.FleetLifecycleError, match="requires --workers"):
        fleet.set_fleet_running(root=tmp_path, fleet="example", action="restart")
    assert calls == []
    done = fleet.set_fleet_running(root=tmp_path, fleet="example", action="restart",
                                   workers_only=True)
    assert calls == ["worker-a", "worker-b"] and len(done.completed) == 2


def test_generated_worker_cannot_mutate_selected_fleet(tmp_path, monkeypatch):
    destination, _, selected = _scope(tmp_path)
    worker = SimpleNamespace(fleet=destination.fleet, bot_id="worker-a")
    monkeypatch.setattr(fleet, "resolve_operation_scope", lambda **_k: (destination, worker))
    monkeypatch.setattr(fleet, "read_selection", lambda _root: selected)
    with pytest.raises(fleet.FleetLifecycleError, match="manager"):
        fleet.set_fleet_running(root=tmp_path, fleet="example", action="start", workers_only=True)


def test_pulse_uses_selected_native_once_and_reports_tick_not_health(tmp_path, monkeypatch):
    from contextlib import contextmanager

    native = tmp_path / "native"
    (native / "fleet-pulse.sh").parent.mkdir()
    (native / "fleet-pulse.sh").touch()
    release = SimpleNamespace(native_path=native, cli_path=tmp_path / "bin/claudlobby",
                              release_id="selected-release")
    destination = SimpleNamespace(fleet=SimpleNamespace(name="example", manager="manager"),
                                  paths=SimpleNamespace(root=tmp_path, lib=native))

    @contextmanager
    def admitted(root, **kwargs):
        assert root == tmp_path
        yield release

    monkeypatch.setattr(fleet_pulse, "mutation_admission", admitted)
    monkeypatch.setattr(fleet_pulse, "native_environment", lambda _paths: {})
    monkeypatch.setattr(fleet_pulse, "resolve_operation_scope",
                        lambda **_kwargs: (destination, None))
    called = []

    def run(command, env):
        called.append((command, env["CLAUDLOBBY_PRIVATE_PULSE_RELEASE"]))
        return "worker DOWN\n", "watchdog dark\n", 0

    monkeypatch.setattr(fleet_pulse, "_sweep", run)
    result = fleet_pulse.pulse_fleet(root=tmp_path, fleet="example")
    assert called == [([str(native / "fleet-pulse.sh"), "example"], "selected-release")]
    assert result.summary == "worker DOWN\n"
    assert result.stderr_tail == "watchdog dark\n"
    assert result.summary_path == tmp_path / "state/pulse/example.pulse-summary.txt"

    worker = SimpleNamespace(fleet=destination.fleet, bot_id="worker")
    monkeypatch.setattr(fleet_pulse, "resolve_operation_scope",
                        lambda **_kwargs: (destination, worker))
    with pytest.raises(fleet_pulse.FleetPulseError, match="manager"):
        fleet_pulse.pulse_fleet(root=tmp_path, fleet="example")
    assert len(called) == 1


def test_pulse_preserves_native_warning_and_summary(capfd):
    summary, tail, code = fleet_pulse._sweep(
        ["/bin/sh", "-c", "printf 'worker DOWN\\n'; printf 'watchdog dark\\n' >&2"],
        dict(os.environ))
    assert (summary, tail, code) == ("worker DOWN\n", "watchdog dark\n", 0)
    assert "watchdog dark" in capfd.readouterr().err


def test_pulse_timeout_kills_its_private_process_group(monkeypatch):
    child = SimpleNamespace(pid=731, calls=0)
    def communicate(*, timeout=None):
        child.calls += 1
        if timeout is not None:
            raise subprocess.TimeoutExpired("pulse", timeout)
        return b"", None
    child.communicate = communicate
    child.returncode = -9
    monkeypatch.setattr(fleet_pulse.subprocess, "Popen", lambda *a, **k: child)
    killed = []
    monkeypatch.setattr(fleet_pulse.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(fleet_pulse.FleetPulseError) as failure:
        fleet_pulse._sweep(["private-pulse"], {})
    assert failure.value.code == "timeout" and failure.value.effect_attempted
    assert killed == [(731, fleet_pulse.signal.SIGKILL)]


def test_selected_private_pulse_refuses_direct_entry(tmp_path):
    script = Path(__file__).resolve().parent.parent / "lib/fleet-pulse.sh"
    env = {**os.environ, "CLAUDLOBBY_RELEASE_ID": "selected-release",
           "CLAUDLOBBY_ROOT": str(tmp_path)}
    env.pop("CLAUDLOBBY_PRIVATE_PULSE_RELEASE", None)
    result = subprocess.run(["bash", str(script), "example"], env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 3
    assert "use claudlobby fleet pulse" in result.stderr
    assert not (tmp_path / "state/pulse").exists()


def test_reconcile_keeps_enrollment_and_private_session_distinct(tmp_path, monkeypatch):
    destination, _, selected = _scope(tmp_path)
    monkeypatch.setattr(fleet, "_scope", lambda *_a, **_k: (destination, None, selected))
    monkeypatch.setattr(fleet, "read_plan", lambda *_a: SimpleNamespace(release_id="selected-release"))
    monkeypatch.setattr(fleet, "current_declarations", lambda *_a: ())
    monkeypatch.setattr(fleet, "selected_bot_entry", lambda _r, _f, bot, _p: {
        "target": bot + ".service", "installed": str(tmp_path / (bot + ".service"))})
    monkeypatch.setattr(fleet, "build_supervision_spec", lambda _b, _f, _p: SimpleNamespace(
        bot_dir=tmp_path, label="private", environment={"TMUX_TMPDIR": str(tmp_path)}))
    observed = {"worker-a": (False, "inactive", "absent"),
                "worker-b": (True, "active", "absent"),
                "manager": (True, "active", "ready")}
    units = [SimpleNamespace(declaration=SimpleNamespace(scope="bot", fleet="example",
             bot=bot, working_directory=tmp_path), target=bot + ".service",
             installed=(SimpleNamespace(path=str(tmp_path / (bot + ".service"))),) if enrolled else (),
             properties=(("ActiveState", active),))
             for bot, (enrolled, active, _session) in observed.items()]
    monkeypatch.setattr(fleet, "collect_enrollment", lambda *_a, **_k: SimpleNamespace(
        require_complete=lambda: SimpleNamespace(units=units)))
    class Native:
        package = destination.paths.package
        def read(self, function):
            assert function == "svc_inventory_catalog"
            return f"manager\tLinux\ndirectory\t{tmp_path}\n"
        def call(self, function, bot_dir, label, tmux_dir, installed, target):
            assert function == "svc_bot_session_observe" and bot_dir == tmp_path
            assert installed == str(tmp_path / (next(expected_bots) + ".service"))
            assert target.endswith(".service")
            # The selected fixture's spec is per-bot in production; this seam
            # returns each observation in the same stable sweep order.
            return subprocess.CompletedProcess([], 0, next(sessions) + "\n", "")
    sessions = iter(observed[bot][2] for bot in ("worker-a", "worker-b", "manager"))
    expected_bots = iter(("worker-a", "worker-b", "manager"))
    result = fleet.reconcile_fleet(root=tmp_path, fleet="example", adapter=Native())
    assert [(row.bot, row.declared, row.enrolled, row.session, row.state)
            for row in result.bots] == [
                ("worker-a", True, False, "absent", "unsupervised_down"),
                ("worker-b", True, True, "absent", "missing"),
                ("manager", True, True, "ready", "healthy")]
    sessions = iter(("absent",))
    expected_bots = iter(("worker-a",))
    one = fleet.reconcile_fleet(root=tmp_path, fleet="example", bot="worker-a", adapter=Native())
    assert [(row.bot, row.state) for row in one.bots] == [("worker-a", "unsupervised_down")]
