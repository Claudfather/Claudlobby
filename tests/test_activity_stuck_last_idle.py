"""fleet-pulse times activity_stuck from the later of a bot's last tool call
and the last tick keepalive saw it idle (#2391).

keepalive touches data/.idle and data/.last-idle on every IDLE tick, and the
BUSY tick of a new turn removes .idle alone. A bot that sat idle past the
threshold keeps an old tool-call marker, so timing the stall from that marker
alone paged a bot whose turn had begun seconds after an IDLE tick. A turn
that goes past the threshold with no tool call still pages. Drives the REAL
keepalive tick with recording off (tests/test_keepalive_usage_limit.py's rig)
and the REAL sweep (one stub: tg-post.sh) against a throwaway plane.
"""

from __future__ import annotations

import os
import time

from tests.test_fleet_pulse_events_plane import (
    _await,
    _pulse,
    _pulse_lib,
    _two_dead_bots,
    needs_tmux,
)
from tests.test_fleet_pulse_usage_limit import _events
from tests.test_keepalive_usage_limit import FIXTURES, Rig

MIN = 60
HOUR = 3600


def _age(marker, now, age):
    """Stamp *marker* *age* seconds before *now*."""
    marker.touch()
    os.utime(marker, (now - age, now - age))


def test_the_busy_tick_of_a_new_turn_removes_idle_and_keeps_last_idle(tmp_path):
    """The two ticks before #2391's page: an IDLE tick, then a minute later
    the BUSY tick of a turn with no tool call yet (its pane shows the
    interrupt hint; its last tool call is past keepalive's 180 s window)."""
    r = Rig(tmp_path, {}, armed=False)
    r.show((FIXTURES / "idle-prompt.txt").read_text())
    r.tick()
    assert "IDLE — at prompt" in r.log()
    idle, last_idle = r.data(".idle").stat(), r.data(".last-idle").stat()
    assert abs(idle.st_mtime - last_idle.st_mtime) < 2
    now = int(time.time())
    _age(r.data(".last-tool-call"), now, 5726)
    _age(r.data(".last-idle"), now, 60)
    r.show((FIXTURES / "busy-spinner.txt").read_text())
    r.tick()
    assert "BUSY — active processing" in r.log()
    assert not r.data(".idle").exists()
    assert r.data(".last-idle").stat().st_mtime == now - 60


def _stuck(tmp_path, scratch_plane_env, *, expect, **ages):
    """Stamp each bot's markers the given seconds before now (a marker not
    named stays absent), run one sweep, and check that exactly the bots in
    *expect* paged. Returns now and each page's data."""
    root, paths = _two_dead_bots(tmp_path)
    now = int(time.time())
    for bot, markers in ages.items():
        for name, age in markers.items():
            _age(paths.runtime_bots / bot / "data" / name, now, age)
    r = _pulse(
        root,
        _pulse_lib(tmp_path, tmp_path / "pages"),
        scratch_plane_env=scratch_plane_env,
    )
    assert r.returncode == 0, r.stderr
    _await(
        root, "SELECT COUNT(*) FROM events WHERE event = 'activity_stuck'", len(expect)
    )
    stuck = {
        alias.rsplit("/", 1)[-1]: detail["data"]
        for alias, detail in _events(root, "activity_stuck")
    }
    assert stuck.keys() == expect, stuck
    return now, stuck


@needs_tmux
def test_a_turn_begun_after_a_long_idle_is_not_stuck_before_its_first_tool_call(
    tmp_path, *, scratch_plane_env
):
    """#2391's record: the last tool call 5,731 s before the sweep, the last
    IDLE tick 65 s before it, and .idle gone, removed by the BUSY tick of a
    turn that has made no tool call yet. That is not a hang. w2 has the same
    tool-call marker and no .last-idle, as under a keepalive that predates
    the marker: it still pages on the tool-call clock, which also shows the
    sweep reached Check 5."""
    _, stuck = _stuck(
        tmp_path,
        scratch_plane_env,
        expect={"w2"},
        w1={".last-tool-call": 5731, ".last-idle": 65},
        w2={".last-tool-call": 5731},
    )
    assert stuck["w2"]["last_idle_epoch"] is None


@needs_tmux
def test_a_turn_past_the_threshold_with_no_tool_call_pages_from_the_later_marker(
    tmp_path, *, scratch_plane_env
):
    """A hang still pages: w1 was last seen idle 31 min ago and has made no
    tool call in two hours, past the 1800 s threshold; so has w2, whose last
    tool call 31 min ago came after its last IDLE tick. Each stall runs from
    the later of its two markers."""
    now, stuck = _stuck(
        tmp_path,
        scratch_plane_env,
        expect={"w1", "w2"},
        w1={".last-tool-call": 2 * HOUR, ".last-idle": 31 * MIN},
        w2={".last-tool-call": 31 * MIN, ".last-idle": 2 * HOUR},
    )
    w1, w2 = stuck["w1"], stuck["w2"]
    assert (w1["last_tool_call_epoch"], w1["last_idle_epoch"]) == (
        now - 2 * HOUR,
        now - 31 * MIN,
    )
    assert (w2["last_tool_call_epoch"], w2["last_idle_epoch"]) == (
        now - 31 * MIN,
        now - 2 * HOUR,
    )
    # Timed from 31 min ago, not two hours; the margin covers the sweep's own run.
    for data in (w1, w2):
        assert 31 * MIN <= data["elapsed_seconds"] < 36 * MIN, data
