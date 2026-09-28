"""One native call, explicit targets and honest outcomes; no tmux/services."""

from dataclasses import replace
import io
import shlex
import signal
import subprocess

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
    assert captured.read_bytes().split(b"\0") == [destination.socket.encode(), b"=worker", body.encode(), b""]
    assert not forbidden.exists()


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
    assert result.status == "unknown" and kills == [(process.pid, signal.SIGKILL)]
    assert waits == [{"input": b"hi", "timeout": 2}, {"timeout": 1}]
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
