"""Falsify the native evidence controller without starting a native service."""
import os
import hashlib
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import shlex
import signal
import sys
from types import SimpleNamespace

import pytest

from tests.conftest import constructed_env
from tests import native_supervisor_evidence as native
from tests.native_supervisor_evidence import run_owned_session


@pytest.mark.parametrize("detached", [False, True], ids=["plain-child", "setsid-child"])
def test_timeout_runs_exit_cleanup_and_reaps_plain_child(tmp_path, detached):
    home = tmp_path / "home"
    home.mkdir()
    child_pid = tmp_path / "child.pid"
    marker = tmp_path / "exit-cleanup"
    startup = tmp_path / "cancel.sh"
    startup.write_text("trap 'exit 124' TERM\n")
    child = tmp_path / "plain_child.py"
    child.write_text(
        "import os, pathlib, time\n"
        + ("os.setsid()\n" if detached else "")
        + f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(120)\n"
    )
    harness = tmp_path / "timeout-control.sh"
    harness.write_text(
        "#!/bin/bash\n"
        + shlex.quote(sys.executable) + " " + shlex.quote(str(child)) + " &\n"
        + "child=$!\n"
        + "cleanup() { "
        + ("" if detached else 'kill "$child" 2>/dev/null || :; wait "$child" 2>/dev/null || :; ')
        + "printf cleaned > " + shlex.quote(str(marker)) + "; }\n"
        + "trap cleanup EXIT\nwait \"$child\"\n"
    )
    env = constructed_env(HOME=home, TMPDIR=tmp_path, BASH_ENV=startup)
    try:
        proc, cleanup = run_owned_session(
            ["/bin/bash", harness], cwd=tmp_path, env=env, timeout=2, grace=3,
            stdout_path=tmp_path / "stdout", stderr_path=tmp_path / "stderr",
        )
        assert child_pid.is_file(), "Control did not start its plain Python child"
        pid = int(child_pid.read_text())
        assert proc.returncode == 124
        assert cleanup["timed_out"] and not cleanup["cancelled"]
        assert cleanup["terminated_children"] or cleanup["detached_terminated"], "Control did not exercise cancellation"
        assert cleanup["remaining"] == {}, "Owned group survived cancellation"
        assert cleanup["detached_remaining"] == {}, "Detached owned child survived cancellation"
        if detached:
            assert pid in cleanup["detached_terminated"], "setsid child was not found outside the owned group"
        assert marker.read_text() == "cleaned", "Shell EXIT cleanup was bypassed"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError(f"Plain Python child {pid} survived cancellation")
    finally:
        # Even a falsified cleanup implementation must not leak the control.
        if child_pid.is_file():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


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
                       "terminated_children": [], "remaining": {},
                       "detached_terminated": [], "detached_remaining": {}}


def test_permission_failure_with_live_group_is_not_suppressed(monkeypatch):
    proc = SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(native, "process_group_members", lambda _pgid: {123: "S"})

    def denied(_pgid, _signal):
        raise PermissionError("owned live group could not be signaled")

    monkeypatch.setattr(native.os, "killpg", denied)
    with pytest.raises(PermissionError, match="owned live group"):
        native.signal_live_group(proc, signal.SIGTERM)


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("flock"), reason="Linux consent lock uses flock")
def test_session_fixture_supplies_the_real_consent_lock_parent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).resolve().parents[1]
    script = (
        '. "$1/lib/lib-common.sh"\n'
        'write_consent() { printf accepted > "$HOME/.claude/settings.json"; }\n'
        'with_lock "$HOME/.claude/settings.json.lock" write_consent\n'
    )
    env = constructed_env(HOME=home, TMPDIR=tmp_path)

    def run_lock():
        return subprocess.run(["/bin/bash", "-c", script, "probe", str(source)],
                              cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)

    cold = run_lock()
    assert cold.returncode != 0
    assert "settings.json.lock" in cold.stderr and "No such file or directory" in cold.stderr
    assert not (home / ".claude/settings.json").exists()
    native.prepare_session_home(home)
    assert not (home / ".claude/settings.json").exists(), "Fixture must leave consent to the launcher"
    ready = run_lock()
    assert ready.returncode == 0, ready.stderr
    assert (home / ".claude/settings.json").read_text() == "accepted"


def _launchd_snapshot():
    return {"persistent_launch_agents": {"files": {"/owned/guard.plist": "sha"}, "labels": ["guard"]},
            "launchd_disabled": {"guard": "false"},
            "registrations": {"guard": ["42", "0"], "transient.apple.job": ["-", "0"]}}


def test_launchd_preservation_discloses_ambient_liveness_changes():
    before = _launchd_snapshot()
    after = deepcopy(before)
    after["registrations"]["transient.apple.job"] = ["99", "0"]
    assert native.verify_launchd_preservation(before, after) == {
        "transient.apple.job": {"before": ["-", "0"], "after": ["99", "0"]}}


@pytest.mark.parametrize("mutation,expected", [
    ("definition", "definitions changed"),
    ("disabled", "disabled overrides changed"),
    ("unloaded", "persistent launch agent was unloaded"),
])
def test_launchd_preservation_rejects_persistent_changes(mutation, expected):
    before = _launchd_snapshot()
    after = deepcopy(before)
    if mutation == "definition":
        after["persistent_launch_agents"]["files"]["/owned/guard.plist"] = "different"
    elif mutation == "disabled":
        after["launchd_disabled"]["guard"] = "true"
    else:
        del after["registrations"]["guard"]
    with pytest.raises(AssertionError, match=expected):
        native.verify_launchd_preservation(before, after)


def test_macos_agent_labels_use_native_parser_and_preserve_original_bytes(tmp_path, monkeypatch):
    raw = b"<?xml version=1.0?><plist><dict><key>Label</key><string>guard</string></dict></plist>"
    path = tmp_path / "guard.plist"
    path.write_bytes(raw)
    monkeypatch.setattr(native.platform, "system", lambda: "Darwin")
    calls = []

    def native_read(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "guard\n", "")

    monkeypatch.setattr(native.subprocess, "run", native_read)
    snapshot = native.persistent_launch_agents([tmp_path])
    assert snapshot == {"files": {str(path): hashlib.sha256(raw).hexdigest()}, "labels": ["guard"]}
    assert calls == [["/usr/bin/plutil", "-extract", "Label", "raw", "-expect", "string", "-o", "-", str(path)]]
    assert path.read_bytes() == raw


def test_native_plist_parse_failure_names_the_file_instead_of_skipping(tmp_path, monkeypatch):
    path = tmp_path / "broken.plist"
    path.write_text("broken")
    monkeypatch.setattr(native.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(native.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 1, "", "parse rejected"))
    with pytest.raises(AssertionError, match="broken.plist: native plist Label read failed: parse rejected"):
        native.persistent_launch_agents([tmp_path])
