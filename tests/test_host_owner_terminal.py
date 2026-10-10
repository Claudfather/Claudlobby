"""Real child CLI / controlling terminal; all state and principals are synthetic."""

import errno
import os
import select
import signal
import sys
import time

import pytest

from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.owner_access import AccessDenied, OwnerAccess, PrincipalRef
from tests.conftest import constructed_env


def _command(root, action, dialogue):
    import pty

    argv = [sys.executable, "-m", "claudlobby", "--root", str(root), "host", "owner", action]
    env = constructed_env()
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(sys.executable, argv, env)
    output = bytearray()
    pending = list(dialogue)
    status = None
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if pending and pending[0][0].encode() in output:
                    _, answer = pending.pop(0)
                    os.write(fd, (answer + "\n").encode())
            done, status_value = os.waitpid(pid, os.WNOHANG)
            if done:
                status = status_value
                break
        while status is None and time.monotonic() < deadline:
            done, status_value = os.waitpid(pid, os.WNOHANG)
            if done:
                status = status_value
            else:
                time.sleep(0.01)
        assert status is not None, "owner terminal command did not finish: " + output.decode(errors="replace")
        assert not pending, "owner terminal command missed prompts: " + output.decode(errors="replace")
        return os.waitstatus_to_exitcode(status), output.decode(errors="replace")
    finally:
        os.close(fd)
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="owner local console requires a POSIX terminal")
def test_initialize_pair_revoke_through_actual_cli_terminal(tmp_path):
    ensure_host_uid(tmp_path / "state")
    store = OwnerAccess(tmp_path)
    code, output = _command(tmp_path, "initialize", [("Type INITIALIZE", "INITIALIZE")])
    assert code == 0, output
    assert store.current_grant() is None
    principal = PrincipalRef("test-verifier", "human-001")
    challenge = store.begin_pairing(principal)
    code, output = _command(tmp_path, "confirm", [
        ("Paste the browser pairing code (hidden):", challenge.token),
        ("Type PAIR", "PAIR"),
    ])
    assert code == 0, output
    assert principal.subject in output and challenge.token not in output
    assert store.current_grant().principal == principal
    session = store.open_session(principal)
    code, output = _command(tmp_path, "revoke", [("Type REVOKE", "REVOKE")])
    assert code == 0, output
    assert not store.current_grant().active
    with pytest.raises(AccessDenied):
        store.authorize_read(session.token, principal, host_uid=session.grant.host_uid)


@pytest.mark.skipif(os.name != "posix", reason="owner local console requires a POSIX terminal")
def test_attest_existing_source_through_actual_cli_terminal(tmp_path):
    from claudlobby.plane.owner_source import inspect_source
    from tests.test_plane_two_fleets import _seed

    _seed(tmp_path)
    OwnerAccess.initialize(tmp_path)
    code, output = _command(tmp_path, "bind-source", [("Type BIND", "BIND")])
    assert code == 0, output
    assert "including imported records" in output
    inspect_source(tmp_path)
