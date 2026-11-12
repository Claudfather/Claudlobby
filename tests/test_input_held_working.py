"""A held input box pages input_held on a bot that keeps working (#2854).

keepalive read the box only after 180 s with no tool call, asked BUSY before HELD,
and removed data/.held on every BUSY tick; fleet-pulse paged only a .held newer than
the last tool call (#2071). So a box holding a delivery whose Enter did not take, on
a bot that kept working, raised nothing, while the send path refused every later
message into it. keepalive now reads the box on every tick with the send path's own
test (pane_is_held or pane_box_end_held, on the whole capture): data/.held keeps the
epoch the box was first seen holding text and goes once the box is empty, with
data/.held-turn beside it while a turn is running. fleet-pulse pages input_held once
the hold passes OBSERVABILITY_INPUT_HELD_THRESHOLD, whatever the tool calls, and 0
turns the page off. Drives the REAL keepalive tick against a stub pane
(tests/test_keepalive_usage_limit.py's rig, recording off) and the REAL sweep (one
stub: tg-post.sh) against a throwaway plane. No key goes into a live pane.
"""

from __future__ import annotations

import os
import subprocess
import time

from tests.test_activity_stuck_no_turn import _age, _rig, _show
from tests.test_fleet_pulse_events_plane import (
    _await,
    _pulse,
    _pulse_lib,
    _two_dead_bots,
    needs_tmux,
)
from tests.test_fleet_pulse_usage_limit import _events
from tests.test_keepalive_usage_limit import FIXTURES, LIB

MIN = 60
HOUR = 3600


def _working(r, age=10):
    """A tool call *age* seconds ago: the marker decides BUSY."""
    _age(r.data(".last-tool-call"), int(time.time()), age)


def _first(r) -> int:
    """The hold's start. The marker stays the bare epoch #2070's readers take."""
    text = r.data(".held").read_text()
    assert text.strip().isdigit(), text
    return int(text)


def _tall_box() -> str:
    """A box taller than the pane, in the shape tests/test_pane_send_verified.sh
    measured (#2669): its top, prompt line included, is gone, and what stays in
    view is its last lines, its bottom rule and the footer."""
    lines = [f"  line {i} of a message taller than the pane" for i in range(9, 30)]
    footer = ["  ⏵⏵ auto mode on (shift+tab to cycle)", " " * 52 + "Not logged in"]
    return "\n".join([*lines, "─" * 80, *footer]) + "\n"


def test_a_box_held_under_a_running_turn_is_marked_from_its_first_tick(tmp_path):
    """A bot that keeps working: its last tool call 10 s ago, so the marker
    decides BUSY, and text sits in its box under a running turn. The tick
    reads the box anyway: .held holds the first tick's epoch and .held-turn
    says a turn is running; the next tick keeps that epoch and re-stamps the
    marker. Every verdict stays BUSY. Once the box is empty, both go."""
    r = _rig(tmp_path)
    _working(r)
    _show(r, "input-typed-busy.txt")
    r.tick()
    first = _first(r)
    assert abs(first - time.time()) < 10, first
    assert r.data(".held-turn").exists()
    _age(r.data(".held"), int(time.time()), 60)
    _working(r)
    r.tick()
    assert _first(r) == first
    assert time.time() - r.data(".held").stat().st_mtime < 10
    _show(r, "busy-spinner.txt")
    r.tick()
    assert not r.data(".held").exists() and not r.data(".held-turn").exists()
    log = r.log()
    assert log.count("BUSY — ") == 3 and " HELD " not in log, log


def test_a_hold_keeps_its_start_while_a_turn_begins_and_ends(tmp_path):
    """The hold is the box's, not the verdict's: text held with no turn
    running (HELD, no .held-turn), then a turn runs with the text still there
    (.held-turn), then the turn ends with it still there: one start."""
    r = _rig(tmp_path)
    _show(r, "input-held-cr.txt")
    r.tick()
    assert " HELD " in r.log()
    first = _first(r)
    assert not r.data(".held-turn").exists()
    _working(r)
    _show(r, "input-typed-busy.txt")
    r.tick()
    assert _first(r) == first and r.data(".held-turn").exists()
    _show(r, "input-held-cr.txt")
    r.tick()
    assert _first(r) == first and not r.data(".held-turn").exists()


def test_a_box_taller_than_the_pane_is_held_on_a_working_bot(tmp_path):
    """No prompt line in view, so no HELD verdict from the last ten lines; the
    send path refuses on the box's bottom edge (pane_box_end_held), and so does
    the tick's read. A turn above such a box is out of view: no .held-turn."""
    r = _rig(tmp_path)
    _working(r)
    r.show(_tall_box())
    r.tick()
    assert "BUSY — " in r.log()
    _first(r)
    assert not r.data(".held-turn").exists()


def test_a_capture_that_fails_changes_neither_marker(tmp_path):
    """A capture that failed says nothing about the box: the hold keeps its
    start, its stamp and its turn marker (fleet-pulse's freshness window ends
    a hold keepalive stops seeing)."""
    r = _rig(tmp_path)
    _working(r)
    _show(r, "input-typed-busy.txt")
    r.tick()
    first = _first(r)
    stamp = int(time.time()) - 60
    _age(r.data(".held"), stamp + 60, 60)
    failing = tmp_path / "tmux-fails"
    failing.write_text('#!/bin/bash\ncase " $* " in *" has-session "*) exit 0 ;; esac\nexit 1\n')
    failing.chmod(0o755)
    r.env["TMUX_BIN"] = str(failing)
    _working(r)
    r.tick()
    assert _first(r) == first and r.data(".held-turn").exists()
    assert r.data(".held").stat().st_mtime == stamp


_SEND_PATH = r"""
. "$1/lib-common.sh" >/dev/null 2>&1
set +e +u
shift
for f in "$@"; do
    t=$(cat "$f")
    h=-; b=-
    if pane_is_held "$t" || pane_box_end_held "$t"; then h=held; fi
    if pane_is_busy "$t"; then b=busy; fi
    printf '%s %s %s\n' "${f##*/}" "$h" "$b"
done
"""


def test_every_pane_fixture_is_held_exactly_where_the_send_path_refuses(tmp_path):
    """Parity: on every pane fixture the tick's .held is the send path's
    held refusal (pane_send_verified's pane_is_held or pane_box_end_held on
    the whole capture), and .held-turn is that and a running turn on screen.
    Every HELD verdict leaves .held, so the marker never reads less than the
    verdict did. These ticks make no tool call; the tests above show a tick
    whose tool-call marker decides BUSY reads the same box."""
    frames = sorted(FIXTURES.glob("*.txt"))
    assert len(frames) >= 50, len(frames)
    oracle = subprocess.run(
        ["bash", "-c", _SEND_PATH, "send-path", str(LIB), *map(str, frames)],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(tmp_path)},
    )
    assert oracle.returncode == 0, oracle.stderr
    want = {}
    for line in oracle.stdout.splitlines():
        name, held, busy = line.split()
        want[name] = (held == "held", held == "held" and busy == "busy")
    assert len(want) == len(frames), oracle.stdout
    r = _rig(tmp_path)
    got, verdict_held = {}, []
    for frame in frames:
        before = len(r.log())
        r.show(frame.read_text())
        r.tick()
        if " HELD — " in r.log()[before:]:
            verdict_held.append(frame.name)
        got[frame.name] = (r.data(".held").exists(), r.data(".held-turn").exists())
    assert got == want
    assert verdict_held and all(got[name][0] for name in verdict_held), verdict_held
    assert got["input-typed-busy.txt"] == (True, True)
    assert got["compact-at-queued-user-prompt-submit.txt"] == (True, True)
    assert got["input-held-cr.txt"] == (True, False)
    for quiet in ("input-queued-turn.txt", "thinking-turn.txt", "busy-spinner.txt",
                  "menu-option.txt", "usage-limit-menu.txt", "idle-prompt.txt"):
        assert got[quiet] == (False, False), quiet


def _hold(data, now, *, first, stamp=0, turn=False):
    """keepalive's markers for a box first seen holding text *first* seconds
    before *now* and re-stamped *stamp* seconds before it, under a running
    turn or not."""
    (data / ".held").write_text(f"{now - first}\n")
    os.utime(data / ".held", (now - stamp, now - stamp))
    if turn:
        (data / ".held-turn").touch()


def _sweep(tmp_path, root, scratch_plane_env):
    lib = tmp_path / "lib"
    if not lib.exists():
        _pulse_lib(tmp_path, tmp_path / "pages")
    p = _pulse(root, lib, scratch_plane_env=scratch_plane_env)
    assert p.returncode == 0, p.stderr
    return p


def _who(events):
    return [alias.rsplit("/", 1)[-1] for alias, _ in events]


@needs_tmux
def test_a_hold_past_the_threshold_pages_however_much_the_bot_works(
    tmp_path, *, scratch_plane_env
):
    """Both bots keep working: each made a tool call 10 s ago, after keepalive
    last re-stamped .held (30 s ago), the order that kept #2071's gate shut.
    w1's box has held text for 6 min under a running turn: one input_held,
    with the hold's start, its length and the turn. w2's has held for 2 min,
    under the threshold: nothing. Neither pages activity_stuck."""
    root, paths = _two_dead_bots(tmp_path)
    now = int(time.time())
    for bot, first in (("w1", 6 * MIN), ("w2", 2 * MIN)):
        data = paths.runtime_bots / bot / "data"
        _hold(data, now, first=first, stamp=30, turn=True)
        _age(data / ".last-tool-call", now, 10)
    _sweep(tmp_path, root, scratch_plane_env)
    _await(root, "SELECT COUNT(*) FROM events WHERE event = 'input_held'", 1)
    held = _events(root, "input_held")
    assert _who(held) == ["w1"], held
    data = held[0][1]["data"]
    assert data["held_since_epoch"] == now - 6 * MIN and data["turn"] == "running", data
    assert 6 * MIN <= data["held_seconds"] < 11 * MIN, data
    assert _events(root, "activity_stuck") == []


@needs_tmux
def test_a_threshold_of_zero_turns_the_page_off(tmp_path, *, scratch_plane_env):
    """OBSERVABILITY_INPUT_HELD_THRESHOLD=0 turns input_held off. w1's box, no
    turn running, has held text for 2 h under that 0: no input_held and no
    alert, one stderr line saying so, and still no activity_stuck, since a held
    box with no turn running is not a hang. w2's same hold, under the default,
    pages: the control."""
    root, paths = _two_dead_bots(tmp_path)
    with open(paths.runtime_bots / "w1" / "bot.conf", "a", encoding="utf-8") as conf:
        conf.write("OBSERVABILITY_INPUT_HELD_THRESHOLD=0\n")
    now = int(time.time())
    for bot in ("w1", "w2"):
        data = paths.runtime_bots / bot / "data"
        _hold(data, now, first=2 * HOUR)
        _age(data / ".last-tool-call", now, 3 * HOUR)
    p = _sweep(tmp_path, root, scratch_plane_env)
    _await(root, "SELECT COUNT(*) FROM events WHERE event = 'input_held'", 1)
    held = _events(root, "input_held")
    assert _who(held) == ["w2"], held
    assert held[0][1]["data"]["turn"] == "none"
    assert "input_held is off (OBSERVABILITY_INPUT_HELD_THRESHOLD=0)" in p.stderr, p.stderr
    alerted = sorted(m.name for m in (root / "state" / "pulse").glob("*.held_alerted"))
    assert len(alerted) == 1 and "w2" in alerted[0], alerted
    assert _events(root, "activity_stuck") == []


@needs_tmux
def test_a_hung_turn_still_pages_activity_stuck_beside_its_held_box(
    tmp_path, *, scratch_plane_env
):
    """A held box does not stand in for a hang it sits under. Both bots made
    their last tool call 2 h ago and both boxes have held text for 40 min.
    w1's turn still runs above its box: activity_stuck for the turn, and
    input_held naming the running turn. w2 has no turn running: input_held
    alone, as #2070 ruled."""
    root, paths = _two_dead_bots(tmp_path)
    now = int(time.time())
    for bot, turn in (("w1", True), ("w2", False)):
        data = paths.runtime_bots / bot / "data"
        _hold(data, now, first=40 * MIN, turn=turn)
        _age(data / ".last-tool-call", now, 2 * HOUR)
    _sweep(tmp_path, root, scratch_plane_env)
    _await(root, "SELECT COUNT(*) FROM events WHERE event = 'activity_stuck'", 1)
    assert _who(_events(root, "activity_stuck")) == ["w1"]
    _await(root, "SELECT COUNT(*) FROM events WHERE event = 'input_held'", 2)
    held = _events(root, "input_held")
    turns = {alias.rsplit("/", 1)[-1]: d["data"].get("turn") for alias, d in held}
    assert turns == {"w1": "running", "w2": "none"}, held


@needs_tmux
def test_a_stale_hold_pages_nothing_and_a_cleared_box_ends_the_episode(
    tmp_path, *, scratch_plane_env
):
    """keepalive stopped re-stamping w2's .held 6 min ago, past the 300 s
    window: it says nothing about the box now, so no page, however old the
    hold. w1's hold pages and opens its episode; once its box is clear (no
    .held), the next sweep closes the episode, so a new hold pages again."""
    root, paths = _two_dead_bots(tmp_path)
    now = int(time.time())
    _hold(paths.runtime_bots / "w1" / "data", now, first=10 * MIN)
    _hold(paths.runtime_bots / "w2" / "data", now, first=HOUR, stamp=6 * MIN)
    _sweep(tmp_path, root, scratch_plane_env)
    _await(root, "SELECT COUNT(*) FROM events WHERE event = 'input_held'", 1)
    assert _who(_events(root, "input_held")) == ["w1"]
    pulse = root / "state" / "pulse"
    assert [m.name for m in pulse.glob("*w1*.held_alerted")], sorted(os.listdir(pulse))
    assert not list(pulse.glob("*w2*.held_alerted"))
    (paths.runtime_bots / "w1" / "data" / ".held").unlink()
    _sweep(tmp_path, root, scratch_plane_env)
    assert not list(pulse.glob("*w1*.held_alerted")), sorted(os.listdir(pulse))
