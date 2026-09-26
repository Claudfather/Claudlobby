"""The real crash-window test must survive Darwin's disappearing group race."""

import os
import signal
import subprocess

from tests import test_plane_gauntlet_doors as gauntlet

# Reuse the recording fixture, including its owned root/socket/CLI checks.
armed = gauntlet.armed


def test_intent_crash_handles_group_disappearing_after_first_signal(tmp_path, armed, monkeypatch):
    real_killpg = os.killpg
    signaled = set()
    groups = []
    real_popen = subprocess.Popen

    def tracked_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        if kwargs.get("start_new_session"):
            groups.append(proc)
        return proc

    def kill_once(pgid, sig):
        assert sig == signal.SIGKILL
        if pgid in signaled:
            # macOS can deny a repeated signal while the dead leader has not
            # yet been reaped. The first call below still performs a real kill.
            raise PermissionError("already-killed fixture group")
        real_killpg(pgid, sig)
        signaled.add(pgid)

    monkeypatch.setattr(os, "killpg", kill_once)
    monkeypatch.setattr(subprocess, "Popen", tracked_popen)
    try:
        gauntlet.test_crash_between_intent_and_send_leaves_visible_intent(tmp_path, armed)
    finally:
        # Independent cleanup also covers a mutant that omits the crash.
        for proc in groups:
            if proc.pid not in signaled and proc.poll() is None:
                real_killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
    assert len(signaled) == 1, "the intent-window process group was not actually killed"
