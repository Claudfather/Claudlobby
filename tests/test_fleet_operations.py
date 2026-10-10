"""Selected fleet sweeps retain serial outcomes and read native state without repair."""

from pathlib import Path
from types import SimpleNamespace
import os
import shutil
import socket
import subprocess

import pytest

from claudlobby import fleet_operations as fleet
from claudlobby import fleet_pulse
from claudlobby.bot_operations import BotLifecycleError, BotLifecycleResult
from claudlobby.config import FleetPulseConfig
from tests.ingest_listener import short_socket_dir


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


def test_fleet_restart_skips_de_enrolled_bot_and_continues(tmp_path, monkeypatch):
    destination, origin, selected = _scope(tmp_path)
    monkeypatch.setattr(fleet, "_scope", lambda *_a, **_k: (destination, origin, selected))
    calls = []
    def restart(**kwargs):
        calls.append(kwargs["bot"])
        if kwargs["bot"] == "worker-a":
            raise BotLifecycleError("bot is de-enrolled; use bot start", skip_reason="de_enrolled",
                                    release_id="selected-release", target="worker-a-unit")
        if kwargs["bot"] == "manager":
            raise BotLifecycleError("bot native state is failed; use bot stop, then bot start")
        return BotLifecycleResult("example", kwargs["bot"], "selected-release",
                                  kwargs["bot"] + "-unit", "running", True, "bridge_ready", "attempted")
    monkeypatch.setattr(fleet, "set_bot_running", restart)
    done = fleet.set_fleet_running(root=tmp_path, fleet="example", action="restart",
                                   workers_only=True)
    assert calls == ["worker-a", "worker-b"]
    assert [(row.bot, row.state, row.changed, row.reason) for row in done.completed] == [
        ("worker-a", "skipped", False, "de_enrolled"), ("worker-b", "running", True, None)]
    # Only restart skips; any other refusal still stops the sweep honestly.
    calls.clear()
    with pytest.raises(fleet.FleetLifecycleError) as failed:
        fleet.set_fleet_running(root=tmp_path, fleet="example", action="restart")
    assert calls == ["worker-a", "worker-b", "manager"] and failed.value.bot == "manager"
    assert [row.state for row in failed.value.completed] == ["skipped", "running"]


def test_fleet_start_does_not_skip_de_enrolled_refusal(tmp_path, monkeypatch):
    destination, origin, selected = _scope(tmp_path)
    monkeypatch.setattr(fleet, "_scope", lambda *_a, **_k: (destination, origin, selected))
    def start(**kwargs):
        raise BotLifecycleError("refused", skip_reason="de_enrolled")
    monkeypatch.setattr(fleet, "set_bot_running", start)
    with pytest.raises(fleet.FleetLifecycleError) as failed:
        fleet.set_fleet_running(root=tmp_path, fleet="example", action="start")
    assert failed.value.bot == "worker-a" and failed.value.completed == ()


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
    destination = SimpleNamespace(fleet=SimpleNamespace(name="example", manager="manager",
                                                        fleet_pulse=FleetPulseConfig(timeout_s=450)),
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

    def run(command, env, **kwargs):
        called.append((command, env["CLAUDLOBBY_PRIVATE_PULSE_RELEASE"], kwargs))
        return "worker DOWN\n", "watchdog dark\n", 0

    monkeypatch.setattr(fleet_pulse, "_sweep", run)
    result = fleet_pulse.pulse_fleet(root=tmp_path, fleet="example")
    # fleet.yaml's cap and the summary path reach the sweep (#2059)
    assert called == [([str(native / "fleet-pulse.sh"), "example"], "selected-release",
                       {"timeout_s": 450, "summary_path": tmp_path / "state/pulse/example.pulse-summary.txt"})]
    assert result.summary == "worker DOWN\n"
    assert result.stderr_tail == "watchdog dark\n"
    assert result.summary_path == tmp_path / "state/pulse/example.pulse-summary.txt"

    worker = SimpleNamespace(fleet=destination.fleet, bot_id="worker")
    monkeypatch.setattr(fleet_pulse, "resolve_operation_scope",
                        lambda **_kwargs: (destination, worker))
    with pytest.raises(fleet_pulse.FleetPulseError, match="manager"):
        fleet_pulse.pulse_fleet(root=tmp_path, fleet="example")
    assert len(called) == 1


def test_pulse_preserves_native_warning_and_summary(capfd, tmp_path):
    summary, tail, code = fleet_pulse._sweep(
        ["/bin/sh", "-c", "printf 'worker DOWN\\n'; printf 'watchdog dark\\n' >&2"],
        dict(os.environ), summary_path=tmp_path / "pulse-summary.txt")
    assert (summary, tail, code) == ("worker DOWN\n", "watchdog dark\n", 0)
    assert "watchdog dark" in capfd.readouterr().err


def test_pulse_timeout_kills_its_private_process_group(monkeypatch, tmp_path):
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
        fleet_pulse._sweep(["private-pulse"], {}, summary_path=tmp_path / "pulse-summary.txt")
    assert failure.value.code == "timeout" and failure.value.effect_attempted
    # SIGTERM first, so the sweep's own traps can clean up; SIGKILL what is left.
    assert killed == [(731, fleet_pulse.signal.SIGTERM), (731, fleet_pulse.signal.SIGKILL)]


def test_selected_private_pulse_refuses_direct_entry(tmp_path):
    script = Path(__file__).resolve().parent.parent / "claudlobby/_runtime_scripts/fleet-pulse.sh"
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


def test_reconcile_reads_a_cleanly_stopped_session_absent_only_by_the_quiet_proof(tmp_path, monkeypatch):
    """#2227: tmux leaves its socket file after a clean exit and bot stop keeps it,
    so the session observer reads unknown. As bot move does, only the kernel proof
    (inactive exact unit, empty cgroup where witnessed, socket refusing
    connections) reads that as absent; anything less stays unknown."""
    destination, _, selected = _scope(tmp_path)
    monkeypatch.setattr(fleet, "_scope", lambda *_a, **_k: (destination, None, selected))
    monkeypatch.setattr(fleet, "read_plan", lambda *_a: SimpleNamespace(release_id="selected-release"))
    monkeypatch.setattr(fleet, "current_declarations", lambda *_a: ())
    monkeypatch.setattr(fleet, "selected_bot_entry", lambda _r, _f, bot, _p: {
        "target": bot + ".service", "installed": str(tmp_path / (bot + ".service"))})
    tmux_dir = short_socket_dir("sq-")
    stale = tmux_dir / f"tmux-{os.getuid()}" / "private"
    stale.parent.mkdir()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(stale))  # bound, never listening: what a clean tmux exit leaves
    monkeypatch.setattr(fleet, "build_supervision_spec", lambda _b, _f, _p: SimpleNamespace(
        bot_dir=tmp_path, label="private", environment={"TMUX_TMPDIR": str(tmux_dir)}))
    unit = SimpleNamespace(declaration=SimpleNamespace(scope="bot", fleet="example", bot="worker-a",
                                                       working_directory=tmp_path),
                           target="worker-a.service", installed=(),
                           properties=(("ActiveState", "inactive"),))
    monkeypatch.setattr(fleet, "collect_enrollment", lambda *_a, **_k: SimpleNamespace(
        require_complete=lambda: SimpleNamespace(units=[unit])))
    native = {"session": "unknown", "quiet": (0, "inactive\tno-cgroup-witness\n")}
    calls = []

    class Native:
        package = destination.paths.package

        def read(self, function):
            assert function == "svc_inventory_catalog"
            return f"manager\tLinux\ndirectory\t{tmp_path}\n"

        def call(self, function, *args, timeout=30):
            calls.append(function)
            if function == "svc_bot_session_observe":
                return subprocess.CompletedProcess([], 0, native["session"] + "\n", "")
            assert function == "svc_activation_quiet"
            assert args[:2] == (tmp_path / "worker-a.service", "worker-a.service")
            rc, out = native["quiet"]
            return subprocess.CompletedProcess([], rc, out, "")

    def session():
        calls.clear()
        row, = fleet.reconcile_fleet(root=tmp_path, fleet="example", bot="worker-a",
                                     adapter=Native()).bots
        return row.session, row.state

    try:
        assert session() == ("absent", "unsupervised_down")
        assert calls == ["svc_bot_session_observe", "svc_activation_quiet"]
        assert stale.exists()  # the proof never removes the socket file
        native["quiet"] = (3, "")  # the unit still active, or its session still exiting
        assert session() == ("unknown", "indeterminate")
        native["quiet"] = (0, "inactive\tno-cgroup-witness\n")
        stale.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as live:
            live.bind(str(stale))
            live.listen(1)  # a server still answers on the socket: never absent
            assert session() == ("unknown", "indeterminate")
        for observed in ("ready", "absent"):  # a definite observation needs no proof
            native["session"] = observed
            session()
            assert calls == ["svc_bot_session_observe"]
    finally:
        shutil.rmtree(tmux_dir, ignore_errors=True)


def test_reconcile_reads_a_recorded_stop_as_stopped(tmp_path, monkeypatch):
    """#2243 F8: not enrolled, no ready session and the stop door's record is `stopped`, whatever
    the session probe could tell: `absent`, or `unknown` when the quiet proof fails (#2227). With
    no record the states are unchanged."""
    destination, _, selected = _scope(tmp_path)
    monkeypatch.setattr(fleet, "_scope", lambda *_a, **_k: (destination, None, selected))
    monkeypatch.setattr(fleet, "read_plan", lambda *_a: SimpleNamespace(release_id="selected-release"))
    monkeypatch.setattr(fleet, "current_declarations", lambda *_a: ())
    monkeypatch.setattr(fleet, "selected_bot_entry", lambda _r, _f, bot, _p: {
        "target": bot + ".service", "installed": str(tmp_path / (bot + ".service"))})
    monkeypatch.setattr(fleet, "build_supervision_spec", lambda bot, _f, _p: SimpleNamespace(
        bot_dir=tmp_path / bot, label="private", environment={"TMUX_TMPDIR": str(tmp_path)}))
    observed = {"worker-a": (True, "absent"), "worker-b": (True, "unknown"),
                "manager": (False, "absent")}
    destination.fleet.bots = {bot: bot for bot in observed}
    for bot, (record, _session) in observed.items():
        (tmp_path / bot / "data").mkdir(parents=True)
        if record:
            (tmp_path / bot / "data" / ".stopped").write_text(
                '{"by": "operator", "reason": null, "request_id": "r", '
                '"stopped_at": "2026-10-09T20:40:00Z", "stopped_epoch": 1791578400}\n')
    units = [SimpleNamespace(declaration=SimpleNamespace(scope="bot", fleet="example",
             bot=bot, working_directory=tmp_path / bot), target=bot + ".service",
             installed=(), properties=(("ActiveState", "inactive"),))
             for bot in observed]
    monkeypatch.setattr(fleet, "collect_enrollment", lambda *_a, **_k: SimpleNamespace(
        require_complete=lambda: SimpleNamespace(units=units)))
    class Native:
        package = destination.paths.package
        def read(self, function):
            return f"manager\tLinux\ndirectory\t{tmp_path}\n"
        def call(self, function, *args, timeout=30):
            if function == "svc_activation_quiet":
                # the quiet proof fails, so an unknown session stays unknown (#2227)
                return subprocess.CompletedProcess([], 3, "", "")
            assert function == "svc_bot_session_observe"
            return subprocess.CompletedProcess([], 0, observed[args[0].name][1] + "\n", "")
    result = fleet.reconcile_fleet(root=tmp_path, fleet="example", adapter=Native())
    assert [(row.bot, row.enrolled, row.session, row.state) for row in result.bots] == [
        ("worker-a", False, "absent", "stopped"),
        ("worker-b", False, "unknown", "stopped"),
        ("manager", False, "absent", "unsupervised_down")]
