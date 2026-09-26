"""Exercise the harness's actual manager and Plane fixture setup in isolation."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import tempfile
import time
import uuid

import pytest

from tests.conftest import constructed_env, read_fleet_events

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "lib" / "validate-bot-change.sh"


def _between(start, end):
    source = HARNESS.read_text()
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


def _helpers():
    return _between("# --- Harness-only diagnostics and owned foreign-tree cleanup",
                    "# --- End harness-only helpers")


def _run(body, env, cwd):
    return subprocess.run(["/bin/bash", "-c", "set -euo pipefail\n" + body],
                          cwd=cwd, env=env, text=True, capture_output=True, timeout=40)


def _tmux(env, socket, *args):
    return subprocess.run(["tmux", "-L", socket, "-f", "/dev/null", *args],
                          env=env, text=True, capture_output=True, timeout=10)


def _send_notifications(env, socket, prefix):
    # Each line is short; the repeated stream exceeds both platforms' PTY
    # input capacity when the manager never reads. Active readers drain it.
    result = _run('''
for n in $(seq 1 128); do
    text="$PREFIX-$n-abcdefghijklmnopqrstuvwxyz-abcdefghijklmnopqrstuvwxyz-abcdefghijklmnopqrstuvwxyz"
    tmux -L "$SOCKET" send-keys -t manager -l -- "$text"
    tmux -L "$SOCKET" send-keys -t manager Enter
    sleep 0.01
done
''', dict(env, SOCKET=socket, PREFIX=prefix), env["HOME"])
    assert result.returncode == 0, result.stderr
    return f"{prefix}-128-"


def _visible(env, socket):
    result = _tmux(env, socket, "capture-pane", "-t", "manager", "-p", "-J")
    assert result.returncode == 0, result.stderr
    return result.stdout


def _require_pane_command(env, socket, expected):
    deadline = time.monotonic() + 2
    observed = ""
    while time.monotonic() < deadline:
        result = _tmux(env, socket, "display-message", "-t", "manager", "-p",
                       "#{pane_pid} #{pane_current_command}")
        observed = result.stdout.strip()
        if result.returncode == 0:
            pid, command = observed.split(" ", 1)
            if command == expected:
                assert int(pid) > 1
                return int(pid)
        time.sleep(0.05)
    raise AssertionError(f"owned manager pane did not become {expected}: {observed}")


@pytest.mark.parametrize("scenario", ["initial", "briefing-fallback"])
def test_actual_manager_fixture_drains_notifications_and_nonreader_control(tmp_path, scenario):
    home = tmp_path / "home"
    home.mkdir()
    socket = "observation-" + uuid.uuid4().hex
    # A short, uniquely owned namespace avoids macOS sockaddr path limits.
    with tempfile.TemporaryDirectory(prefix="vbc-observe-", dir="/tmp") as namespace:
        env = constructed_env(HOME=home, TMPDIR=tmp_path, CLAUDLOBBY_ROOT=tmp_path,
                              TMUX_TMPDIR=namespace, SOCKET=socket)
        # Exercise the actual initial setup and briefing fallback call sites.
        # The latter must recreate the same draining reader if it has exited.
        if scenario == "initial":
            setup = _between('# --- Run: stand up a non-idle worker pane',
                             'tmux new-session -d -s "$BOT"')
        else:
            setup = _between('tmux has-session -t "$MGR"', '\n') + '\n'
        body = _helpers() + '''
MGR=manager
tmux() { command tmux -L "$SOCKET" -f /dev/null "$@"; }
''' + setup
        try:
            result = _run(body, env, tmp_path)
            assert result.returncode == 0, result.stderr
            prefix = "active-" + uuid.uuid4().hex
            last = _send_notifications(env, socket, prefix)
            assert last in _visible(env, socket), "actual manager fixture stopped receiving notifications"
            reader_pid = _require_pane_command(env, socket, "cat")
            if scenario == "briefing-fallback":
                # An existing manager is left running, without replacement or
                # losing the notification already observed in its transcript.
                present = _run(body, env, tmp_path)
                assert present.returncode == 0, present.stderr
                assert _require_pane_command(env, socket, "cat") == reader_pid
                assert last in _visible(env, socket)

            # Replace only this private pane with the original read-nothing
            # fixture. SIGSTOP is not a valid control: the observed pane did
            # not stay stopped. No process-table target selection is needed.
            mutant = _tmux(env, socket, "respawn-pane", "-k", "-t", "manager", "exec sleep 600")
            assert mutant.returncode == 0, mutant.stderr
            _require_pane_command(env, socket, "sleep")
            absent = _send_notifications(env, socket, "nonreader-" + uuid.uuid4().hex)
            assert absent not in _visible(env, socket), "nonreader control did not saturate the PTY"

            # Restore through the actual harness call site, not a second
            # implementation of its reader, then require a new visible tail.
            killed = _tmux(env, socket, "kill-session", "-t", "manager")
            assert killed.returncode == 0, killed.stderr
            restored_setup = _run(body, env, tmp_path)
            assert restored_setup.returncode == 0, restored_setup.stderr
            _require_pane_command(env, socket, "cat")
            restored = _send_notifications(env, socket, "restored-" + uuid.uuid4().hex)
            assert restored in _visible(env, socket), "restoring the reader did not restore delivery"
        finally:
            result = _tmux(env, socket, "kill-server")
            assert result.returncode in (0, 1), result.stderr


def test_actual_plane_fixture_records_cold_dispatch_and_discloses_fallback(tmp_path, scratch_plane_env):
    root = tmp_path / "root"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    socket = scratch_plane_env.socket_dir() / "plane.sock"
    env = constructed_env(HOME=home, TMPDIR=temporary,
                          **scratch_plane_env(root, socket=socket))
    env.update(PL_ROOT=str(root), PL_REPO=str(REPO), PL_CLI=env["PLANE_EMIT_CLI"],
               PL_SOCK=str(socket))
    setup = _between('    PL_LIB="$PL_ROOT/lib"', '    "$PL_CLI" --root "$PL_ROOT" plane serve')
    dispatch = _between('    _pl_dispatch() {', '    _pl_count()')
    # Reproduce the complete harness's actual outer unit-style selector.
    # A clean env with only FLEET_NAME misses its precedence over that name
    # inside the real dispatch door's fleet resolvers.
    outer_fleet = (_between('\nFLEET=', '\nBOT=')
                   + _between('\nexport CLAUDLOBBY_FLEET=', '\n\n') + '\n')
    dispatch = outer_fleet + dispatch
    result = _run(_helpers() + setup, env, tmp_path)
    assert result.returncode == 0, result.stderr
    env["PL_LIB"] = str(root / "lib")
    daemon = subprocess.Popen([env["PL_CLI"], "--root", str(root), "plane", "serve", "--socket", str(socket)],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not socket.exists() and time.monotonic() < deadline:
            assert daemon.poll() is None, daemon.stderr.read()
            time.sleep(0.05)
        assert socket.exists(), "scratch daemon did not become ready"
        first = _run(_helpers() + dispatch + '\n_pl_dispatch "" "leg one: live daemon"\n', env, tmp_path)
        assert first.returncode == 0, (first.stdout, first.stderr)
        assert "falling back" not in (root / "err").read_text()
        daemon.terminate()
        daemon.wait(timeout=10)
        assert not (root / "state/plane/.socket-wedged").exists(), "live daemon unexpectedly wedged"
        second = _run(_helpers() + dispatch + '\nPL_LEG=1\n_pl_dispatch "PLANE_EMIT_ENABLED=0" "leg two: daemon down"\n', env, tmp_path)
        stderr = (root / "err").read_text()
        events = [json.loads(line) for line in read_fleet_events(root).splitlines()]
        errors = [event for event in events if event["type"] == "script_error"]
        with sqlite3.connect(root / "state/plane/plane.db") as db:
            bodies = [row[0] for row in db.execute("SELECT body FROM communications ORDER BY ingest_seq")]
        assert second.returncode == 0, (second.stdout, second.stderr, errors)
        assert len(bodies) == 2 and "leg two: daemon down" in bodies[1], (bodies, stderr, errors)
        assert not errors, ("fixture preflight emitted script_error before the dispatch", errors, stderr)
        assert "falling back" in stderr, ("cold dispatch did not disclose the transport fallback", stderr, events)
        assert "can't open file" not in stderr, stderr
    finally:
        if daemon.poll() is None:
            daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # This Popen is the owned scratch daemon. Reap it even when TERM
            # fails, while keeping the timeout fatal rather than calling it a
            # successful graceful cleanup.
            daemon.kill()
            daemon.wait(timeout=10)
            raise
        finally:
            daemon.stderr.close()
