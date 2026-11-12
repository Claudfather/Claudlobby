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
from claudlobby.installation import Installation
from claudlobby.runtime_admission import GitCurrentRuntime
from tests.ingest_listener import short_socket_dir


def _scope(tmp_path, *, origin=None):
    selected_fleet = SimpleNamespace(name="example", manager="manager",
                                     bots={bot: object() for bot in ("manager", "worker-b", "worker-a")})
    package = SimpleNamespace(native=tmp_path / "native",installation=Installation(tmp_path.parent/"managed",tmp_path),source_revision="a"*40)
    destination = SimpleNamespace(fleet=selected_fleet,
                                  paths=SimpleNamespace(root=tmp_path, package=package))
    return destination, origin, GitCurrentRuntime(package.installation,"a"*40,"b"*64,{})


def _bindings(monkeypatch,destination,current,*,directory,tmux_dir=None,per_bot=False):
    from claudlobby import active_config
    monkeypatch.setattr(active_config,"read_current_config",lambda *_a,**_k:current.snapshot)
    declarations=tuple(SimpleNamespace(source=directory/(bot+".service"),scope="bot",fleet="example",bot=bot)
                       for bot in destination.fleet.bots)
    def bound(root,admitted,selected,bot,adapter):
        assert admitted is current
        bot_dir=directory/bot if per_bot else directory
        return SimpleNamespace(adapter=adapter,manager="Linux",declarations=declarations,
            entry={"target":bot+".service","installed":str(directory/(bot+".service"))},
            spec=SimpleNamespace(bot_dir=bot_dir,label="private",environment={"TMUX_TMPDIR":str(tmux_dir or directory)}),
            verify_configuration=lambda:None)
    monkeypatch.setattr(fleet,"_current_bot_binding",bound)


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


@pytest.mark.parametrize("skip_reason", ["de_enrolled", "disabled"])
def test_fleet_restart_skips_deliberately_stopped_bot_and_continues(tmp_path, monkeypatch, skip_reason):
    destination, origin, selected = _scope(tmp_path)
    monkeypatch.setattr(fleet, "_scope", lambda *_a, **_k: (destination, origin, selected))
    calls = []
    def restart(**kwargs):
        calls.append(kwargs["bot"])
        if kwargs["bot"] == "worker-a":
            raise BotLifecycleError("use bot start", skip_reason=skip_reason,
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
        ("worker-a", "skipped", False, skip_reason), ("worker-b", "running", True, None)]
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
                                  paths=SimpleNamespace(root=tmp_path, lib=native,
                                                        package=SimpleNamespace(installation=Installation(tmp_path.parent/"managed",tmp_path), runtime_native=native)))

    @contextmanager
    def admitted(root, **kwargs):
        assert root == tmp_path
        yield release

    monkeypatch.setattr(fleet_pulse, "mutation_admission", admitted)
    monkeypatch.setattr(fleet_pulse, "native_environment", lambda _paths, current: {"CLAUDLOBBY_NATIVE_ADMITTED": "1"})
    monkeypatch.setattr(fleet_pulse, "resolve_operation_scope",
                        lambda **_kwargs: (destination, None))
    called = []

    def run(command, env, **kwargs):
        called.append((command, env["CLAUDLOBBY_PRIVATE_PULSE_RUNTIME"], kwargs))
        return "worker DOWN\n", "watchdog dark\n", 0

    monkeypatch.setattr(fleet_pulse, "_sweep", run)
    result = fleet_pulse.pulse_fleet(root=tmp_path, fleet="example")
    # fleet.yaml's cap and the summary path reach the sweep (#2059)
    assert called == [([str(native / "fleet-pulse.sh"), "example"], "1",
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
    child = SimpleNamespace(pid=731, waits=[])
    def wait(*, timeout):  # a sweep that has not exited, even after SIGKILL
        child.waits.append(timeout)
        raise subprocess.TimeoutExpired("pulse", timeout)
    child.communicate = child.wait = wait
    monkeypatch.setattr(fleet_pulse.subprocess, "Popen", lambda *a, **k: child)
    killed = []
    monkeypatch.setattr(fleet_pulse.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(fleet_pulse.FleetPulseError) as failure:
        fleet_pulse._sweep(["private-pulse"], {}, summary_path=tmp_path / "pulse-summary.txt")
    assert failure.value.code == "timeout" and failure.value.effect_attempted
    # SIGTERM first, so the sweep's own traps can clean up; SIGKILL what is left.
    assert killed == [(731, fleet_pulse.signal.SIGTERM), (731, fleet_pulse.signal.SIGKILL)]
    # Every wait is bounded, so a sweep that will not die still ends the stop.
    assert child.waits[1:] == [fleet_pulse.TERM_GRACE_S, fleet_pulse.REAP_WAIT_S]


def test_pulse_stop_takes_eperm_as_a_group_with_no_live_member(monkeypatch):
    """macOS refuses a signal to a group none of whose members can take one (EPERM),
    as after the sweep exits and before it is reaped. The stop goes on to reap it."""
    child = SimpleNamespace(pid=731, waits=[])
    def wait(*, timeout):
        child.waits.append(timeout)
    child.communicate = child.wait = wait
    def refuse(pid, sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(fleet_pulse.os, "killpg", refuse)
    fleet_pulse._stop(child)
    assert child.waits == [fleet_pulse.REAP_WAIT_S]


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
    _bindings(monkeypatch,destination,selected,directory=tmp_path)
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
    tmux_dir = short_socket_dir("sq-")
    stale = tmux_dir / f"tmux-{os.getuid()}" / "private"
    stale.parent.mkdir()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(stale))  # bound, never listening: what a clean tmux exit leaves
    _bindings(monkeypatch,destination,selected,directory=tmp_path,tmux_dir=tmux_dir)
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
    _bindings(monkeypatch,destination,selected,directory=tmp_path,per_bot=True)
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


def test_fleet_lifecycle_lines_carry_what_each_bot_start_says(tmp_path, monkeypatch):
    """#2243: `fleet start --workers` prints, under each bot, what `bot start` prints: a stop
    record the start could not remove keeps a later loss of that bot's unit silent."""
    from argparse import Namespace

    from claudlobby import context
    from claudlobby.commands import fleet_runtime

    monkeypatch.setattr(context, "resolve_paths", lambda root=None: SimpleNamespace(root=tmp_path))
    kept = BotLifecycleResult("example", "worker-a", "selected-release", "worker-a-unit",
                              "running", True, "bridge_ready", recording="committed",
                              stop_record_kept=True)
    clean = BotLifecycleResult("example", "worker-b", "selected-release", "worker-b-unit",
                               "running", True, "bridge_ready", recording="committed")
    monkeypatch.setattr(fleet, "set_fleet_running", lambda **_k: fleet.FleetLifecycleResult(
        "example", "selected-release", "start", True, (kept, clean)))
    out = fleet_runtime.dispatch(Namespace(public_command="fleet.start", seed=False,
                                           fleet="example", workers=True, root=str(tmp_path)))
    first, note, last = out.lines
    assert first == "example/worker-a: running; readiness=bridge_ready"
    assert note.startswith("example/worker-a: ") and "could not remove this bot's stop record" in note
    assert last == "example/worker-b: running; readiness=bridge_ready"


# Reuse the existing real generated Git unit/bot.conf fixture; native effects
# remain explicit adapter observations and are never sent to a host manager.
from tests.test_git_composition import candidate  # noqa: F401
from tests.test_bot_operations import git_bot_binding  # noqa: F401


class _GitStoppedNative:
    def __init__(self, case):
        self.package, self.directory, self.calls = case.adapter.package, case.directory, []
        self.foreign = False

    def read(self, function, *args):
        self.calls.append((function, args))
        if function == 'svc_inventory_catalog':
            return f'manager\tLinux\ndirectory\t{self.directory}\n'
        assert function == 'svc_inventory_properties'
        return '\n'.join(f'{key}={value}' for key, value in {
            'Id':args[0], 'LoadState':'not-found', 'ActiveState':'inactive', 'UnitFileState':'',
            'FragmentPath':'', 'DropInPaths':'', 'NeedDaemonReload':'no', 'Triggers':'', 'TriggeredBy':'',
            'WorkingDirectory':'', 'Environment':'', 'ExecStart':''}.items())

    def call(self, function, *args, timeout=30):
        self.calls.append((function, args))
        if function == 'svc_bot_unit_owned_by':
            return subprocess.CompletedProcess([], 1 if self.foreign else 0, '', '')
        assert function == 'svc_bot_session_observe'
        return subprocess.CompletedProcess([], 0, 'absent\n', '')


def test_git_reconcile_preserves_stopped_intent_without_historical_selection(git_bot_binding, monkeypatch):
    from claudlobby.stop_record import write_stop
    case = git_bot_binding
    (case.root / 'state/selected-release.json').write_text('malformed obsolete selection\n')
    monkeypatch.setattr(fleet, 'resolve_operation_scope', lambda **_kw: (case.destination, None))
    native = _GitStoppedNative(case)
    bot_dir = case.destination.paths.bot_runtime('lead')
    (bot_dir / 'data').mkdir()
    write_stop(bot_dir, by='operator', request_id='stopped-intent', reason='Retain intentional stop')
    result = fleet.reconcile_fleet(root=case.root, fleet='example', adapter=native)
    row, = result.bots
    assert (row.bot, row.enrolled, row.session, row.state) == ('lead', False, 'absent', 'stopped')
    assert result.release_id is None and result.source_revision == case.current.revision
    assert result.config_fingerprint == case.current.config_fingerprint
    assert not (case.directory / 'com.example.lead.service').exists()
    native.foreign = True
    with pytest.raises(fleet.InventoryError, match='foreign or unknown'):
        fleet.reconcile_fleet(root=case.root, fleet='example', adapter=native)


class _GitDanglingLinkNative(_GitStoppedNative):
    """#2700: the unit file is gone, its default.target.wants link stayed, and systemd
    still lists the unit (not-found), so the catalogue reports it loaded."""

    def __init__(self, case, quiet):
        super().__init__(case)
        self.quiet = quiet  # svc_activation_quiet's (rc, stdout)

    def read(self, function, *args):
        if function == 'svc_inventory_catalog':
            return super().read(function, *args) + 'loaded\tcom.example.lead.service\n'
        return super().read(function, *args)

    def call(self, function, *args, timeout=30):
        if function == 'svc_activation_quiet':
            self.calls.append((function, args))
            return subprocess.CompletedProcess([], *self.quiet, '')
        return super().call(function, *args, timeout=timeout)


def test_git_reconcile_reads_a_unit_left_listed_by_a_dangling_wants_link_as_down(git_bot_binding, monkeypatch):
    """#2700: the lead's unit file removed and its default.target.wants link left behind
    made reconcile refuse the fleet; a unit listed only through that link runs nothing."""
    case = git_bot_binding
    monkeypatch.setattr(fleet, 'resolve_operation_scope', lambda **_kw: (case.destination, None))
    unit = case.directory / 'com.example.lead.service'
    (case.directory / 'default.target.wants').mkdir()
    (case.directory / 'default.target.wants' / unit.name).symlink_to(unit)
    native = _GitDanglingLinkNative(case, (0, 'inactive\tno-cgroup-witness\n'))
    result = fleet.reconcile_fleet(root=case.root, fleet='example', adapter=native)
    row, = result.bots
    assert (row.bot, row.enrolled, row.session, row.state) == ('lead', False, 'absent', 'unsupervised_down')
    assert [args[1] for function, args in native.calls if function == 'svc_activation_quiet'] == [unit.name]
    # A process left in the unit's cgroup is a consumer: reconcile still refuses.
    native = _GitDanglingLinkNative(case, (3, ''))
    with pytest.raises(fleet.InventoryError, match=f'{unit.name}: loaded consumer lacks installed source bytes'):
        fleet.reconcile_fleet(root=case.root, fleet='example', adapter=native)


@pytest.mark.parametrize('skip_reason', ['de_enrolled', 'disabled'])
def test_git_fleet_restart_delegates_frozen_current_scope_and_skips_stopped_bot(git_bot_binding, monkeypatch, skip_reason):
    case = git_bot_binding
    monkeypatch.setattr(fleet, 'resolve_operation_scope', lambda **_kw: (case.destination, None))
    calls = []
    def restart(**kwargs):
        calls.append(kwargs)
        assert kwargs['_expected_provenance'] == (None, {'source_revision':case.current.revision,
                                                        'config_fingerprint':case.current.config_fingerprint})
        raise BotLifecycleError('deliberately stopped', skip_reason=skip_reason, target='com.example.lead.service',
                                source_revision=case.current.revision, config_fingerprint=case.current.config_fingerprint)
    monkeypatch.setattr(fleet, 'set_bot_running', restart)
    result = fleet.set_fleet_running(root=case.root, fleet='example', action='restart')
    assert len(calls) == 1 and calls[0]['running'] and calls[0]['restart']
    assert result.release_id is None and result.source_revision == case.current.revision
    assert [(row.bot, row.state, row.changed) for row in result.completed] == [('lead', 'skipped', False)]
    assert result.completed[0].config_fingerprint == case.current.config_fingerprint
    assert result.completed[0].reason == skip_reason
