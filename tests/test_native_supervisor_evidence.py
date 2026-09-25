"""Falsify the native evidence controller without starting a native service."""
import os
import shlex
import sys

from conftest import constructed_env
from native_supervisor_evidence import run_owned_session


def test_timeout_runs_exit_cleanup_and_reaps_plain_child(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    child_pid = tmp_path / "child.pid"
    marker = tmp_path / "exit-cleanup"
    startup = tmp_path / "cancel.sh"
    startup.write_text("trap 'exit 124' TERM\n")
    child = tmp_path / "plain_child.py"
    child.write_text(
        "import os, pathlib, time\n"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(120)\n"
    )
    harness = tmp_path / "timeout-control.sh"
    harness.write_text(
        "#!/bin/bash\n"
        + shlex.quote(sys.executable) + " " + shlex.quote(str(child)) + " &\n"
        + "child=$!\n"
        + "cleanup() { kill \"$child\" 2>/dev/null || :; wait \"$child\" 2>/dev/null || :; "
        + "printf cleaned > " + shlex.quote(str(marker)) + "; }\n"
        + "trap cleanup EXIT\nwait \"$child\"\n"
    )
    env = constructed_env(HOME=home, TMPDIR=tmp_path, BASH_ENV=startup)
    proc, cleanup = run_owned_session(
        ["/bin/bash", harness], cwd=tmp_path, env=env, timeout=2, grace=3,
        stdout_path=tmp_path / "stdout", stderr_path=tmp_path / "stderr",
    )
    assert child_pid.is_file(), "Control did not start its plain Python child"
    pid = int(child_pid.read_text())
    assert proc.returncode == 124
    assert cleanup["timed_out"] and not cleanup["cancelled"]
    assert cleanup["terminated_children"], "Control did not exercise group cancellation"
    assert cleanup["remaining"] == {}, "Owned group survived cancellation"
    assert marker.read_text() == "cleaned", "Shell EXIT cleanup was bypassed"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError(f"Plain Python child {pid} survived cancellation")


def test_normal_completion_runs_exit_cleanup_without_reaping(tmp_path):
    startup = tmp_path / "cancel.sh"
    startup.write_text("trap 'exit 124' TERM\n")
    marker = tmp_path / "exit-cleanup"
    harness = tmp_path / "normal-control.sh"
    harness.write_text(
        "#!/bin/bash\n"
        + "cleanup() { printf cleaned > " + shlex.quote(str(marker)) + "; }\n"
        + "trap cleanup EXIT\nprintf complete\n"
    )
    proc, cleanup = run_owned_session(
        ["/bin/bash", harness], cwd=tmp_path,
        env=constructed_env(HOME=tmp_path, TMPDIR=tmp_path, BASH_ENV=startup),
        timeout=10, grace=3, stdout_path=tmp_path / "stdout", stderr_path=tmp_path / "stderr",
    )
    assert proc.returncode == 0 and proc.stdout == "complete"
    assert marker.read_text() == "cleaned"
    assert cleanup == {"timed_out": False, "cancelled": False,
                       "terminated_children": [], "remaining": {}}
