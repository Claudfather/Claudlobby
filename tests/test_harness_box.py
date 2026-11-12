"""lib-common's harness_box_down when the old tmux server outlives its wait (#2405).

The helper ends a session and, when it was its server's last, waits for that
server to exit, because a start on the same socket fails while it is still
exiting. A server set to stay up with no sessions (`exit-empty off`) never
exits. Every caller discards the helper's status, so its line on stderr is the
only record of that state.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.conftest import constructed_env

LIB = Path(__file__).resolve().parents[1] / "claudlobby" / "_runtime_scripts" / "lib-common.sh"
TMUX = shutil.which("tmux")

pytestmark = pytest.mark.skipif(TMUX is None, reason="tmux not installed")


@pytest.fixture
def server():
    """A private tmux server whose one session is `stand-in`: (env, tmux)."""
    # A short, private socket path stays under macOS's Unix-socket path limit.
    with TemporaryDirectory(prefix="cl-box-", dir="/tmp") as scratch:
        env = constructed_env(TMUX_TMPDIR=scratch)

        def tmux(*args):
            return subprocess.run([TMUX, "-L", "box", "-f", "/dev/null", *args], env=env,
                                  capture_output=True, text=True, timeout=10,
                                  check=True).stdout.strip()

        tmux("new-session", "-d", "-s", "stand-in", "sleep 300")
        try:
            yield env, tmux
        finally:
            subprocess.run([TMUX, "-L", "box", "kill-server"], env=env,
                           capture_output=True, timeout=10)


def _box_down(env):
    return subprocess.run(["bash", "-c", f'. "{LIB}"; set +e; harness_box_down box stand-in'],
                          env=env, capture_output=True, text=True, timeout=60)


def test_a_server_that_outlives_the_wait_is_named_on_stderr(server):
    env, tmux = server
    tmux("set-option", "-s", "exit-empty", "off")
    pid = tmux("display-message", "-p", "-t", "stand-in", "#{pid}")
    r = _box_down(env)
    assert (r.returncode, r.stdout) == (1, ""), r
    os.kill(int(pid), 0)  # the state the line names: the old server is still running
    line = re.fullmatch(rf"harness_box_down: tmux server {pid} is still running (\d+) s"
                        r" after its last session \(stand-in\) ended\n", r.stderr)
    assert line and int(line.group(1)) >= 5, r.stderr


def test_a_server_that_exits_leaves_no_line(server):
    env, tmux = server
    pid = int(tmux("display-message", "-p", "-t", "stand-in", "#{pid}"))
    r = _box_down(env)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), r
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
