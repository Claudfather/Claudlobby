"""One native call, explicit targets and honest outcomes; no tmux/services."""

from dataclasses import replace
import io
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import time
from tempfile import TemporaryDirectory

import pytest

from claudlobby import message_transport as transport
from tests.package_fixtures import source_package


MSG = "msg_" + "a" * 32
HASH = "sha256:" + "b" * 64


@pytest.fixture
def destination(tmp_path):
    root, sockets = tmp_path / "data", tmp_path / "tmux"
    root.mkdir()
    sockets.mkdir()
    return transport.TransportDestination(root, "fleet", "com.test.worker", "worker", sockets)


def test_one_call_preserves_stdin_and_rejects_ambient_shell_and_trace_controls(destination, monkeypatch):
    hostile = {"BASH_ENV": "/tmp/evil", "ENV": "/tmp/evil", "SHELLOPTS": "xtrace",
               "BASH_FUNC_bot_tmux_send%%": "() { exit 0; }", "TMUX_BIN": "/tmp/evil",
               "PATH": "/tmp/evil", "PLANE_MSG_ID": "msg_" + "c" * 32,
               "PLANE_WIRE_OUT": "/tmp/leaked-proof", "PANE_VERIFY_TRACE": "/tmp/leaked-body",
               "PANE_SEND_VERIFY_TICKS": "99", "PANE_SEND_CHUNK_BYTES": "0",
               "TMUX_TMPDIR": "/tmp/foreign-server", "PLANE_EMIT_DISABLED": "0"}
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)
    body = 'quotes "\' $HOME $(touch forbidden) `false` ;\n\t\x1b[31m résumé\n'
    calls = []
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0,
            f"transport-v1\tinvoked\ntransport-v1\tresult\t0\t{HASH}\t42\n".encode(), b"")
    result = transport.send(source_package(), destination, message_id=MSG, body=body, runner=runner)
    assert result == transport.TransportOutcome("submitted", HASH, 42, 0)
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:4] == ["/bin/bash", "--noprofile", "--norc", "-c"]
    assert command[-3:] == [str(source_package().native / "lib-common.sh"), destination.socket, destination.session]
    assert body not in " ".join(command) and kwargs["input"] == body.encode()
    env = kwargs["env"]
    assert set(env) == {"PATH", "LC_ALL", "CLAUDLOBBY_ROOT", "FLEET_NAME", "TMUX_TMPDIR",
                        "TMPDIR", "PLANE_MSG_ID", "PLANE_EMIT_DISABLED", "PANE_SEND_VERIFY_TICKS"}
    assert (env["PLANE_MSG_ID"], env["PLANE_EMIT_DISABLED"], env["PANE_SEND_VERIFY_TICKS"]) == (MSG, "1", "0")
    assert env["TMUX_TMPDIR"] == env["TMPDIR"] == str(destination.tmux_tmpdir)


def test_private_bash_wrapper_passes_hostile_text_as_data_once(destination, tmp_path):
    # Test collaborator replaces only bot_tmux_send, not its chunk algorithm.
    # Bash itself executes the production wrapper; no native tmux is invoked.
    native = tmp_path / "native ' quote"
    native.mkdir()
    captured = tmp_path / "captured"
    owned = shlex.quote(str(captured))
    (native / "lib-common.sh").write_text(
        "_lc_cleanup() { :; }\n"
        "bot_tmux_send() {\n"
        f"  printf '%s\\0' \"$@\" >> {owned}\n"
        f"  PLANE_WIRE_SHA256={HASH}; PLANE_WIRE_BYTES=42\n"
        "}\n")
    forbidden = tmp_path / "must-not-exist"
    body = f'$(touch {shlex.quote(str(forbidden))}) `false` " ;\n\t\x1b[31m résumé\n\n'
    result = transport.send(replace(source_package(), native=native), destination,
                            message_id=MSG, body=body, timeout=5)
    assert result.status == "submitted"
    assert captured.read_bytes().split(b"\0") == [destination.socket.encode(), b"=worker:", body.encode(), b""]
    assert not forbidden.exists()


def test_real_native_send_uses_exact_session_and_pane_target():
    tmux = shutil.which("tmux")
    assert tmux, "native transport requires tmux"
    # A short, private socket path stays under macOS's Unix-socket path limit.
    with TemporaryDirectory(prefix="cl-msg-", dir="/tmp") as scratch:
        root = Path(scratch)
        sockets = root / "s"
        home = root / "h"
        sockets.mkdir()
        home.mkdir()
        env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
               "HOME": str(home), "TMUX_TMPDIR": str(sockets), "TMPDIR": str(sockets)}
        socket = "private-worker"
        subprocess.run([tmux, "-L", socket, "-f", "/dev/null", "new-session",
                        "-d", "-s", "worker-extra", "cat"], env=env, check=True)
        try:
            # The worker draws an input box that shows what is typed: the send
            # presses Enter only once the box shows the payload (#1236), so a
            # bare `cat` pane would correctly never be submitted to.
            box = Path(__file__).resolve().parent / "fixtures" / "input-box-stub.py"
            subprocess.run([tmux, "-L", socket, "new-session", "-d", "-s", "worker",
                            f"python3 {shlex.quote(str(box))}"], env=env, check=True)
            destination = transport.TransportDestination(root, "fleet", socket, "worker", sockets)
            package = replace(source_package(), native=Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts")
            body = "X" * 1450 + "END_OF_PRIVATE_MESSAGE"
            result = transport.send(package, destination, message_id=MSG, body=body, timeout=10)
            assert result.status == "submitted" and result.native_returncode == 0, result
            assert result.wire_sha256 and result.wire_bytes == len(body)
            worker = subprocess.run([tmux, "-L", socket, "capture-pane", "-t", "worker",
                                     "-p", "-S", "-"], env=env, capture_output=True, text=True, check=True)
            other = subprocess.run([tmux, "-L", socket, "capture-pane", "-t", "worker-extra",
                                    "-p", "-S", "-"], env=env, capture_output=True, text=True, check=True)
            assert "XXXX" in worker.stdout and "XXXX" not in other.stdout
        finally:
            subprocess.run([tmux, "-L", socket, "kill-server"], env=env,
                           capture_output=True, timeout=5)


def test_real_native_refuses_existing_input_without_typing_or_enter():
    tmux = shutil.which("tmux")
    assert tmux, "native transport requires tmux"
    with TemporaryDirectory(prefix="cl-held-", dir="/tmp") as scratch:
        root = Path(scratch)
        sockets, home = root / "s", root / "h"
        sockets.mkdir(); home.mkdir()
        env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
               "HOME": str(home), "TMUX_TMPDIR": str(sockets), "TMPDIR": str(sockets)}
        box = Path(__file__).resolve().parent / "fixtures" / "input-box-stub.py"
        log = root / "submissions"
        command = [tmux, "-L", "private-held", "-f", "/dev/null"]
        def native(*args):
            return subprocess.run([*command, *args], env=env, capture_output=True,
                                  text=True, timeout=5, check=True)
        try:
            native("new-session", "-d", "-s", "worker",
                   f"python3 {shlex.quote(str(box))} --chrome --log {shlex.quote(str(log))}")
            def wait_for(text):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    pane = native("capture-pane", "-t", "worker", "-p").stdout
                    if text in pane:
                        return pane
                    time.sleep(0.02)
                pytest.fail("private input fixture did not render expected text")
            wait_for(">\n")
            original = "STRANDED_PRIVATE_INPUT"
            native("send-keys", "-t", "worker", "-l", "--", original)
            before = wait_for(original)
            destination = transport.TransportDestination(root, "fleet", "private-held", "worker", sockets)
            package = replace(source_package(), native=Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts")
            result = transport.send(package, destination, message_id=MSG,
                                    body="NEW_PAYLOAD_MUST_NOT_APPEND", timeout=10)
            assert result.status == "failed" and result.native_returncode == 4, result
            assert result.wire_sha256 is result.wire_bytes is None
            assert native("capture-pane", "-t", "worker", "-p").stdout == before
            assert not log.exists()  # No CR submitted either the old or new text.
        finally:
            subprocess.run([*command, "kill-server"], env=env, capture_output=True, timeout=5)


@pytest.mark.parametrize("marker,reported,actual,expected", [
    (b"pane_send: recipient-input-held; no payload or Enter was sent\n", 4, 4, "failed"),
    (b"unclassified failure", 4, 4, "unknown"),
    (b"pane_send: recipient-input-held; no payload or Enter was sent\n", 4, 1, "unknown"),
])
def test_held_refusal_requires_coherent_native_proof(destination, marker, reported, actual, expected):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, actual,
            f"transport-v1\tinvoked\ntransport-v1\tresult\t{reported}\t{HASH}\t42\n".encode(), marker)
    result = transport.send(source_package(), destination, message_id=MSG, body="new", runner=runner)
    assert result.status == expected and len(calls) == 1
    if expected == "failed":
        assert result.wire_sha256 is result.wire_bytes is None


@pytest.mark.parametrize(("field", "value"), [("socket", "../foreign"), ("socket", ""),
                                                ("session", "worker:0"), ("session", "*")])
def test_nonliteral_targets_refuse_before_launch(destination, field, value):
    with pytest.raises(ValueError, match="literal"):
        replace(destination, **{field: value})


def test_prelaunch_refusal_differs_from_partial_and_timeout_uncertainty(destination):
    package = source_package()
    def must_not_run(*args, **kwargs):
        pytest.fail("invalid input reached the native runner")
    for message_id, body in ((MSG + "\n", "hi"), (MSG, "has\0null")):
        with pytest.raises(ValueError):
            transport.send(package, destination, message_id=message_id, body=body, runner=must_not_run)
    missing = transport.send(replace(package, native=destination.root / "missing"), destination,
                             message_id=MSG, body="hi", runner=must_not_run)
    assert missing.status == "failed"
    def failed_launch(*args, **kwargs):
        raise OSError("not executable")
    assert transport.send(package, destination, message_id=MSG, body="hi", runner=failed_launch).status == "failed"
    calls = []
    def partial(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1,
            f"transport-v1\tinvoked\ntransport-v1\tresult\t1\t{HASH}\t42\n".encode(), b"partial chunk")
    result = transport.send(package, destination, message_id=MSG, body="hi", runner=partial)
    assert result.status == "unknown" and result.wire_sha256 == HASH
    assert len(calls) == 1  # A failed chunk never causes a second payload send.
    def no_session(command, **kwargs):
        return subprocess.CompletedProcess(command, 1,
            b"transport-v1\tinvoked\ntransport-v1\tresult\t1\t\t\n",
            b"bot_tmux_send: session 'worker' not found on socket 'private'"
            b" - send dropped (logged)\n")
    missing_session = transport.send(package, destination, message_id=MSG, body="hi",
                                     runner=no_session)
    assert missing_session.status == "failed" and missing_session.wire_sha256 is None
    def timed_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"transport-v1\tinvoked\n")
    result = transport.send(package, destination, message_id=MSG, body="hi", runner=timed_out)
    assert result.status == "unknown" and result.wire_sha256 is result.wire_bytes is None


def test_timeout_kills_only_owned_group_with_a_bounded_cleanup(destination, monkeypatch):
    class Process:
        pid = 912345
        stdin, stdout, stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
        def communicate(self, **kwargs):
            waits.append(kwargs)
            raise subprocess.TimeoutExpired("native", kwargs["timeout"], output=b"transport-v1\tinvoked\n")
        def wait(self, *, timeout):
            assert timeout == 1
    waits, kills = [], []
    process = Process()
    def popen(command, **kwargs):
        assert kwargs["start_new_session"] is True and kwargs["stdin"] == subprocess.PIPE
        return process
    monkeypatch.setattr(transport.subprocess, "Popen", popen)
    monkeypatch.setattr(transport.os, "killpg", lambda pid, sig: kills.append((pid, sig)))
    result = transport.send(source_package(), destination, message_id=MSG, body="hi", timeout=2)
    assert result.status == "unknown"
    assert kills == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]
    assert waits == [{"input": b"hi", "timeout": 2}, {"timeout": 1}, {"timeout": 1}]
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


def test_timeout_allows_shell_exit_cleanup(tmp_path):
    temporary = tmp_path / "credential-config"
    command = ["/bin/bash", "--noprofile", "--norc", "-c",
               'trap \'rm -f -- "$1"\' EXIT; : > "$1"; sleep 30', "_", str(temporary)]
    with pytest.raises(subprocess.TimeoutExpired):
        transport._run(command, input=b"", env={"PATH": "/usr/bin:/bin"}, timeout=0.5)
    assert not temporary.exists()
