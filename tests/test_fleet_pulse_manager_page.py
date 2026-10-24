"""An alert whose subject is the fleet manager pages the fleet's channel (#997, #2129).

Every per-bot alert is pushed into the pane of the bot's manager, and the manager's own
alerts into its own pane: when the manager is the one that cannot act, nothing else
carries them. So an alert about the manager also posts to the fleet's alert channel,
through the escalation's chat and sender, once an episode: its latch sits beside the
alert's debounce marker (`<marker>.paged`) and is cleared with it. A push the manager's
pane refused, at a menu or a held box, was not delivered, so it leaves the alert's window
open (rc 3); 4 means only that the manager has no session.

Each case drives the REAL sweep (the repo's claudlobby/_runtime_scripts/, linked file by
file) over a fleet of two declared bots with live sessions: the manager, whose pane is a
stand-in (an input box that logs each line it is submitted, or a menu that records every
byte typed into it), and a worker. Stand-ins: tg-post.sh (appends each page to a file),
plane-lookup.py and dispatch-overdue.py (nothing to read) and systemctl; the sweep
records nothing (PLANE_EMIT_DISABLED=1).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.conftest import _write_exec
from tests.plane_fixtures import F, REPO, plane_root

LIB = REPO / "claudlobby/_runtime_scripts"
FIXTURES = REPO / "tests/fixtures"
pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="fleet-pulse needs tmux")
FLEET_YAML = (f"fleet:\n  name: {F}\n  manager: mgr\n  service_prefix: com.test\n  bots:\n"
              "    mgr:\n      expertise: [orchestration]\n"
              "    w1:\n      expertise: [software-engineering]\n")
HOUR = 3600


class Fleet:
    """Fleet f: the manager mgr and the worker w1, and the pages the sweep posted."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.root = plane_root(tmp)
        (self.root / "home").mkdir()
        fleet_dir = self.root / "local" / F
        fleet_dir.mkdir(parents=True)
        (fleet_dir / "fleet.yaml").write_text(FLEET_YAML)
        # a short path: a tmux socket's path is limited to about 100 bytes
        self.tmux = Path(tempfile.mkdtemp(prefix="t997-", dir="/tmp"))
        self.tmux_env = {**os.environ, "TMUX_TMPDIR": str(self.tmux)}
        self.bin = tmp / "bin"
        self.bin.mkdir()
        _write_exec(self.bin / "systemctl", "#!/bin/sh\nexit 3\n")
        self.proc = tmp / "proc"
        self.proc.mkdir()
        self.pages = tmp / "pages.log"
        self.lib = self._lib(tmp / "lib")
        self.state = self.root / "state" / "pulse"
        self.bots = fleet_dir / "runtime" / "bots"
        for bot in ("mgr", "w1"):
            (self.bots / bot / "data").mkdir(parents=True)
            (self.bots / bot / "bot.conf").write_text(
                f"BOT_NAME={bot}\nBOT_ID={bot}\nFLEET_NAME={F}\nTMUX_SOCKET=t997-{bot}\n"
                "MANAGER_TMUX=mgr\nMANAGER_TMUX_SOCKET=t997-mgr\n")
        r = self.tmux_cmd("-L", "t997-w1", "new-session", "-d", "-s", "w1", "sleep 3600")
        assert r.returncode == 0, r.stderr

    def _lib(self, lib: Path) -> Path:
        lib.mkdir()
        stubs = {
            "tg-post.sh": f'#!/bin/bash\nprintf "%s\\n" "$1" >> {shlex.quote(str(self.pages))}\n',
            "plane-lookup.py": "import sys\nsys.exit(0)\n",
            "dispatch-overdue.py": "import sys\nsys.exit(0)\n",
        }
        for f in LIB.iterdir():
            if f.name not in stubs:
                (lib / f.name).symlink_to(f)
        for name, body in stubs.items():
            _write_exec(lib / name, body)
        return lib

    def tmux_cmd(self, *argv):
        return subprocess.run(["tmux", *argv], env=self.tmux_env, capture_output=True, text=True, timeout=30)

    def start_box(self) -> Path:
        """The manager's pane: an input box; returns the log of the lines it is submitted."""
        log = self.tmp / "manager.log"
        log.touch()
        box = f"{shlex.quote(sys.executable)} {shlex.quote(str(FIXTURES / 'input-box-stub.py'))} --log {shlex.quote(str(log))}"
        r = subprocess.run(["bash", "-c", f". {shlex.quote(str(LIB / 'lib-common.sh'))} && "
                            f"harness_box_up t997-mgr mgr {shlex.quote(box)}"],
                           env=self.tmux_env, capture_output=True, text=True, timeout=120)
        assert r.stdout.strip() == "drawn", r.stdout + r.stderr
        return log

    def start_menu(self) -> Path:
        """The manager's pane: Claude Code's usage-limit menu; returns the record of the
        bytes typed into it."""
        keys = self.tmp / "menu-keys"
        frame = FIXTURES / "pane-states" / "usage-limit-menu.txt"
        menu = (f"{shlex.quote(sys.executable)} {shlex.quote(str(FIXTURES / 'menu-pane-stub.py'))} "
                f"{shlex.quote(str(frame))} --keys {shlex.quote(str(keys))}")
        r = self.tmux_cmd("-L", "t997-mgr", "new-session", "-d", "-s", "mgr", menu)
        assert r.returncode == 0, r.stderr
        deadline = time.monotonic() + 10
        while "Esc to cancel" not in self.tmux_cmd("-L", "t997-mgr", "capture-pane", "-t", "mgr", "-p").stdout:
            assert time.monotonic() < deadline, "the menu frame did not render"
            time.sleep(0.05)
        return keys

    def stop_pane(self):
        """End the manager's pane, waiting for its tmux server to exit, so the next
        pane starts on a fresh server."""
        subprocess.run(["bash", "-c", f". {shlex.quote(str(LIB / 'lib-common.sh'))} && "
                        "harness_box_down t997-mgr mgr"],
                       env=self.tmux_env, capture_output=True, text=True, timeout=60)

    def stall(self, bot: str):
        """No tool call for two hours and no idle tick since: activity_stuck."""
        (self.bots / bot / "data" / ".idle").unlink(missing_ok=True)
        marker = self.bots / bot / "data" / ".last-tool-call"
        marker.touch()
        then = time.time() - 2 * HOUR
        os.utime(marker, (then, then))

    def idle(self, bot: str):
        """keepalive saw the bot idle after its last tool call: no alert."""
        (self.bots / bot / "data" / ".idle").touch()

    def posts(self) -> list[str]:
        return self.pages.read_text().splitlines() if self.pages.exists() else []

    def sweep(self, **env):
        result = subprocess.run(
            ["bash", str(self.lib / "fleet-pulse.sh"), F],
            env={"PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin", "HOME": str(self.root / "home"),
                 "LANG": "C.UTF-8", "CLAUDLOBBY_ROOT": str(self.root), "FLEET_NAME": F,
                 "TMUX_TMPDIR": str(self.tmux), "BROWSER_SCAN_PROC": str(self.proc),
                 "PLANE_EMIT_DISABLED": "1",
                 "FLEET_PULSE_ESCALATION_CHAT_ID": "-1001234567890",
                 "FLEET_PULSE_ESCALATION_STATE_DIR": str(self.root / "escalation-sender"), **env},
            capture_output=True, text=True, errors="replace", timeout=300)
        assert result.returncode == 0, result.stderr[-3000:]
        return result

    def close(self):
        for sock in ("t997-mgr", "t997-w1"):
            self.tmux_cmd("-L", sock, "kill-server")
        shutil.rmtree(self.tmux, ignore_errors=True)


@pytest.fixture
def fleet(tmp_path):
    f = Fleet(tmp_path)
    try:
        yield f
    finally:
        f.close()


def about(posts: list[str], text: str) -> list[str]:
    return [p for p in posts if text in p]


def test_an_alert_about_the_manager_posts_to_the_channel_once_an_episode(fleet):
    """Both bots have made no tool call for two hours with no idle tick since, so the
    sweep raises activity_stuck about each. The manager's alert is pushed into its own
    pane, as before, and also posts to the channel, once: the next sweep posts nothing.
    The manager then reads idle, which ends the episode and its latch, and it stalls
    again: that episode posts once more. The worker's alert goes to the manager's pane
    alone, as before."""
    log = fleet.start_box()
    fleet.stall("mgr")
    fleet.stall("w1")
    latch = fleet.state / "mgr.activity_alerted.paged"
    fleet.sweep()
    posts = fleet.posts()
    assert len(about(posts, "mgr activity_stuck")) == 1, posts
    assert about(posts, "w1 activity_stuck") == [], posts
    assert latch.exists()
    submitted = log.read_text()
    assert "mgr activity_stuck" in submitted and "w1 activity_stuck" in submitted, submitted
    fleet.sweep()
    assert len(about(fleet.posts(), "mgr activity_stuck")) == 1, fleet.posts()
    fleet.idle("mgr")
    fleet.sweep()
    assert not latch.exists()
    assert len(about(fleet.posts(), "mgr activity_stuck")) == 1, fleet.posts()
    fleet.stall("mgr")
    fleet.sweep()
    posts = fleet.posts()
    assert len(about(posts, "mgr activity_stuck")) == 2, posts
    assert about(posts, "w1 activity_stuck") == [], posts


def test_a_push_the_managers_menu_refused_leaves_the_alert_open(fleet):
    """The manager's pane shows a menu, which reads typed keys as choices, so nothing
    is typed into it. That push was not delivered, so the worker's activity_stuck
    window stays open and no debounce marker is written: the alert is sent again once
    the pane takes input. Nothing was typed either, so no held-push floor holds the
    next push back. Positive control: the same alert pushed into an input box writes
    the marker."""
    keys = fleet.start_menu()
    fleet.idle("mgr")
    fleet.stall("w1")
    marker = fleet.state / "w1.activity_alerted"
    fleet.sweep()
    time.sleep(0.5)  # any byte that did cross the pty has reached the record
    assert keys.read_bytes() == b"", "bytes were typed into the menu"
    assert not marker.exists(), marker.read_text()
    assert not list(fleet.state.glob("held-push.*")), "a held-push floor was written"
    fleet.stop_pane()
    log = fleet.start_box()
    fleet.sweep()
    assert marker.exists()
    assert "w1 activity_stuck" in log.read_text(), log.read_text()
