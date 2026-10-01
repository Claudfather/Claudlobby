"""Stale CLI and interrupted activation refuse before a mutation starts."""

from pathlib import Path
import os
import selectors
import shlex
import signal
import socket
import subprocess
import sys
import time

import pytest

from claudlobby import activation_state as a
from claudlobby.releases import read_release
from claudlobby.runtime_admission import (
    ReleaseMismatch, RuntimeIdentity, WatchdogActivationPause, activation_start, admit_native, mutation_admission,
)
from tests.test_activation_state import _advance, _prepare
from tests.test_config_plan import proposal
from tests.test_releases import installed


@pytest.fixture
def tmp_path(tmp_path_factory):
    # AF_UNIX has a short platform limit; keep owned data paths compact.
    return tmp_path_factory.mktemp("admit")


def _active(plan):
    with a.locked_activation(plan.data_root) as store:
        _prepare(store, plan)
        _advance(store, "candidate", a.STEPS[:a.STEPS.index("selection_switched")])
        store.begin("candidate", "selection_switched")
        store.select("candidate")
        _advance(store, "candidate", a.STEPS[a.STEPS.index("selection_switched") + 1:])


def _identity(builder):
    release = read_release(builder.root, builder.release_id)
    return RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)


def test_stale_context_and_executable_refuse_before_effect(proposal):
    builder, _, _ = proposal
    plan = builder.seal()
    _active(plan)
    identity = _identity(builder)
    with pytest.raises(ReleaseMismatch, match="no mutation performed") as err:
        with mutation_admission(builder.root, identity=identity, expected_release="r-stale"):
            pytest.fail("stale bot configuration admitted")
    assert err.value.exit_code == 7 and str(builder.root) in err.value.hint
    with pytest.raises(ReleaseMismatch):
        with mutation_admission(builder.root, identity=RuntimeIdentity(
                Path("/stale/bin/claudlobby"), identity.native, identity.artifact_id)):
            pytest.fail("same artifact from wrong executable admitted")
    with mutation_admission(builder.root, identity=identity) as admitted:
        assert admitted.release_id == plan.release_id
        with pytest.raises(a.ActivationError, match="holds the lock"):
            with a.locked_activation(builder.root):
                pytest.fail("activation raced an admitted operation")


def test_incomplete_activation_blocks_even_before_selection_changes(proposal):
    builder, _, _ = proposal
    plan = builder.seal()
    _active(plan)
    identity = _identity(builder)
    with a.locked_activation(builder.root) as store:
        store.assert_locked()
        with pytest.raises(a.ActivationError, match="activation is running"):
            with mutation_admission(builder.root, identity=identity):
                pytest.fail("exclusive cutover lock ignored")
        _prepare(store, plan, "next")
    with pytest.raises(a.ActivationError, match="unfinished activation next"):
        with mutation_admission(builder.root, identity=identity):
            pytest.fail("old selected release admitted during interrupted preparation")
    with pytest.raises(a.ActivationError, match="lock is not held"):
        store.assert_locked()


def test_missing_host_state_never_creates_an_install(tmp_path):
    root = tmp_path / "not-an-install"
    with pytest.raises(a.ActivationError, match="lock unavailable"):
        with mutation_admission(root):
            pytest.fail("missing install admitted")
    assert not root.exists()


def test_native_watchdog_pause_has_distinct_exit(monkeypatch, capsys):
    from claudlobby import runtime_admission as admission

    monkeypatch.setattr(admission.RuntimeIdentity, "current", classmethod(
        lambda cls: admission.RuntimeIdentity(Path("/cli"), Path("/native"), "artifact")))
    def paused(*_args, **_kwargs):
        raise WatchdogActivationPause("host activation is running; watchdog remains paused")
    monkeypatch.setattr(admission, "admit_native", paused)
    monkeypatch.setattr(sys, "argv", ["runtime_admission", "acquire", "9", "123", "/root",
                                     "keepalive", "/root/bot", "r-test", "/cli", "/native", "artifact"])
    assert admission._native_main() == 75
    assert "watchdog remains paused" in capsys.readouterr().err


def _starting(store, plan, operation="start-bot"):
    _prepare(store, plan)
    _advance(store, "candidate", a.STEPS[:a.STEPS.index("selection_switched")])
    store.begin("candidate", "selection_switched")
    store.select("candidate")
    step = "bots_started" if operation == "start-bot" else "ingest_started"
    _advance(store, "candidate", a.STEPS[a.STEPS.index("selection_switched") + 1:a.STEPS.index(step)])
    store.begin("candidate", step)


def test_inherited_native_lock_spans_shell_work_and_normal_cleanup(proposal, tmp_path):
    builder, _, settings = proposal
    plan = builder.seal()
    _active(plan)
    identity = _identity(builder)
    # Explicit fixture identity in a child interpreter; exercise the real lock
    # owner and native EXIT cleanup without starting a bot or installing Python.
    driver = tmp_path / "admission.py"
    driver.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from claudlobby import runtime_admission as r\n"
        f"r.RuntimeIdentity.current = classmethod(lambda cls: r.RuntimeIdentity("
        f"Path({str(identity.cli)!r}), Path({str(identity.native)!r}), {identity.artifact_id!r}))\n"
        "sys.argv = sys.argv[sys.argv.index('claudlobby.runtime_admission'):]\n"
        "raise SystemExit(r._native_main())\n"
    )
    guard = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/runtime-admission.sh"
    script = f"""
set -eu
. {shlex.quote(str(guard))}
admission_python() {{ {shlex.quote(sys.executable)} -B {shlex.quote(str(driver))} "$@"; }}
_NATIVE_ADMISSION_PYTHON=admission_python
_NATIVE_ADMISSION_ROOT={shlex.quote(str(builder.root))}
_NATIVE_ADMISSION_PID=$$
_NATIVE_ADMISSION_SUBSHELL=$BASH_SUBSHELL
exec 9<"$_NATIVE_ADMISSION_ROOT/state/activation.lock"
"$_NATIVE_ADMISSION_PYTHON" -I -B -m claudlobby.runtime_admission acquire 9 $$ \
 "$_NATIVE_ADMISSION_ROOT" start-bot {shlex.quote(str(settings.parent))} \
 {shlex.quote(plan.release_id)} {shlex.quote(str(identity.cli))} \
 {shlex.quote(str(identity.native))} {shlex.quote(identity.artifact_id)}
trap native_admission_cleanup EXIT
(native_admission_cleanup) # a subshell cannot release the parent's lease
printf 'admitted\\n'
read -r finish
sleep 1 & # explicit unlock must also release copies in detached children
"""
    process = subprocess.Popen(["/bin/bash", "-c", script], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=5), "native admission child did not answer within 5 seconds"
        assert process.stdout.readline().strip() == "admitted"
        with pytest.raises(a.ActivationError, match="holds the lock"):
            with a.locked_activation(builder.root):
                pytest.fail("shell work lost its admission after the Python child exited")
        process.stdin.write("finish\n")
        process.stdin.flush()
        assert process.wait(timeout=5) == 0, process.stderr.read()
        with a.locked_activation(builder.root):
            pass
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_coordinator_scope_admits_one_exact_start_never_watchdog(proposal):
    builder, _, settings = proposal
    plan = builder.seal()
    identity = _identity(builder)
    other = settings.parent.with_name("other")
    other.mkdir()
    with a.locked_activation(builder.root) as store:
        _starting(store, plan)
        fd = os.open(builder.root / "state/activation.lock", os.O_RDONLY)
        try:
            def enter(operation="start-bot", bot_dir=settings.parent, selected=plan.release_id):
                return admit_native(builder.root, fd, owner_pid=os.getppid(), operation=operation,
                                    bot_dir=bot_dir, expected_release=selected, identity=identity,
                                    timeout=0.2)
            with activation_start(store, "candidate", operation="start-bot",
                                  bot_dir=settings.parent, identity=identity):
                with pytest.raises(WatchdogActivationPause, match="watchdog remains paused"):
                    enter("keepalive")
                with pytest.raises(a.ActivationError, match="does not admit"):
                    enter(bot_dir=other)
                with pytest.raises(a.ActivationError, match="does not admit"):
                    enter(selected="r-stale")
                assert enter() == "activation"
                with pytest.raises(a.ActivationError, match="authorization unavailable"):
                    enter()  # a manager retry needs another explicitly armed scope
            assert not (builder.root / "state/activation-start.sock").exists()
            with pytest.raises(a.ActivationError, match="authorization unavailable"):
                enter()
        finally:
            os.close(fd)
    with pytest.raises(a.ActivationError, match="incomplete"):
        with mutation_admission(builder.root, identity=identity):
            pytest.fail("one admitted verification start made the host active")


def test_stalled_or_dead_start_responder_cannot_license_effects(proposal):
    builder, _, settings = proposal
    plan = builder.seal()
    identity = _identity(builder)
    effects = []
    with a.locked_activation(builder.root) as store:
        _starting(store, plan, "ingest-start")
        endpoint = builder.root / "state/activation-start.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(endpoint))
        listener.listen(1)
        fd = os.open(builder.root / "state/activation.lock", os.O_RDONLY)
        try:
            started = time.monotonic()
            with pytest.raises(a.ActivationError, match="authorization unavailable"):
                admit_native(builder.root, fd, owner_pid=os.getppid(), operation="ingest-start",
                             bot_dir=None, expected_release=plan.release_id, identity=identity,
                             timeout=0.05)
                effects.append("ingest")
            assert time.monotonic() - started < 1
            listener.close()  # dead socket remains: no live coordinator proof
            with pytest.raises(a.ActivationError, match="authorization unavailable"):
                admit_native(builder.root, fd, owner_pid=os.getppid(), operation="ingest-start",
                             bot_dir=None, expected_release=plan.release_id, identity=identity)
                effects.append("ingest")
            with activation_start(store, "candidate", operation="ingest-start", identity=identity):
                assert admit_native(builder.root, fd, owner_pid=os.getppid(), operation="ingest-start",
                                    bot_dir=None, expected_release=plan.release_id,
                                    identity=identity) == "activation"
        finally:
            listener.close()
            os.close(fd)
    assert effects == []
