"""keepalive stamps what it watched, and fleet-pulse judges activity_stuck only
on watched time (#2862).

activity_stuck reads the absence of keepalive's data/.idle stamp as a running
turn, so while a fleet's keepalive was silent the sweep paged it for every bot
that had made no tool call in the threshold, hundreds of pages in one burst.
keepalive now stamps data/.observed on every tick that settles a verdict, and
data/.observed-since on its first such tick and on the first after a silence
longer than OBSERVABILITY_KEEPALIVE_SILENT_THRESHOLD (600 s by default), so a
late tick never restarts a hung turn's clock. fleet-pulse judges
activity_stuck only while .observed is within the active window, times the
stall from the latest of the last tool call, .last-no-turn, .observed-since and
.spawn, and names every live, supervised bot it has seen unwatched past that
threshold in one keepalive_silent alert an episode. Drives the REAL keepalive
tick against a stub pane (tests/test_keepalive_usage_limit.py's rig, recording
off) and the REAL sweep (one stub: tg-post.sh) against a throwaway plane.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.fleet_events import fleet_event_request
from tests.plane_fixtures import F, plane_root, ro
from tests.test_activity_stuck_no_turn import (
    WATCHED,
    _age,
    _rig,
    _show,
    _show_limit,
    _stuck,
)
from tests.test_fleet_pulse_events_plane import _await, _pulse, _pulse_lib, needs_tmux
from tests.test_keepalive_usage_limit import Rig

MIN = 60
HOUR = 3600
DAY = 24 * HOUR

# How a tick comes to read each verdict.
VERDICTS = {
    "BUSY": lambda r: _show(r, "busy-spinner.txt"),
    "IDLE": lambda r: _show(r, "idle-prompt.txt"),
    "HELD": lambda r: _show(r, "input-held-cr.txt"),
    "UNKNOWN": lambda r: _show(r, "unknown-output.txt"),
    "LIMIT": _show_limit,
}


def _mtime(path: Path) -> int:
    return int(path.stat().st_mtime)


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# --- keepalive's stamps ---------------------------------------------------------------


@pytest.mark.parametrize("verdict", VERDICTS)
def test_every_verdict_stamps_the_watch_and_only_a_long_silence_moves_its_start(
    tmp_path, verdict
):
    """The first tick stamps .observed and .observed-since, whatever its
    verdict. After a 5 min silence, under the 600 s default, the next tick
    stamps .observed alone, so the watch keeps its start; the tick after a
    15 min silence starts a new watch."""
    r = _rig(tmp_path)
    VERDICTS[verdict](r)
    r.tick()
    assert f"{verdict} — " in r.log()
    now = int(time.time())
    assert now - _mtime(r.data(".observed")) < 5
    assert now - _mtime(r.data(".observed-since")) < 5
    _age(r.data(".observed-since"), now, 2 * HOUR)
    _age(r.data(".observed"), now, 5 * MIN)
    r.tick()
    assert int(time.time()) - _mtime(r.data(".observed")) < 5
    assert _mtime(r.data(".observed-since")) == now - 2 * HOUR
    _age(r.data(".observed"), now, 15 * MIN)
    r.tick()
    assert int(time.time()) - _mtime(r.data(".observed-since")) < 5


@pytest.mark.parametrize(
    "value, silence, restarts",
    [("1200", 15 * MIN, False), ("0", 15 * MIN, True), ("0", 5 * MIN, False),
     ("ten", 15 * MIN, True)],
)
def test_the_watch_restarts_only_after_the_bots_own_threshold(
    tmp_path, value, silence, restarts
):
    """keepalive reads the bot's OBSERVABILITY_KEEPALIVE_SILENT_THRESHOLD, as
    fleet-pulse does: at 1200 a 15 min silence keeps the watch's start. 0
    turns the alert off, not the watch, which then reads the 600 s default, as
    a value that is not a number does."""
    r = _rig(tmp_path)
    with open(r.bot / "bot.conf", "a", encoding="utf-8") as conf:
        conf.write(f"OBSERVABILITY_KEEPALIVE_SILENT_THRESHOLD={value}\n")
    _show(r, "idle-prompt.txt")
    r.tick()
    now = int(time.time())
    _age(r.data(".observed-since"), now, 2 * HOUR)
    _age(r.data(".observed"), now, silence)
    r.tick()
    assert (_mtime(r.data(".observed-since")) != now - 2 * HOUR) is restarts


def test_a_tick_on_a_dead_session_stamps_neither(tmp_path):
    """keepalive's dead-session path reads no pane and settles no verdict: it
    restarts the bot, or leaves a boot, a crash loop or a bot with no unit
    alone, and stamps nothing. A bot with no session goes unwatched, and its
    own alerts speak for it."""
    r = _rig(tmp_path)
    _show(r, "idle-prompt.txt")
    r.tick()
    now = int(time.time())
    for name in (".observed", ".observed-since"):
        _age(r.data(name), now, HOUR)
    dead = tmp_path / "tmux-dead"
    dead.write_text('#!/bin/bash\ncase " $* " in *" has-session "*) exit 1 ;; esac\nexit 0\n')
    dead.chmod(0o755)
    r.env["TMUX_BIN"] = str(dead)
    r.tick()
    assert "SKIP — bot is de-enrolled" in r.log(), r.log()
    for name in (".observed", ".observed-since"):
        assert _mtime(r.data(name)) == now - HOUR, name


# --- activity_stuck charges only watched time -------------------------------------------


@needs_tmux
@pytest.mark.parametrize(
    "markers",
    [{".observed": 5 * MIN}, {".spawn": 5 * MIN}, {".observed-since": 5 * MIN}],
    ids=["unwatched-5-min", "new-session", "new-watch"],
)
def test_activity_stuck_charges_only_watched_time_in_the_current_session(
    tmp_path, markers, *, scratch_plane_env
):
    """Both bots made their last tool call two hours ago, with no idle tick
    since. w2, watched all along, pages, timed from that tool call. w1 does
    not: keepalive has not watched it within the active window (180 s), or
    its session, or keepalive's current watch, began 5 min ago, so no 30 min
    stall can be charged to it yet."""
    then, stuck = _stuck(
        tmp_path,
        scratch_plane_env,
        expect={"w2"},
        w1={".last-tool-call": 2 * HOUR, **markers},
        w2={".last-tool-call": 2 * HOUR},
    )
    w2 = stuck["w2"]
    assert w2["observed_since_epoch"] == then - WATCHED[".observed-since"]
    assert w2["spawn_epoch"] is None
    assert 2 * HOUR <= w2["elapsed_seconds"] < 2 * HOUR + 5 * MIN, w2


@needs_tmux
def test_a_hang_across_a_short_gap_pages_on_time_and_a_long_silence_restarts_its_clock(
    tmp_path, *, scratch_plane_env
):
    """A turn that hung 31 min ago, past the 30 min threshold, on a bot that
    keepalive has watched for two hours. Its ticks stopped for a while and came
    back a moment ago. w1's silence lasted 5 min, under the 600 s that pages
    keepalive_silent: the tick after it keeps the watch's start, so the sweep
    pages the hang on time, timed from its last tool call (had the late tick
    restarted the clock, nothing would page for another half hour). w2's
    silence lasted 15 min: the tick after it starts a new watch, which restarts
    the hang's clock, so it does not page yet."""
    now = int(time.time())
    rigs = {}
    for bot, silence in (("w1", 5 * MIN), ("w2", 15 * MIN)):
        (tmp_path / bot).mkdir()
        r = Rig(tmp_path / bot, {}, armed=False)
        _show(r, "busy-spinner.txt")
        _age(r.data(".last-tool-call"), now, 31 * MIN)
        r.tick()
        assert "BUSY — " in r.log()
        _age(r.data(".observed-since"), now, 2 * HOUR)
        _age(r.data(".observed"), now, silence)
        r.tick()
        rigs[bot] = r
    assert _mtime(rigs["w1"].data(".observed-since")) == now - 2 * HOUR
    assert int(time.time()) - _mtime(rigs["w2"].data(".observed-since")) < 5
    _, stuck = _stuck(
        tmp_path,
        scratch_plane_env,
        expect={"w1"},
        watched=False,
        data={bot: r.bot / "data" for bot, r in rigs.items()},
        w1={".last-tool-call": 31 * MIN},
        w2={".last-tool-call": 31 * MIN},
    )
    assert stuck["w1"]["observed_since_epoch"] == now - 2 * HOUR
    assert 31 * MIN <= stuck["w1"]["elapsed_seconds"] < 36 * MIN, stuck["w1"]


# --- keepalive_silent -------------------------------------------------------------------


class Fleet:
    """Fleet f on a throwaway plane. Each bot is live and supervised unless told
    otherwise: a unit file and a LaunchAgent in the scene's HOME (what
    svc_is_registered reads on each OS), systemctl and launchctl answering
    active, and a tmux session on the bot's own socket."""

    def __init__(self, tmp_path: Path):
        self.root = plane_root(tmp_path, initialize=True)
        (self.root / ".claudlobby-harness-root").write_text(str(self.root.resolve()))
        # The overdue reader refuses a plane that holds no bot of the fleet:
        # seed the roster through the real write door, as a live plane holds it.
        emit_batch(self.root, [fleet_event_request(
            "pane_stuck", fleet=F, subject_kind="actor", subject=f"bot:{F}/roster-seed",
            source="pulse", data={})], require_commit=True)
        self.home = self.root / "home"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name in ("systemctl", "launchctl"):
            stub = self.bin / name
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        # a short path: a tmux socket's path is limited to about 100 bytes
        self.tmux = Path(tempfile.mkdtemp(prefix="t2862-", dir="/tmp"))
        self.capture = tmp_path / "tg.log"
        self.libdir = _pulse_lib(tmp_path, self.capture)
        self.bots = self.root / "local" / F / "runtime" / "bots"
        self.bots.mkdir(parents=True)
        self.state = self.root / "state" / "pulse"
        self.names: list[str] = []

    def tmux_cmd(self, *argv):
        return subprocess.run(["tmux", *argv], env={**os.environ, "TMUX_TMPDIR": str(self.tmux)},
                              capture_output=True, text=True, timeout=30)

    def bot(self, name, *, unit=True, session=True, service=True, stopped=False, conf=""):
        d = self.bots / name
        (d / "data").mkdir(parents=True)
        label = f"com.t2862.{name}"
        text = f"BOT_NAME={name}\nTMUX_SOCKET=t2862-{name}\n{conf}"
        if service:
            text += f"BOT_SERVICE={label}\n"
        (d / "bot.conf").write_text(text)
        if unit:
            for path in (self.home / ".config/systemd/user" / f"{label}.service",
                         self.home / "Library/LaunchAgents" / f"{label}.plist"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")
        if stopped:
            at = int(time.time()) - HOUR
            (d / "data" / ".stopped").write_text(json.dumps({
                "by": f"bot:{F}/w1", "reason": "parked for the test", "request_id": "r-2862",
                "stopped_at": _iso(at), "stopped_epoch": at}, sort_keys=True) + "\n")
        if session:
            r = self.tmux_cmd("-L", f"t2862-{name}", "new-session", "-d", "-s", name, "sleep 3600")
            assert r.returncode == 0, r.stderr
        self.names.append(name)
        (self.root / "local" / F / "fleet.yaml").write_text(
            f"fleet:\n  name: {F}\n  manager: {self.names[0]}\n  service_prefix: com.t2862\n"
            "  bots:\n" + "".join(f"    {n}:\n      expertise: [software-engineering]\n"
                                  for n in self.names))
        return d

    def data(self, name, marker) -> Path:
        return self.bots / name / "data" / marker

    def unwatched(self, name, *, ago):
        """keepalive last watched *name* 20 min ago, and the sweep first saw it
        unwatched *ago* seconds back."""
        now = int(time.time())
        _age(self.data(name, ".observed"), now, 20 * MIN)
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / f"{F}.{name}.unwatched").write_text(f"{now - ago}\n")

    def sweep(self, scratch_plane_env):
        r = _pulse(self.root, self.libdir, scratch_plane_env=scratch_plane_env,
                   PATH=f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                   TMUX_TMPDIR=str(self.tmux))
        assert r.returncode == 0, r.stderr[-3000:]
        return r

    def pages(self) -> list[str]:
        text = self.capture.read_text() if self.capture.exists() else ""
        return [line for line in text.splitlines() if "[keepalive_silent]" in line]

    def rows(self, event) -> list:
        with ro(self.root) as conn:
            return conn.execute("SELECT subject_alias FROM events WHERE event = ?",
                                (event,)).fetchall()

    def close(self):
        for name in self.names:
            self.tmux_cmd("-L", f"t2862-{name}", "kill-server")
        shutil.rmtree(self.tmux, ignore_errors=True)


@pytest.fixture
def fleet(tmp_path):
    f = Fleet(tmp_path)
    try:
        yield f
    finally:
        f.close()


def _named(page: str) -> list[str]:
    return re.findall(r"(\w+) \((?:last watched|no watch recorded)", page)


@needs_tmux
def test_a_silent_keepalive_pages_once_naming_its_bots_and_no_activity_stuck(
    fleet, *, scratch_plane_env
):
    """The burst, replayed: keepalive stopped 20 min ago for the whole fleet,
    and neither bot has made a tool call in two hours, with no idle tick since.
    No activity_stuck. The sweep's first sighting of the silence starts its
    wait, so nothing pages yet: a sweep that runs before keepalive's first tick
    after a boot pages nothing. Once the sweep has seen both unwatched past
    600 s, one keepalive_silent names both, with when each was last watched,
    and a later sweep sends nothing new."""
    now = int(time.time())
    for name in ("w1", "w2"):
        fleet.bot(name)
        _age(fleet.data(name, ".last-tool-call"), now, 2 * HOUR)
        _age(fleet.data(name, ".observed-since"), now, DAY)
        _age(fleet.data(name, ".observed"), now, 20 * MIN)
    fleet.sweep(scratch_plane_env)
    assert fleet.pages() == []
    seen = sorted(p.name for p in fleet.state.glob(f"{F}.*.unwatched"))
    assert seen == [f"{F}.w1.unwatched", f"{F}.w2.unwatched"], seen
    for name in ("w1", "w2"):
        fleet.unwatched(name, ago=11 * MIN)
    fleet.sweep(scratch_plane_env)
    pages = fleet.pages()
    assert len(pages) == 1, pages
    w1, w2 = (_iso(_mtime(fleet.data(n, ".observed"))) for n in ("w1", "w2"))
    assert f"has not watched w1 (last watched {w1}); w2 (last watched {w2})" in pages[0], pages
    assert _await(fleet.root, "SELECT COUNT(*) FROM events WHERE event = 'keepalive_silent'", 1) == 1
    fleet.sweep(scratch_plane_env)
    assert len(fleet.pages()) == 1, fleet.pages()
    assert fleet.rows("activity_stuck") == []


@needs_tmux
def test_any_unwatched_bot_is_named_and_one_that_joins_is_sent_at_once(
    fleet, *, scratch_plane_env
):
    """keepalive watches w2 and has not watched w1 for 20 min: the alert names
    w1 alone. Then w2 goes unwatched as well: the alert goes again at once,
    naming both, with hours of its re-notify window still to run. Then
    keepalive watches both again, its first tick after the silence starting a
    new watch: the episode ends, with nothing left of it."""
    now = int(time.time())
    for name in ("w1", "w2"):
        fleet.bot(name)
        _age(fleet.data(name, ".observed-since"), now, DAY)
        _age(fleet.data(name, ".observed"), now, MIN)
    fleet.unwatched("w1", ago=11 * MIN)
    fleet.sweep(scratch_plane_env)
    pages = fleet.pages()
    assert len(pages) == 1 and _named(pages[0]) == ["w1"], pages
    fleet.unwatched("w2", ago=11 * MIN)
    fleet.sweep(scratch_plane_env)
    pages = fleet.pages()
    assert len(pages) == 2 and _named(pages[1]) == ["w1", "w2"], pages
    for name in ("w1", "w2"):
        for marker in (".observed", ".observed-since"):
            fleet.data(name, marker).touch()
    fleet.sweep(scratch_plane_env)
    assert len(fleet.pages()) == 2, fleet.pages()
    left = sorted(p.name for p in fleet.state.glob(f"{F}.*") if "unwatched" in p.name
                  or "keepalive_silent" in p.name)
    assert left == [], left


@needs_tmux
def test_a_bot_keepalive_rightly_skips_is_never_named_and_zero_turns_the_alert_off(
    fleet, *, scratch_plane_env
):
    """keepalive last watched every bot here 20 min ago, and the sweep first saw
    each unwatched 11 min ago. Named: w1, live and supervised, and w6, whose
    threshold is not a number and reads as 600. Never named, since keepalive
    does not watch them and their own checks report them: w2, stopped through
    bot stop; w3, its unit file gone (unit_missing); w4, with no session
    (session_missing); w5, with no service. w7's threshold is 0: not named, and
    the sweep says so on stderr. The sweep keeps no sighting of a bot it does
    not watch for."""
    specs = {
        "w1": {},
        "w2": {"unit": False, "session": False, "stopped": True},
        "w3": {"unit": False},
        "w4": {"session": False},
        "w5": {"service": False},
        "w6": {"conf": "OBSERVABILITY_KEEPALIVE_SILENT_THRESHOLD=ten\n"},
        "w7": {"conf": "OBSERVABILITY_KEEPALIVE_SILENT_THRESHOLD=0\n"},
    }
    for name, spec in specs.items():
        fleet.bot(name, **spec)
        fleet.unwatched(name, ago=11 * MIN)
    r = fleet.sweep(scratch_plane_env)
    pages = fleet.pages()
    assert len(pages) == 1 and _named(pages[0]) == ["w1", "w6"], pages
    assert re.search(
        r"keepalive has not watched w7 \(last watched [0-9TZ:-]+\); keepalive_silent is off "
        r"\(OBSERVABILITY_KEEPALIVE_SILENT_THRESHOLD=0\)", r.stderr), r.stderr[-3000:]
    kept = sorted(p.name for p in fleet.state.glob(f"{F}.*.unwatched"))
    assert kept == [f"{F}.{n}.unwatched" for n in ("w1", "w6", "w7")], kept
