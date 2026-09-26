"""Version deadlines without GNU timeout; only private synthetic processes run."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def probe(tmp_path):
    bins, home, temp, root = [tmp_path / p for p in ("bin", "home", "tmp", "root")]
    for path in (bins, home, temp, root):
        path.mkdir()
    for name, target in {"bash": "/bin/bash", "uname": "/usr/bin/uname",
                         "dirname": "/usr/bin/dirname", "mktemp": "/usr/bin/mktemp",
                         "rm": "/bin/rm"}.items():
        (bins / name).symlink_to(target)
    (bins / "python3").symlink_to(sys.executable)
    calls = tmp_path / "calls"
    groups = tmp_path / "groups"
    finished = tmp_path / "finished"
    env = {"PATH": str(bins), "HOME": str(home), "TMPDIR": str(temp),
           "CLAUDLOBBY_ROOT": str(root), "PLANE_EMIT_DISABLED": "1",
           "PLANE_SOCKET": str(tmp_path / "absent.sock"),
           "TELEGRAM_STATE_DIR": str(tmp_path / "channel"),
           "XDG_CONFIG_HOME": str(home / ".config"),
           "CALLS": str(calls), "GROUPS": str(groups),
           "FINISHED": str(finished)}
    binary = tmp_path / "fake-claude"

    def write(body):
        binary.write_text(
            f"#!{sys.executable}\nimport os, sys, time, signal, subprocess\n"
            "from pathlib import Path\n"
            "with open(os.environ['CALLS'], 'a') as f: f.write('called\\n')\n"
            "with open(os.environ['GROUPS'], 'a') as f: f.write(str(os.getpgrp())+'\\n')\n"
            + body + "\n")
        binary.chmod(0o755)

    def run(body=None, *, seconds="3", code=None, python=True, settle=0):
        if body is not None:
            write(body)
        if not python:
            (bins / "python3").unlink()
        child_env = {**env, "CLAUDE_VERSION_TIMEOUT_S": seconds}
        argv = ["/bin/bash", str(REPO / "lib/claude-version.sh"), str(binary)]
        if code is not None:
            argv = ["/bin/bash", "-c", f'. "{REPO}/lib/lib-common.sh"; set +e; {code}']
        started = time.monotonic()
        proc = subprocess.Popen(argv, env=child_env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
        outer_timeout = False
        try:
            try:
                # Every synthetic child is finite (at most 1.5s per probe).
                # Leave room for the unfixed parent's two probes to finish;
                # its late return must be a behavioral failure, not cleanup.
                stdout, stderr = proc.communicate(timeout=6)
            except subprocess.TimeoutExpired:
                outer_timeout = True
                # Only the outer group we created and groups recorded by this
                # private executable. Never a process-name/shared-uid search.
                kill_owned(proc)
                stdout, stderr = proc.communicate(timeout=2)
            elapsed = time.monotonic() - started
            # Observe private delayed receipts BEFORE emergency cleanup.
            # Otherwise cleanup could hide a production watchdog's leak.
            time.sleep(settle)
            return SimpleNamespace(returncode=proc.returncode, stdout=stdout, stderr=stderr,
                                   elapsed=elapsed, outer_timeout=outer_timeout,
                                   late_finish_before_cleanup=finished.exists())
        finally:
            kill_owned(proc)
            proc.wait(timeout=2)

    def kill_owned(proc):
        # Do not signal a group whose leader we already reaped. On Darwin a
        # redundant signal can return EPERM rather than ESRCH. Our descendants
        # are finite; the delayed receipt checks observe them before cleanup.
        if proc.poll() is None:
            assert proc.pid > 1 and proc.pid != os.getpgrp()
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)

    return SimpleNamespace(run=run, write=write, env=env, binary=binary, calls=calls,
                           groups=groups, finished=finished, temp=tmp_path)


def assert_deadline(result):
    assert not result.outer_timeout, "version reader escaped its deadline"
    assert (result.returncode, result.stdout) == (3, ""), result.stderr
    assert "did not finish within 0.5s" in result.stderr
    assert result.elapsed < 1.2, result.elapsed
    assert not result.late_finish_before_cleanup


def test_late_valid_version_is_refused_without_gnu_timeout(probe):
    result = probe.run("time.sleep(1.5)\nprint('2.1.281 (Claude Code)')", seconds="0.5")
    assert_deadline(result)
    assert probe.calls.read_text().splitlines() == ["called"]


def test_timeout_does_not_launch_the_diagnostic_probe_or_finish_later(probe):
    result = probe.run("time.sleep(1.5)\nPath(os.environ['FINISHED']).write_text('late')", seconds="0.5")
    assert_deadline(result)
    assert probe.calls.read_text().splitlines() == ["called"]
    assert not probe.finished.exists()


@pytest.mark.parametrize("leader_exits", [False, True])
def test_deadline_closes_child_held_output_and_ignoring_term_group(probe, leader_exits):
    # fork retains the real output descriptors without another interpreter
    # startup consuming the short budget before the child fixture is armed.
    body = ("if os.fork() == 0:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"    Path({str(probe.temp / 'child-started')!r}).touch()\n"
            "    time.sleep(1.5)\n"
            f"    Path({str(probe.finished)!r}).write_text('escaped')\n"
            "    os._exit(0)\n"
            + ("sys.exit(0)" if leader_exits else
               "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(1.5)"))
    result = probe.run(body, seconds="0.5", settle=1.7)
    assert (probe.temp / "child-started").exists(), "child cleanup was not exercised"
    assert_deadline(result)
    assert not probe.finished.exists()


@pytest.mark.parametrize("body, expected_rc, version, reason", [
    ("print('2.1.281 (Claude Code)')", 0, "2.1.281\n", ""),
    ("print('2.1.281');sys.exit(7)", 3, "", "exited 7: 2.1.281"),
    ("print('2.1.281', file=sys.stderr)", 3, "", "printed no parseable version: 2.1.281"),
    ("print('warning');print('2.1.281')", 3, "", "printed no parseable version: warning"),
    ("pass", 3, "", "printed no parseable version"),
    ("print('intentional');sys.exit(124)", 3, "", "exited 124: intentional"),
])
def test_healthy_and_error_contracts_without_gnu_timeout(probe, body, expected_rc, version, reason):
    result = probe.run(body)
    assert not result.outer_timeout
    assert (result.returncode, result.stdout) == (expected_rc, version), result.stderr
    assert reason in result.stderr
    assert "did not finish" not in result.stderr


def test_missing_interpreter_refuses_without_starting_a_version_process(probe):
    result = probe.run("print('2.1.281')", python=False)
    assert (result.returncode, result.stdout) == (3, "")
    assert "deadline" in result.stderr and "python3" in result.stderr
    assert not probe.calls.exists()


def test_diagnostic_probe_has_its_own_deadline_and_clears_stale_version(probe):
    probe.write("if len(Path(os.environ['CALLS']).read_text().splitlines()) == 1:\n"
                "    print('warning')\n"
                "else:\n    time.sleep(1.5)\n")
    result = probe.run(seconds="0.5", code=
                       f'CLAUDE_VERSION=9.9.9; measure_claude_version "{probe.binary}"; '
                       'printf "[%s]|%s" "$CLAUDE_VERSION" "$CLAUDE_VERSION_WHY"; exit 3')
    assert not result.outer_timeout
    assert result.elapsed < 1.2, result.elapsed
    assert result.stdout.startswith("[]|")
    assert "diagnostic" in result.stdout and "did not finish within 0.5s" in result.stdout


def test_missing_binary_keeps_exit_127_diagnostic(probe):
    result = probe.run()
    assert (result.returncode, result.stdout) == (3, "")
    assert "exited 127" in result.stderr


def test_deadline_does_not_signal_an_unrelated_owned_sibling(probe):
    sibling = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(10)"],
                               env=probe.env, start_new_session=True)
    try:
        result = probe.run("time.sleep(1.5)", seconds="0.5")
        assert_deadline(result)
        assert sibling.poll() is None
    finally:
        sibling.kill()
        sibling.wait(timeout=2)


@pytest.mark.parametrize("seconds", ["0", "-1", "nan", "inf", "nonsense"])
def test_invalid_deadline_refuses_before_starting_the_binary(probe, seconds):
    result = probe.run("print('2.1.281')", seconds=seconds)
    assert (result.returncode, result.stdout) == (3, "")
    assert "deadline must be positive finite seconds" in result.stderr
    assert not probe.calls.exists()


def test_missing_helper_refuses_without_unbounded_fallback(probe):
    probe.write("print('2.1.281')")
    result = probe.run(code=f'_LIB_COMMON_DIR="{probe.temp}"; '
                       f'measure_claude_version "{probe.binary}"; '
                       'printf "%s" "$CLAUDE_VERSION_WHY"; exit 3')
    assert "deadline helper failed" in result.stdout
    assert not probe.calls.exists()


def test_empty_explicit_binary_does_not_require_interpreter(probe):
    result = probe.run(python=False, code='measure_claude_version ""; '
                       'printf "%s" "$CLAUDE_VERSION_WHY"; exit 3')
    assert result.stdout == "no claude binary resolved"
    assert not probe.calls.exists()


@pytest.mark.parametrize("interrupt", [signal.SIGTERM, signal.SIGINT])
def test_interrupted_helper_cleans_its_owned_group(probe, interrupt):
    probe.write("time.sleep(1.5)\nPath(os.environ['FINISHED']).write_text('escaped')")
    proc = subprocess.Popen([sys.executable, str(REPO / "lib/command-deadline.py"),
                             "3", "stdout", str(probe.binary)], env=probe.env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, text=True)
    try:
        until = time.monotonic() + 2
        while not probe.calls.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert probe.calls.exists(), "helper never launched the owned probe"
        proc.send_signal(interrupt)  # only this helper PID; it owns the group
        stdout, stderr = proc.communicate(timeout=2)
        time.sleep(1.7)
        assert not probe.finished.exists(), "interruption abandoned the probe"
        assert (proc.returncode, stdout) == (125, "")
        assert "interrupted" in stderr
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=2)
