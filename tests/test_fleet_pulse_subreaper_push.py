"""A session that lost its subreaper is pushed to its manager once an episode (#2892).

Since #2226 the pulse records bot_subreaper_missing on every sweep that reads a live
session's tmux server outside its bot-subreaper, and nothing read the record: it was
not pushed, not critical and not in the brief. Now the bot's manager also gets one
push an episode, through the debounce every alert uses: read the start's
bot_subreaper_unavailable first, newest first and from the session's start. If the
start recorded one, do not restart: send its reason to the manager (to the operator,
for the manager itself). If not, restart the bot at its next safe point.
Recording is unchanged. A sweep that reads the session under its subreaper again ends
the episode, and one with no live session leaves it open. The re-notify window (#831)
applies, as it does to every other alert. OBSERVABILITY_SUBREAPER_PUSH=0 in the bot's
bot.conf stops the push, says so on stderr, leaves the record and ends the episode.
The push about the manager itself
posts nothing to the fleet's channel: unlike the alerts #997 posts there, this one
leaves the manager's own pane able to act on it. Its recipient is that pane's session,
so the manager's own restart changes it: the first change is pushed at the next sweep,
and a later one no more than once per re-arm window (#831, #1088).

Each case drives the REAL sweep (the repo's claudlobby/_runtime_scripts/, linked file
by file) over a fleet of two declared bots with live sessions: the manager, whose pane
is the input-box stand-in that logs each line it is submitted, and a worker, w1. A
session started with plain tmux under STAND_IN, a child subreaper named plain-tmux
that adopts its server, runs outside any bot-subreaper: the shape the pulse reads as
lost, on any host (a server started from inside a bot session would otherwise be
adopted by that session's own bot-subreaper and read as kept). w1 started through
lib-common's bot_session_spawn runs under its own, the shape start-bot.sh gives a bot. The sweep records into the root's plane
staging (no daemon serves this root), which is where the record is counted.
Stand-ins: tg-post.sh (appends each channel post to a file), plane-lookup.py and
dispatch-overdue.py (nothing to read) and systemctl. A child subreaper is a Linux
facility, so these run on Linux only.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from tests.conftest import _write_exec
from tests.plane_fixtures import F, REPO, plane_root

LIB = REPO / "claudlobby/_runtime_scripts"
FIXTURES = REPO / "tests/fixtures"
pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="a child subreaper is a Linux facility"),
    pytest.mark.skipif(shutil.which("tmux") is None, reason="fleet-pulse needs tmux"),
]
FLEET_YAML = (f"fleet:\n  name: {F}\n  manager: mgr\n  service_prefix: com.test\n  bots:\n"
              "    mgr:\n      expertise: [orchestration]\n"
              "    w1:\n      expertise: [software-engineering]\n")
HOUR = 3600
# A child subreaper named plain-tmux: it runs its argv (a command that starts a tmux
# session), writes the command's status and output to its first argument, then holds
# the server it adopted until the server exits.
STAND_IN = textwrap.dedent("""\
    import ctypes, os, subprocess, sys
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        sys.exit("cannot become a child subreaper")
    libc.prctl(15, b"plain-tmux", 0, 0, 0)  # PR_SET_NAME: the name the push gives
    out, argv = sys.argv[1], sys.argv[2:]
    done = subprocess.run(argv, capture_output=True, text=True)
    with open(out + ".tmp", "w") as f:
        f.write(f"{done.returncode}\\n{done.stdout}{done.stderr}")
    os.replace(out + ".tmp", out)
    while True:
        try:
            os.wait()
        except ChildProcessError:
            break
""")
OFF_LINE = ("fleet-pulse: w1 bot_subreaper_missing push: OFF here (OBSERVABILITY_SUBREAPER_PUSH=0)"
            " -- the sweep still records the event")
# What the rule text says to do when the start recorded a bot_subreaper_unavailable.
CLAUSE = ("If it did, don't restart: send the recorded reason to your manager"
          " (to the operator if you are the fleet manager).")


def parent_comm(pid: int) -> str:
    stat = Path(f"/proc/{pid}/stat").read_text()
    ppid = int(stat.rsplit(")", 1)[1].split()[1])
    return Path(f"/proc/{ppid}/comm").read_text().strip()


class Fleet:
    """Fleet f: the manager mgr and the worker w1, what mgr was submitted, and the
    channel's posts."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.root = plane_root(tmp)
        (self.root / "home").mkdir()
        fleet_dir = self.root / "local" / F
        fleet_dir.mkdir(parents=True)
        (fleet_dir / "fleet.yaml").write_text(FLEET_YAML)
        # a short path: a tmux socket's path is limited to about 100 bytes
        self.tmux = Path(tempfile.mkdtemp(prefix="t2892-", dir="/tmp"))
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
            self.conf(bot)
        self.log = self.tmp / "manager.log"
        self.log.touch()
        self.stand_ins: list[subprocess.Popen] = []

    def conf(self, bot: str, extra: str = ""):
        (self.bots / bot / "bot.conf").write_text(
            f"BOT_NAME={bot}\nBOT_ID={bot}\nFLEET_NAME={F}\nTMUX_SOCKET=t2892-{bot}\n"
            f"MANAGER_TMUX=mgr\nMANAGER_TMUX_SOCKET=t2892-mgr\n{extra}")

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

    def outside(self, *argv) -> tuple[int, str]:
        """Run argv, which starts a tmux session, under STAND_IN: its server runs outside
        any bot-subreaper. Returns argv's status and output."""
        out = self.tmp / f"stand-in-{len(self.stand_ins)}.out"
        self.stand_ins.append(subprocess.Popen(
            [sys.executable, "-c", STAND_IN, str(out), *argv], env=self.tmux_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
        deadline = time.monotonic() + 120
        while not out.exists():
            assert self.stand_ins[-1].poll() is None, "the stand-in subreaper exited"
            assert time.monotonic() < deadline, "the stand-in's command did not finish"
            time.sleep(0.05)
        rc, text = out.read_text().split("\n", 1)
        return int(rc), text

    def server_parent(self, bot: str) -> str:
        server = int(self.tmux_cmd("-L", f"t2892-{bot}", "display-message", "-p", "#{pid}").stdout)
        return parent_comm(server)

    def start_box(self):
        """The manager's pane: an input box that logs each line it is submitted, on a
        server outside any bot-subreaper."""
        box = (f"{shlex.quote(sys.executable)} {shlex.quote(str(FIXTURES / 'input-box-stub.py'))}"
               f" --log {shlex.quote(str(self.log))}")
        rc, out = self.outside("bash", "-c", f". {shlex.quote(str(LIB / 'lib-common.sh'))} && "
                               f"harness_box_up t2892-mgr mgr {shlex.quote(box)}")
        assert rc == 0 and out.strip().endswith("drawn"), out
        assert self.server_parent("mgr") == "plain-tmux"

    def restart_box(self):
        """Replace the manager's session, as its own restart does: end its server, wait for
        the process to go, and start a new box outside any bot-subreaper. The push's
        recipient, the session's creation time and its pane's pid (#831), is a new one."""
        server = int(self.tmux_cmd("-L", "t2892-mgr", "display-message", "-p", "#{pid}").stdout)
        self.tmux_cmd("-L", "t2892-mgr", "kill-server")
        deadline = time.monotonic() + 10
        while Path(f"/proc/{server}").exists():
            assert time.monotonic() < deadline, "the manager's server did not exit"
            time.sleep(0.05)
        self.start_box()

    def start_worker(self, *, subreaper: bool):
        """w1's session: under its own bot-subreaper, as start-bot.sh starts it, or with
        plain tmux, outside any."""
        if not subreaper:
            rc, out = self.outside("tmux", "-L", "t2892-w1", "new-session", "-d", "-s", "w1", "sleep 3600")
            assert rc == 0, out
            assert self.server_parent("w1") == "plain-tmux"
            return
        starter = (f". {shlex.quote(str(LIB / 'lib-common.sh'))} >/dev/null 2>&1\nset +e\n"
                   "emit_fleet_event() { :; }\n"
                   f"_NATIVE_ADMISSION_PYTHON={shlex.quote(sys.executable)}\n"
                   "bot_session_spawn t2892-w1 w1 'sleep 3600'\n"
                   'printf "%s\\n" "$BOT_SUBREAPER_REPORT"\n')
        r = subprocess.run(["bash", "-c", starter], env=self.tmux_env, capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0 and r.stdout.strip().endswith("adopted=yes"), r.stdout + r.stderr
        deadline = time.monotonic() + 10
        while self.server_parent("w1") != "bot-subreaper":  # it takes its name as it re-executes
            assert time.monotonic() < deadline, "the subreaper never took its name"
            time.sleep(0.05)

    def stop_worker(self):
        """End w1's session and wait for its server to be gone."""
        self.tmux_cmd("-L", "t2892-w1", "kill-server")
        deadline = time.monotonic() + 10
        while self.tmux_cmd("-L", "t2892-w1", "has-session", "-t", "=w1").returncode == 0:
            assert time.monotonic() < deadline, "w1's server did not exit"
            time.sleep(0.05)

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

    def pushes(self, bot: str) -> list[str]:
        """The bot_subreaper_missing pushes about *bot* the manager was submitted."""
        return [line for line in self.log.read_text().splitlines()
                if line.startswith(f"[FLEET-PULSE] {bot} bot_subreaper_missing — ")]

    def recorded(self, bot: str) -> int:
        """bot_subreaper_missing records about *bot* in the root's plane staging."""
        staged = self.root / "state" / "plane" / "staged"
        alias = f'"bot:{F}/{bot}"'
        return sum(1 for f in staged.rglob("*") if f.is_file()
                   for line in f.read_text(errors="replace").splitlines()
                   if '"bot_subreaper_missing"' in line and alias in line)

    def posts(self) -> list[str]:
        return self.pages.read_text().splitlines() if self.pages.exists() else []

    def sweep(self, **env):
        (self.root / ".claudlobby-harness-root").write_text(str(self.root.resolve()))
        result = subprocess.run(
            [sys.executable, str(REPO / "harness/compose.py"), "--root", str(self.root),
             "--fleet", F, "--record-plane", "--pulse", "--native-dir", str(self.lib)],
            env={"PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin", "HOME": str(self.root / "home"),
                 "LANG": "C.UTF-8", "CLAUDLOBBY_ROOT": str(self.root), "FLEET_NAME": F,
                 "TMUX_TMPDIR": str(self.tmux), "BROWSER_SCAN_PROC": str(self.proc),
                 "FLEET_PULSE_ESCALATION_CHAT_ID": "-1001234567890",
                 "FLEET_PULSE_ESCALATION_STATE_DIR": str(self.root / "escalation-sender"), **env},
            capture_output=True, text=True, errors="replace", timeout=300)
        assert result.returncode == 0, result.stderr[-3000:]
        return result

    def close(self):
        for sock in ("t2892-mgr", "t2892-w1"):
            self.tmux_cmd("-L", sock, "kill-server")
        for p in self.stand_ins:  # each leaves once the server it held is gone
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        shutil.rmtree(self.tmux, ignore_errors=True)


@pytest.fixture
def fleet(tmp_path):
    f = Fleet(tmp_path)
    try:
        yield f
    finally:
        f.close()


def test_a_session_without_its_subreaper_is_pushed_once_an_episode_and_recorded_every_sweep(fleet):
    """w1 runs outside any subreaper: the first sweep records it and pushes it to the
    manager with the restart as its instruction; the second records it again and
    pushes nothing. A sweep that reads w1 under its own subreaper ends the episode, so
    a later session outside one is pushed again."""
    fleet.start_box()
    fleet.idle("mgr")
    fleet.idle("w1")
    fleet.start_worker(subreaper=False)
    marker = fleet.state / "w1.subreaper_alerted"
    fleet.sweep()
    pushed = fleet.pushes("w1")
    assert len(pushed) == 1, fleet.log.read_text()
    assert "runs under plain-tmux, not its bot-subreaper" in pushed[0], pushed
    assert ("Read its start's bot_subreaper_unavailable first (claudlobby --json event list"
            " --bot w1 --type bot_subreaper_unavailable). It lists newest first, and only a row"
            " from its session's start counts. If its start recorded one, a restart may fail as"
            f" its start did. {CLAUSE} If not, restart it") in pushed[0], pushed
    assert "If not, restart it at its next safe point, after the safe-worker-restart checks" in pushed[0], pushed
    assert fleet.recorded("w1") == 1
    assert marker.exists()
    fleet.sweep()
    assert len(fleet.pushes("w1")) == 1, fleet.log.read_text()
    assert fleet.recorded("w1") == 2
    fleet.stop_worker()
    fleet.start_worker(subreaper=True)
    fleet.sweep()
    assert not marker.exists(), "a session under its subreaper did not end the episode"
    assert fleet.recorded("w1") == 2
    assert len(fleet.pushes("w1")) == 1, fleet.log.read_text()
    fleet.stop_worker()
    fleet.start_worker(subreaper=False)
    fleet.sweep()
    assert len(fleet.pushes("w1")) == 2, fleet.log.read_text()
    assert fleet.recorded("w1") == 3


def test_a_worker_restart_that_brings_no_subreaper_back_is_not_pushed_again(fleet):
    """A sweep with no live session leaves w1's episode open, so the session its restart
    brings back, outside a subreaper again, is recorded and not pushed again before the
    re-notify window: the push's recipient, the manager's session, is unchanged."""
    fleet.start_box()
    fleet.idle("mgr")
    fleet.idle("w1")
    fleet.start_worker(subreaper=False)
    fleet.sweep()
    marker = fleet.state / "w1.subreaper_alerted"
    assert len(fleet.pushes("w1")) == 1 and marker.exists(), fleet.log.read_text()
    fleet.stop_worker()
    fleet.sweep()
    assert marker.exists(), "a sweep with no live session ended the episode"
    fleet.start_worker(subreaper=False)
    fleet.sweep()
    assert len(fleet.pushes("w1")) == 1, fleet.log.read_text()
    assert fleet.recorded("w1") == 2


def test_the_off_switch_stops_the_push_and_not_the_record(fleet):
    """OBSERVABILITY_SUBREAPER_PUSH=0 in w1's bot.conf: the sweep records w1, pushes
    nothing about it, opens no episode and says the push is off. The manager, whose
    own session also runs outside a subreaper and whose switch is on, is pushed in the
    same sweep: the push path ran. With the line gone, the next sweep pushes w1."""
    fleet.conf("w1", "export OBSERVABILITY_SUBREAPER_PUSH=0\n")
    fleet.start_box()
    fleet.idle("mgr")
    fleet.idle("w1")
    fleet.start_worker(subreaper=False)
    result = fleet.sweep()
    assert fleet.pushes("w1") == [], fleet.log.read_text()
    assert fleet.recorded("w1") == 1
    assert OFF_LINE in result.stderr, result.stderr[-3000:]
    assert not (fleet.state / "w1.subreaper_alerted").exists()
    assert len(fleet.pushes("mgr")) == 1, fleet.log.read_text()
    fleet.conf("w1")
    result = fleet.sweep()
    assert len(fleet.pushes("w1")) == 1, fleet.log.read_text()
    assert fleet.recorded("w1") == 2
    assert OFF_LINE not in result.stderr


def test_turning_the_push_off_ends_an_open_episode(fleet):
    """w1's episode is open, pushed once, when OBSERVABILITY_SUBREAPER_PUSH=0 goes into
    its bot.conf: that sweep records w1, pushes nothing and ends the episode. With the
    line gone, the next sweep pushes w1 at once, inside the re-notify window."""
    fleet.start_box()
    fleet.idle("mgr")
    fleet.idle("w1")
    fleet.start_worker(subreaper=False)
    fleet.sweep()
    marker = fleet.state / "w1.subreaper_alerted"
    assert len(fleet.pushes("w1")) == 1 and marker.exists(), fleet.log.read_text()
    fleet.conf("w1", "export OBSERVABILITY_SUBREAPER_PUSH=0\n")
    fleet.sweep()
    assert not marker.exists(), "a sweep with the push off left the episode open"
    assert len(fleet.pushes("w1")) == 1, fleet.log.read_text()
    fleet.conf("w1")
    fleet.sweep()
    assert len(fleet.pushes("w1")) == 2, fleet.log.read_text()
    assert fleet.recorded("w1") == 3


def test_the_re_notify_window_applies_as_it_does_to_every_alert(fleet):
    """The re-notify window (#831) applies: within it the episode pushes once; once the
    episode's marker is older than the window, the next sweep pushes again."""
    fleet.start_box()
    fleet.idle("mgr")
    fleet.idle("w1")
    fleet.start_worker(subreaper=False)
    window = {"FLEET_PULSE_RENOTIFY_AFTER_S": "600"}
    fleet.sweep(**window)
    fleet.sweep(**window)
    assert len(fleet.pushes("w1")) == 1, fleet.log.read_text()
    marker = fleet.state / "w1.subreaper_alerted"
    then = time.time() - 700
    os.utime(marker, (then, then))
    fleet.sweep(**window)
    assert len(fleet.pushes("w1")) == 2, fleet.log.read_text()


def test_the_push_about_the_manager_itself_posts_nothing_to_the_channel(fleet):
    """The manager's own session runs outside a subreaper: the push goes into its own
    pane, telling it to restart itself, and nothing goes to the fleet's channel, whose
    post is for an alert the manager's pane cannot act on. Positive control: the
    manager has also made no tool call for two hours, and that alert about it does
    post to the channel in the same sweep."""
    fleet.start_box()
    fleet.stall("mgr")
    fleet.idle("w1")
    fleet.start_worker(subreaper=False)
    fleet.sweep()
    pushed = fleet.pushes("mgr")
    assert len(pushed) == 1, fleet.log.read_text()
    assert ("This is you. Read your start's bot_subreaper_unavailable first (claudlobby --json"
            " event list --bot mgr --type bot_subreaper_unavailable). It lists newest first, and"
            " only a row from your session's start counts. If your start recorded one, a restart"
            f" may fail as your start did. {CLAUSE} If not, restart yourself") in pushed[0], pushed
    assert "If not, restart yourself with /restart" in pushed[0], pushed
    posts = fleet.posts()
    assert [p for p in posts if "mgr activity_stuck" in p], posts
    assert [p for p in posts if "bot_subreaper_missing" in p] == [], posts
    assert not (fleet.state / "mgr.subreaper_alerted.paged").exists()


def test_a_manager_whose_restart_brings_no_subreaper_back_is_pushed_again_and_then_held(fleet):
    """The manager's own session is the push's recipient, so its restart changes it
    (#831). Its session is replaced between two sweeps, outside a subreaper again, as a
    restart that brings none back leaves it: the next sweep pushes again, since the
    first change is free. A second replacement inside the re-arm window (#1088) is held,
    and once the window has passed the next sweep pushes. Every sweep records it."""
    fleet.start_box()
    fleet.idle("mgr")
    fleet.idle("w1")
    fleet.start_worker(subreaper=True)
    fleet.sweep()
    assert len(fleet.pushes("mgr")) == 1, fleet.log.read_text()
    fleet.restart_box()
    fleet.sweep()
    pushed = fleet.pushes("mgr")
    assert len(pushed) == 2, fleet.log.read_text()
    assert CLAUSE in pushed[1], pushed
    fleet.restart_box()
    fleet.sweep()
    assert len(fleet.pushes("mgr")) == 2, fleet.log.read_text()
    fleet.sweep(FLEET_PULSE_REARM_WINDOW_S="1")
    assert len(fleet.pushes("mgr")) == 3, fleet.log.read_text()
    assert fleet.recorded("mgr") == 4


def test_a_bot_stopped_on_purpose_closes_the_episode_with_its_session():
    """A bot stopped through bot stop, with its session gone, closes its open episodes,
    this one among them, so a start that brings back a session outside its subreaper
    is pushed afresh (the stopped bot's clear list, beside bridge_down's)."""
    src = (LIB / "fleet-pulse.sh").read_text()
    stopped = src[src.index('if [ "$_stopped" -eq 1 ]; then'):src.index("_STOPPED_REMIND_DAYS\" -gt 0")]
    assert "for _ep in bridge_alerted subreaper_alerted activity_alerted held_alerted limit_alerted; do" in stopped
