"""Selected bot placement remains readable after deliberate de-enrollment."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import time

import pytest

from claudlobby import activation, bot_operations, context
from claudlobby.__main__ import main
from claudlobby.activation_enrollment import selected_bot_entry
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.config_units import planned_units
from tests.package_fixtures import source_package
from tests.test_activation import cold, tmp_path  # noqa: F401 — real short-root activation fixtures
from tests.test_releases import installed  # noqa: F401 — cold fixture dependency


def test_private_control_uses_one_exact_native_send(tmp_path):
    """Exercise the shell adapter with fake tmux; no live session is touched."""
    native = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/supervisor.sh"
    private = tmp_path / "native"
    private.mkdir()
    (private / "lib-common.sh").write_text("""
tmux_session_name() { printf 'worker'; }
bot_tmux() { printf 'tmux %s\\n' "$*" >> "$CONTROL_LOG"; }
pane_send_verified() {
    printf 'pane %s %s %s ticks=%s\\n' "$1" "$2" "$3" "$PANE_SEND_VERIFY_TICKS" >> "$CONTROL_LOG"
}
""")
    log = tmp_path / "calls"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "_SUPERVISOR_LIB_DIR": str(private), "CONTROL_LOG": str(log),
           "PLANE_EMIT_DISABLED": "1"}
    script = '. "$1"; svc_bot_session_observe() { printf "ready\\n"; }; svc_bot_control_exact "$2" "$3" "$4" "$5"'
    def run(control):
        return subprocess.run(["/bin/bash", "-c", script, "control", str(native),
                               str(tmp_path / "bot"), "worker.socket", str(tmp_path), control],
                              env=env, capture_output=True, text=True, timeout=5)

    interrupt = run("interrupt")
    assert interrupt.returncode == 0 and interrupt.stdout == "control-submitted\n"
    assert log.read_text() == "tmux worker.socket send-keys -t worker Escape\n"
    log.unlink()
    compact = run("compact")
    assert compact.returncode == 0 and compact.stdout == "control-submitted\n"
    assert log.read_text() == "pane worker.socket worker /compact ticks=0\n"
    log.unlink()
    refused = run("message")
    assert refused.returncode == 3 and refused.stdout == "" and not log.exists()


@pytest.mark.parametrize("message, mode, expected_rc", [
    ("no server running on", "retired", 0),
    ("no server running on", "retired-purge", 3),
    ("permission denied on", "retired", 3),
    ("wrong socket", "retired", 3),
])
def test_retired_private_server_handles_stale_socket_without_hiding_errors(tmp_path, message, mode, expected_rc):
    native = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/supervisor.sh"
    private = tmp_path / "native"
    private.mkdir()
    (private / "lib-common.sh").write_text('''
tmux_session_name() { printf 'worker'; }
bot_tmux() {
    [ "$2" = list-sessions ] || exit 99
    printf '%s %s/tmux-%s/worker.socket\\n' "$FAILURE" "$SOCKET_ROOT" "$(id -u)" >&2
    return 1
}
''')
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "_SUPERVISOR_LIB_DIR": str(private), "FAILURE": message, "PLANE_EMIT_DISABLED": "1",
           "SOCKET_ROOT": str(tmp_path)}
    if message == "wrong socket":
        env.update(FAILURE="no server running on", SOCKET_ROOT=str(tmp_path / "other"))
    result = subprocess.run(["/bin/bash", "-c", '. "$1"; svc_activation_stop_private_server "$2" worker.socket "$3" "$4"',
                             "stop", str(native), str(tmp_path / "bot"), str(tmp_path), mode],
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == expected_rc
    if mode == "retired-purge":
        assert "liveness is unverified" in result.stderr


@pytest.mark.parametrize("stdout, attempted", [("", False), ("effect-attempted\n", True)])
def test_disenroll_native_phase_is_reported_honestly(stdout, attempted, tmp_path, monkeypatch):
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import bot_runtime

    class Native:
        def call(self, function, *args, timeout=30):
            assert function == "svc_bot_disenroll_exact"
            return subprocess.CompletedProcess([function], 3, stdout,
                                               "activation supervision unknown: caller ancestry")

    with pytest.raises(bot_operations.BotLifecycleError) as error:
        bot_operations._native(Native(), "svc_bot_disenroll_exact", "source", "installed")
    assert error.value.effect_attempted is attempted
    assert ("after beginning" if attempted else "before a native effect") in str(error.value)

    monkeypatch.setattr(context, "resolve_paths", lambda **kwargs: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(bot_operations, "set_bot_running", lambda **kwargs: (_ for _ in ()).throw(
        bot_operations.BotLifecycleError(str(error.value), effect_attempted=attempted,
                                         unavailable=True, release_id="selected", target="gui/501/worker")))
    args = SimpleNamespace(seed=False, bot_id="worker", public_command="bot.stop",
                           ceiling=None, fleet="example", root=tmp_path)
    with pytest.raises(CommandFailure) as public:
        bot_runtime.dispatch(args)
    assert public.value.data["native_outcome"] == ("unknown" if attempted else "unattempted")
    if not attempted:
        assert "before a native effect" in public.value.error.message


def test_lifecycle_lock_contention_is_a_retryable_conflict(tmp_path, monkeypatch):
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import bot_runtime

    (tmp_path / "state").mkdir(exist_ok=True)
    with bot_operations._operation_lock(tmp_path):
        with pytest.raises(bot_operations.BotLifecycleError) as busy:
            with bot_operations._operation_lock(tmp_path):
                pytest.fail("second lifecycle lock was granted")
    assert busy.value.busy and not busy.value.effect_attempted
    with bot_operations._operation_lock(tmp_path):
        pass

    monkeypatch.setattr(context, "resolve_paths", lambda **kwargs: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(bot_operations, "set_bot_running", lambda **kwargs: (_ for _ in ()).throw(busy.value))
    args = SimpleNamespace(seed=False, bot_id="worker", public_command="bot.restart",
                           ceiling=None, fleet="example", root=tmp_path)
    with pytest.raises(CommandFailure) as public:
        bot_runtime.dispatch(args)
    assert public.value.error.code == "conflict" and public.value.error.retryable
    assert "another bot lifecycle operation" in public.value.error.message
    assert public.value.data["native_outcome"] == "unattempted"


def test_selected_bot_placement_survives_removal_of_installed_unit(cold):  # noqa: F811
    root, _, plan, host = cold
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    entry = selected_bot_entry(root, "example", "worker", "Linux")
    installed_unit = Path(entry["installed"])
    assert installed_unit.is_file() and entry["target"] == "com.example.worker.service"
    installed_unit.unlink()
    assert selected_bot_entry(root, "example", "worker", "Linux") == entry


def test_background_caller_uses_selected_same_uid_gui_domain(tmp_path, monkeypatch):
    from claudlobby.supervision_inventory import Adapter, InventoryError

    source = tmp_path / "generated" / "com.example.worker.plist"
    source.parent.mkdir()
    source.write_text("private reviewed unit")
    installed = tmp_path / "LaunchAgents" / source.name
    installed.parent.mkdir()
    target = f"gui/{os.getuid()}/com.example.worker"
    entry = {"target": target, "source": str(source), "installed": str(installed)}
    monkeypatch.setattr(bot_operations, "selected_bot_entry", lambda *_: entry)
    declaration = SimpleNamespace(scope="bot", fleet="example", bot="worker", source=source)
    monkeypatch.setattr(bot_operations, "current_declarations", lambda *_: (declaration,))
    monkeypatch.setattr(bot_operations, "planned_units", lambda *_: ((declaration, {"enroll": True,
                                                                                   "sha256": "private"}),))
    monkeypatch.setattr(bot_operations, "validate_unit_admission", lambda *_: None)
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        domain = f"gui/{os.getuid()}" if command[0] == "/bin/launchctl" else f"user/{os.getuid()}"
        return subprocess.CompletedProcess(command, 0,
            f"manager\tDarwin\ndomain\t{domain}\ndirectory\t{installed.parent}\nPID Status Label\n", "")
    adapter = Adapter(source_package(), runner=runner)
    wrapped = bot_operations._selected_adapter(tmp_path, "example", "worker", adapter)
    manager, got, selected, _ = bot_operations._selected_unit(
        tmp_path, SimpleNamespace(blob=lambda _: b"private"), object(), adapter.package,
        wrapped, "example", "worker")
    assert manager == "Darwin" and got is declaration and selected == entry
    assert calls[0][0] == "/bin/bash"
    assert calls[1][:3] == ["/bin/launchctl", "asuser", str(os.getuid())]
    wrapped.call("svc_bot_enroll_exact", source, installed, target)
    assert calls[-1][:3] == ["/bin/launchctl", "asuser", str(os.getuid())]
    with pytest.raises(InventoryError, match="not owned"):
        adapter.in_selected_gui(f"gui/{os.getuid() + 1}/com.example.worker")


def test_self_restart_requires_fresh_owned_frontmatter(tmp_path):
    bot_dir = tmp_path / "bot"
    handoff_dir = bot_dir / ".claude"
    handoff_dir.mkdir(parents=True)
    handoff = handoff_dir / "session.md"
    now = datetime.now(timezone.utc)
    handoff.write_text(f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
    bot_operations._fresh_self_handoff(bot_dir)
    offset = now.astimezone(timezone(timedelta(hours=-4))).isoformat(timespec="seconds")
    handoff.write_text(f'---\nlast_updated: "{offset}"\n---\ncontext\n')
    bot_operations._fresh_self_handoff(bot_dir)
    handoff.write_text(f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%S}\n---\ncontext\n")
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned"):
        bot_operations._fresh_self_handoff(bot_dir)
    handoff.write_text("---\nlast_updated: 2020-01-01T00:00:00Z\n---\ncontext\n")
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned"):
        bot_operations._fresh_self_handoff(bot_dir)
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned"):
        bot_operations._fresh_self_handoff(bot_dir, observed_capture=True)
    future = now + timedelta(minutes=7)
    handoff.write_text(f"---\nlast_updated: {future:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
    bot_operations._fresh_self_handoff(bot_dir)
    handoff.unlink()
    foreign = tmp_path / "foreign.md"
    foreign.write_text(f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
    handoff.symlink_to(foreign)
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned"):
        bot_operations._fresh_self_handoff(bot_dir)


def test_explicit_handoff_keeps_running_session_env_while_stop_cleans_it(tmp_path):
    bot_dir = tmp_path / "runtime/bots/worker"
    (bot_dir / ".claude").mkdir(parents=True)
    (bot_dir / "bot.conf").write_text("BOT_ID=worker\nBOT_NAME=worker\nFLEET_NAME=example\n"
                                       "BOT_SERVICE=com.example.worker\nTMUX_SOCKET=com.example.worker\n")
    (bot_dir / ".claude/session.md").write_text("recent checkpoint\n")
    secret_env = bot_dir / ".tmux-env"
    secret_env.write_text("private launch values\n")
    script = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/pre-stop-handoff.sh"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "CLAUDLOBBY_ROOT": str(tmp_path), "PLANE_EMIT_DISABLED": "1"}
    explicit = subprocess.run(["/bin/bash", str(script), str(bot_dir), "--explicit"],
                              env=env, capture_output=True, text=True, timeout=10)
    assert explicit.returncode == 0, explicit.stderr
    assert explicit.stdout.strip() == "handoff-skipped:recent"
    assert secret_env.read_text() == "private launch values\n"
    (bot_dir / ".claude/session.md").write_text(
        "<!-- claudlobby first-adoption reference refresh begin -->\n"
        "Canonical task references, not a session capture.\n")
    reference_only = subprocess.run(["/bin/bash", str(script), str(bot_dir), "--explicit"],
                                   env=env, capture_output=True, text=True, timeout=10)
    assert reference_only.returncode == 0, reference_only.stderr
    assert reference_only.stdout.strip() == "handoff-skipped:no-session"
    assert secret_env.exists()
    stopping = subprocess.run(["/bin/bash", str(script), str(bot_dir)],
                              env=env, capture_output=True, text=True, timeout=10)
    assert stopping.returncode == 0, stopping.stderr
    assert not secret_env.exists()


@pytest.mark.parametrize("stalled, late_witness", [
    pytest.param(False, False, id="False"),
    pytest.param(True, False, id="True"),
    pytest.param(False, True, id="False-late-witness"),
    pytest.param(True, True, id="True-late-witness"),
])
def test_self_restart_witness_survives_requesting_process_exit(tmp_path, stalled, late_witness):
    """The one-shot witness, not the dying pane, owns the final outcome.

    A late witness is descheduled for a second between its admission and its wait
    (#2137). Looking late at a caller that has exited, it still logs complete: the
    caller finished, so the restart is safe."""
    bot_dir = tmp_path / "bot"
    (bot_dir / ".claude").mkdir(parents=True)
    (bot_dir / "logs").mkdir()
    now = datetime.now(timezone.utc)
    (bot_dir / ".claude/session.md").write_text(
        f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
    code = """
import os
import sys
import time
from pathlib import Path
from claudlobby import bot_operations as b
root, bot_dir = map(Path, sys.argv[1:3])
stalled = sys.argv[3] == '1'
b._SELF_RESPONSE_WAIT_S = 0.05 if stalled else 30
if sys.argv[4] == '1':
    parent, real_select = os.getpid(), b.select.select
    def late_select(*args):
        if os.getpid() != parent:
            time.sleep(1)
        return real_select(*args)
    b.select.select = late_select
b.read_selection = lambda _: {'release_id': 'selected'}
def restart(**kwargs):
    kwargs['_on_lock']()
    (bot_dir / 'native-effect').write_text('attempted')
    return b.BotLifecycleResult('fleet', 'bot', 'selected', 'unit', 'running', True,
                                'session_ready', 'captured')
b.set_bot_running = restart
b._schedule_self_restart(root=root, fleet='fleet', bot='bot', ceiling=None,
                         bot_dir=bot_dir)
if stalled:
    # Stalled means still alive when the witness's wait ends, so stay until it
    # logs that verdict: a fixed sleep raced the witness's own scheduling (#2137).
    log, deadline = bot_dir / 'logs/startup.log', time.monotonic() + 8
    while time.monotonic() < deadline and not (
            log.exists() and '"status":"incomplete"' in log.read_text()):
        time.sleep(0.01)
os._exit(0)  # even an abrupt caller death releases the child to finish
"""
    process = subprocess.run([os.sys.executable, "-c", code, str(tmp_path), str(bot_dir),
                              "1" if stalled else "0", "1" if late_witness else "0"],
                             capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    log = bot_dir / "logs/startup.log"
    for _ in range(100):
        if log.exists() and ('"status":"incomplete"' if stalled else '"status":"complete"') in log.read_text():
            break
        time.sleep(0.02)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["status"] == ("incomplete" if stalled else "complete")
    if stalled:
        assert "requesting CLI did not finish" in rows[0]["reason"]
        assert not (bot_dir / "native-effect").exists()
    else:
        assert rows[0]["readiness"] == "session_ready"
        assert (bot_dir / "native-effect").read_text() == "attempted"


def _worker_cli(cold, monkeypatch, capsys, *, exec_start_pre=None):
    """The selected worker's bot CLI over a fake native adapter.

    ``exec_start_pre`` seals the worker's systemd unit with that
    ExecStartPre line, the way the compositor renders a boot stagger.
    """
    root, release, plan, host = cold
    # Add the real selected bot.conf output absent from the general activation
    # fixture. Its other sealed inputs/units remain unchanged.
    builder = ConfigPlanBuilder(root, plan.release_id, plan.release_seal, plan.fleets,
                                effects=deepcopy(plan.effects))
    builder.inputs = deepcopy(plan.inputs)
    builder.contents = {path.name: path.read_bytes() for path in (plan.directory / "files").iterdir()}
    builder.changes = {change.target: change for change in plan.changes}
    builder.file(root / "runtime/bots/worker/bot.conf",
                 b"BOT_ID=worker\nBOT_NAME=worker\nFLEET_NAME=example\n"
                 b"BOT_SERVICE=com.example.worker\nTMUX_SOCKET=com.example.worker\n")
    if exec_start_pre is not None:
        # Seal the worker's unit with this ExecStartPre line, as a staggered
        # bot's composed unit carries it; the unit digest moves with its bytes.
        (unit,) = [unit for unit in builder.effects["units"]
                   if unit["source"].endswith("/com.example.worker.service")]
        staggered = builder.contents[unit["sha256"]].replace(
            b"[Service]\n", f"[Service]\nExecStartPre={exec_start_pre}\n".encode(), 1)
        unit["sha256"] = hashlib.sha256(staggered).hexdigest()
        builder.contents[unit["sha256"]] = staggered
        change = builder.changes[unit["source"]]
        builder.changes[unit["source"]] = replace(change, after={**change.after, "sha256": unit["sha256"]})
    plan = builder.seal()
    host.plan = plan
    host.by_name = {decl.source.name: (decl, item) for decl, item in planned_units(plan, "Linux")}
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    package = replace(source_package(), native=release.native_path,
                      artifact_id=release.inputs.artifact_id)
    monkeypatch.setattr(context, "get_resources", lambda: package)

    class Native:
        def __init__(self):
            self.package = package
            self.session = "ready"
            self.actions = []
            self.calls = []
            self.handoff_rc = 0
            self.control_rc = 0
            self.explicit_handoff = "handoff-skipped:recent"
            self.readiness = "bridge-ready"
            self.fence_args = []
            self.target = "com.example.worker.service"
            self.manager = "Linux"
            self.enroll_needs_s = 0

        def call(self, function, *args, timeout=30):
            self.calls.append(function)
            target = self.target
            if function == "svc_inventory_catalog":
                return host.call(function, *args, timeout=timeout)
            if function == "svc_inventory_state":
                value = host.states.get(args[1], "not-found not-found inactive")
            elif function == "svc_bot_session_observe":
                value = self.session
            elif function == "svc_activation_quiet":
                value = "inactive\tno-cgroup-witness"
            elif function == "svc_activation_bot_fence":
                self.fence_args.append(args)
                value = "0\tprivate-fence"
            elif function == "svc_activation_bot_ready":
                value = self.readiness
            elif function == "svc_activation_handoff":
                value = self.explicit_handoff if len(args) == 4 else ""
                if value == "handoff-saved":
                    handoff = Path(args[0]) / ".claude/session.md"
                    handoff.parent.mkdir(exist_ok=True)
                    # The real provider saved promptly but invented a future
                    # timestamp. Explicit native capture witnessed this write.
                    now = datetime.now(timezone.utc) + timedelta(minutes=7)
                    handoff.write_text(f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
            elif function == "svc_bot_control_exact":
                assert args[:2] == (root / "runtime/bots/worker", "com.example.worker")
                assert Path(args[2]).is_absolute()
                assert args[3] in {"interrupt", "compact"}
                value = "control-submitted" if self.control_rc == 0 else ""
            elif function == "svc_bot_disenroll_exact":
                self.actions.append("stop")
                Path(args[1]).unlink()
                host.states[target] = "not-found not-found inactive"
                value = ""
            elif function == "svc_bot_enroll_exact":
                self.actions.append("start")
                Path(args[1]).write_bytes(Path(args[0]).read_bytes())
                host.states[target] = ("unchanged loaded inactive" if self.manager == "Darwin"
                                       else "enabled loaded active")
                if timeout < self.enroll_needs_s:
                    # systemctl blocks through the unit's ExecStartPre: the
                    # adapter kills the client at its budget, the job runs on.
                    raise subprocess.TimeoutExpired([function], timeout)
                value = ""
            else:
                raise AssertionError(f"unexpected native call: {function}")
            return subprocess.CompletedProcess([function],
                                               self.handoff_rc if function == "svc_activation_handoff" else
                                               self.control_rc if function == "svc_bot_control_exact" else 0,
                                               value + "\n", "")

        def read(self, function, *args):
            return self.call(function, *args).stdout

    native = Native()
    monkeypatch.setattr(bot_operations, "Adapter", lambda _: native)

    def observed(_root, _declarations, _adapter, target, installed_path):
        assert target == native.target
        installed_rows = (SimpleNamespace(path=str(installed_path)),) if installed_path.exists() else ()
        active = host.states[target].split()[-1]
        return SimpleNamespace(installed=installed_rows, properties=(("ActiveState", active),))

    monkeypatch.setattr(bot_operations, "_observed", observed)
    monkeypatch.setattr(bot_operations, "assert_quiescent", lambda *a, **kw: None)

    def call(*argv, expected=0, use_root=True):
        root_flag = ["--root", str(root)] if use_root else []
        assert main([*root_flag, "--json", *argv]) == expected
        result = json.loads(capsys.readouterr().out)
        assert result["ok"] is (expected == 0)
        return result
    return SimpleNamespace(root=root, release=release, plan=plan, host=host,
                           native=native, call=call)


def test_public_bot_start_stop_decisions_use_selected_placement(cold, monkeypatch, capsys):  # noqa: F811
    worker = _worker_cli(cold, monkeypatch, capsys)
    root, release, plan, host, native, call = (worker.root, worker.release, worker.plan,
                                               worker.host, worker.native, worker.call)

    healthy = call("bot", "start", "worker")["data"]
    assert healthy["state"] == "running" and healthy["changed"] is False
    assert healthy["readiness"] == "current_session_ready" and native.actions == []

    native.calls.clear()
    skipped = call("bot", "handoff", "worker")["data"]
    assert skipped["handoff"] == "skipped" and skipped["reason"] == "recent"
    assert "svc_activation_handoff" in native.calls and native.actions == []
    for control in ("interrupt", "compact"):
        native.calls.clear()
        submitted = call("bot", control, "worker")["data"]
        assert submitted["outcome"] == "submitted" and submitted["control"] == control
        assert native.calls.index("svc_bot_session_observe") < native.calls.index("svc_bot_control_exact")
        assert native.actions == []
    native.control_rc = 3
    unknown_control = call("bot", "compact", "worker", expected=6)
    assert unknown_control["data"]["outcome"] == "unknown"
    assert "inspect" in unknown_control["error"]["hint"]
    native.control_rc = 0
    before_control = native.calls.count("svc_bot_control_exact")
    assert call("bot", "interrupt", "other", expected=4)["error"]["code"] == "conflict"
    assert native.calls.count("svc_bot_control_exact") == before_control
    # The same selected state must resolve identically for a generated manager
    # caller with an implicit fleet and one that spells out --fleet.
    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "manager")
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/manager"))
    monkeypatch.setenv("FLEET_ROOT", str(root))
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    monkeypatch.setenv("CLAUDLOBBY_RELEASE_ID", release.release_id)
    implicit = call("bot", "handoff", "worker", use_root=False)["data"]
    explicit = call("--fleet", "example", "bot", "handoff", "worker", use_root=False)["data"]
    assert implicit == explicit == skipped
    before_refusal = native.calls.count("svc_activation_handoff")
    assert call("bot", "handoff", "manager", expected=4)["error"]["code"] == "conflict"
    monkeypatch.setenv("BOT_ID", "worker")
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/worker"))
    assert call("bot", "handoff", "worker", expected=4)["error"]["code"] == "conflict"
    assert native.calls.count("svc_activation_handoff") == before_refusal
    assert call("bot", "compact", "worker", expected=4)["error"]["code"] == "conflict"
    assert native.calls.count("svc_bot_control_exact") == before_control
    for key in ("FLEET_NAME", "BOT_ID", "BOT_DIR", "FLEET_ROOT",
                "CLAUDLOBBY_ROOT", "CLAUDLOBBY_RELEASE_ID"):
        monkeypatch.delenv(key)
    native.explicit_handoff = ""  # the old native rc0 is not proof of a handoff
    unknown = call("bot", "handoff", "worker", expected=6)
    assert unknown["error"]["code"] == "unavailable" and native.actions == []
    assert unknown["data"]["handoff"] == "unknown"
    assert unknown["data"]["reason"] == "unverified"
    native.explicit_handoff = "handoff-timeout"
    native.handoff_rc = 3
    timed_out = call("bot", "handoff", "worker", expected=6)
    assert timed_out["error"]["code"] == "unavailable"
    assert timed_out["data"]["handoff"] == "unknown"
    assert timed_out["data"]["reason"] == "timeout"
    assert "do not automatically resend" in timed_out["error"]["hint"]
    assert native.actions == []
    native.handoff_rc = 0
    native.explicit_handoff = "handoff-saved"
    saved = call("bot", "handoff", "worker")["data"]
    assert saved["handoff"] == "saved" and saved["reason"] == "fresh_file_verified"
    assert native.actions == []  # a handoff never stops or re-enrolls the bot
    before_wrong = len(native.calls)
    assert call("bot", "handoff", "other", expected=4)["error"]["code"] == "conflict"
    assert len(native.calls) == before_wrong

    native.session = "unknown"
    assert call("bot", "start", "worker", expected=6)["error"]["code"] == "unavailable"
    assert native.actions == []

    native.session = "absent"  # exact active/exited unit with missing private socket
    recovered = call("bot", "start", "worker")["data"]
    assert recovered["changed"] is True and recovered["readiness"] == "bridge_ready"
    assert native.actions == ["start"]

    stopped = call("bot", "stop", "worker")["data"]
    assert stopped["state"] == "stopped" and stopped["changed"] is True
    assert native.actions == ["start", "stop"]
    restarted = call("bot", "start", "worker")["data"]
    assert restarted["state"] == "running" and restarted["changed"] is True
    assert native.actions == ["start", "stop", "start"]

    for observed_state in ("activating",):
        host.states[native.target] = f"enabled loaded {observed_state}"
        before = len(native.actions)
        for operation in ("start", "restart"):
            refused = call("bot", operation, "worker", expected=4)
            assert observed_state in refused["error"]["message"]
            assert "bot stop" in refused["error"]["message"]
        assert len(native.actions) == before
    host.states[native.target] = "enabled loaded active"

    for caller, target in (("worker", "manager"), ("manager", "manager")):
        monkeypatch.setenv("FLEET_NAME", "example")
        monkeypatch.setenv("BOT_ID", caller)
        monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
        monkeypatch.setenv("FLEET_ROOT", str(root))
        monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots" / caller))
        monkeypatch.setenv("CLAUDLOBBY_RELEASE_ID", release.release_id)
        refused = call("bot", "stop", target, expected=4)
        assert refused["error"]["code"] == "conflict"
        assert native.actions == ["start", "stop", "start"]

    # launchd's selected launcher has exited, but its exact private tmux
    # session is still ready. Native inactive alone must not trigger a restart.
    for key in ("BOT_ID", "FLEET_NAME", "BOT_DIR", "FLEET_ROOT",
                "CLAUDLOBBY_ROOT", "CLAUDLOBBY_RELEASE_ID"):
        monkeypatch.delenv(key)
    selected_unit_original = bot_operations._selected_unit
    selected_adapter_original = bot_operations._selected_adapter
    native.manager = "Darwin"
    native.target = f"gui/501/com.example.worker"
    host.states[native.target] = "unchanged loaded inactive"
    declaration, item = next((decl, item) for decl, item in planned_units(plan, "Linux")
                             if decl.bot == "worker" and item["enroll"])
    entry = dict(selected_bot_entry(root, "example", "worker", "Linux"))
    entry["target"] = native.target
    monkeypatch.setattr(bot_operations, "_selected_unit",
                            lambda *_: ("Darwin", declaration, entry, (declaration,)))
    # This branch simulates Darwin over the Linux host catalog; its selected
    # domain binding is covered by the separate Background-to-GUI test.
    monkeypatch.setattr(bot_operations, "_selected_adapter", lambda _r, _f, _b, adapter: adapter)
    native.session = "ready"
    current = call("bot", "start", "worker")["data"]
    assert current["state"] == "running" and current["changed"] is False
    assert current["readiness"] == "current_session_ready"
    assert native.actions == ["start", "stop", "start"]

    host.states[native.target] = "unchanged unloaded inactive"
    assert call("bot", "start", "worker", expected=4)["error"]["code"] == "conflict"
    assert native.actions == ["start", "stop", "start"]
    host.states[native.target] = "unchanged loaded inactive"

    native.session = "unknown"
    assert call("bot", "start", "worker", expected=6)["error"]["code"] == "unavailable"
    assert native.actions == ["start", "stop", "start"]

    native.session = "ready"
    assert call("bot", "stop", "worker")["data"]["state"] == "stopped"
    started = call("bot", "start", "worker")["data"]
    assert started["changed"] is True and started["readiness"] == "bridge_ready"
    assert host.states[native.target] == "unchanged loaded inactive"
    assert native.actions == ["start", "stop", "start", "stop", "start"]

    # Restart is an intentional bounce even when launchd's old private
    # session is ready. It must not re-enroll a deliberately stopped bot.
    native.calls.clear()
    bounced = call("bot", "restart", "worker")["data"]
    assert bounced["state"] == "running" and bounced["changed"] is True
    assert bounced["readiness"] == "bridge_ready"
    assert bounced["handoff"] == "attempted"
    assert native.calls.index("svc_bot_session_observe") < native.calls.index("svc_activation_handoff")
    assert native.calls.index("svc_activation_handoff") < native.calls.index("svc_activation_bot_fence")
    assert native.calls.index("svc_activation_bot_fence") < native.calls.index("svc_bot_enroll_exact")
    assert native.calls.index("svc_bot_enroll_exact") < native.calls.index("svc_activation_bot_ready")
    assert native.actions == ["start", "stop", "start", "stop", "start", "start"]
    assert call("bot", "stop", "worker")["data"]["state"] == "stopped"
    refused = call("bot", "restart", "worker", expected=4)
    assert refused["error"]["code"] == "conflict"
    assert native.actions == ["start", "stop", "start", "stop", "start", "start", "stop"]

    call("bot", "start", "worker")
    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "manager")
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    monkeypatch.setenv("FLEET_ROOT", str(root))
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/manager"))
    monkeypatch.setenv("CLAUDLOBBY_RELEASE_ID", release.release_id)
    manager_bounce = call("bot", "restart", "worker")["data"]
    assert manager_bounce["changed"] is True
    assert native.actions[-2:] == ["start", "start"]
    assert call("bot", "restart", "manager", expected=4)["error"]["code"] == "conflict"
    with monkeypatch.context() as patch:
        patch.setattr(bot_operations, "_schedule_self_restart", lambda **kwargs:
                      bot_operations.BotLifecycleResult("example", "manager", release.release_id,
                                                       "", "requested", False, "not_checked",
                                                       "captured", "request-1", "/private/startup.log"))
        before_self = len(native.actions)
        requested = call("bot", "restart", "manager")["data"]
        assert requested["state"] == "requested"
        assert requested["native_outcome"] == "unattempted"
        assert requested["runtime_state"] == "unknown"
        assert requested["request_id"] == "request-1"
        assert len(native.actions) == before_self

    native.handoff_rc = 3
    incomplete_handoff = call("bot", "restart", "worker")["data"]
    assert incomplete_handoff["handoff"] == "unavailable"
    assert incomplete_handoff["readiness"] == "bridge_ready"
    assert call("bot", "restart", "worker", "--ceiling", "0", expected=2)["error"]["code"] == "invalid_argument"
    before = len(native.actions)
    assert call("bot", "restart", "worker", "--ceiling", "37")["data"]["readiness"] == "bridge_ready"
    assert native.fence_args[-1][-1] == "37"
    assert len(native.actions) == before + 1

    # The generated weekly timer carries fleet/root scope, not BOT_ID. Run the
    # actual public CLI through selected admission and this recorded native
    # adapter; a shell CLI stub would miss the origin-binding regression.
    for key in ("FLEET_NAME", "BOT_ID", "BOT_DIR"):
        monkeypatch.delenv(key)
    monkeypatch.setenv("CLAUDLOBBY_FLEET", "example")
    monkeypatch.setenv("FLEET_ROOT", str(root))
    before = len(native.actions)
    timer_bounce = call("bot", "restart", "worker", "--ceiling", "37")["data"]
    assert timer_bounce["readiness"] == "bridge_ready"
    assert len(native.actions) == before + 1 and native.fence_args[-1][-1] == "37"
    native.readiness = "session-ready"
    assert call("bot", "restart", "worker")["data"]["readiness"] == "session_ready"
    native.readiness = "unexpected"
    assert call("bot", "restart", "worker", expected=6)["error"]["code"] == "unavailable"
    before_bad_scope = len(native.actions)
    monkeypatch.setenv("FLEET_ROOT", str(root / "other-fleet"))
    assert call("bot", "restart", "worker", expected=4)["error"]["code"] == "conflict"
    assert len(native.actions) == before_bad_scope

    # The admitted one-shot child uses the same selected lifecycle path but
    # consumes the handoff already written by its own pane. It must never send
    # another handoff keystroke to the pane it is about to terminate.
    worker_dir = root / "runtime/bots/worker"
    (worker_dir / ".claude").mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    (worker_dir / ".claude/session.md").write_text(
        f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
    monkeypatch.setenv("FLEET_ROOT", str(root))
    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "worker")
    monkeypatch.setenv("BOT_DIR", str(worker_dir))
    native.readiness = "bridge-ready"
    native.calls.clear()
    self_bounce = bot_operations.set_bot_running(root=root, fleet="example", bot="worker",
                                                 running=True, restart=True, _self_child=True)
    assert self_bounce.handoff == "captured" and self_bounce.readiness == "bridge_ready"
    assert "svc_activation_handoff" not in native.calls
    for key in ("FLEET_ROOT", "FLEET_NAME", "BOT_ID", "BOT_DIR"):
        monkeypatch.delenv(key)
    monkeypatch.setattr(bot_operations, "_selected_unit", selected_unit_original)
    monkeypatch.setattr(bot_operations, "_selected_adapter", selected_adapter_original)
    native.manager = "Linux"
    native.target = "com.example.worker.service"
    native.readiness = "bridge-ready"
    host.states[native.target] = "enabled loaded failed"
    before_failed = len(native.actions)
    recovered_failed = call("bot", "start", "worker")["data"]
    assert recovered_failed["state"] == "running" and recovered_failed["changed"] is True
    assert len(native.actions) == before_failed + 1
    host.states[native.target] = "enabled loaded failed"
    refused_restart = call("bot", "restart", "worker", expected=4)
    assert "failed" in refused_restart["error"]["message"]
    assert "bot stop" in refused_restart["error"]["message"]
    assert len(native.actions) == before_failed + 1


def test_a_staggered_unit_restarts_and_starts_within_its_boot_delay(cold, monkeypatch, capsys):  # noqa: F811
    """systemctl restart and enable --now block through ExecStartPre. A worker
    staggered 30 s returned at 32.9 s on the host, past the enroll call's fixed
    30 s, so a restart that worked read as an unverified effect (#2087)."""
    worker = _worker_cli(cold, monkeypatch, capsys, exec_start_pre="/bin/sleep 30")
    worker.native.enroll_needs_s = 33
    restarted = worker.call("bot", "restart", "worker")["data"]
    assert restarted["state"] == "running" and restarted["readiness"] == "bridge_ready"
    worker.host.states[worker.native.target] = "enabled loaded inactive"
    started = worker.call("bot", "start", "worker")["data"]
    assert started["state"] == "running" and started["changed"] is True
    assert worker.native.actions == ["start", "start"]


def test_a_hang_past_the_units_own_stagger_stays_unverified(cold, monkeypatch, capsys):  # noqa: F811
    """The budget grows by the unit's stagger and no more: a hang is still unknown."""
    worker = _worker_cli(cold, monkeypatch, capsys, exec_start_pre="/bin/sleep 30")
    worker.native.enroll_needs_s = 61
    hung = worker.call("bot", "restart", "worker", expected=6)
    assert hung["error"]["message"] == "bot lifecycle effect is unverified; inspect native state"
    assert hung["data"]["native_outcome"] == "unknown"


def test_an_unstaggered_unit_keeps_the_fixed_enroll_budget(cold, monkeypatch, capsys):  # noqa: F811
    worker = _worker_cli(cold, monkeypatch, capsys)
    worker.native.enroll_needs_s = 31
    slow = worker.call("bot", "restart", "worker", expected=6)
    assert slow["data"]["native_outcome"] == "unknown"


def test_a_changed_unit_is_refused_before_any_effect(cold, monkeypatch, capsys):  # noqa: F811
    """The budget reads only sealed bytes: activation admits a plain /bin/sleep
    stagger alone, and a unit changed since is refused before any native effect,
    so no enroll ever runs on a guessed delay."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    source = worker.root / "runtime/bots/worker/com.example.worker.service"
    source.write_bytes(source.read_bytes().replace(
        b"[Service]\n", b"[Service]\nExecStartPre=/bin/sh -c 'sleep 33'\n", 1))
    refused = worker.call("bot", "restart", "worker", expected=4)
    assert refused["error"]["code"] == "conflict"
    assert "svc_bot_enroll_exact" not in worker.native.calls and worker.native.actions == []
