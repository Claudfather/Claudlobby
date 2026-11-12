"""fleet-pulse times activity_stuck from the later of a bot's last tool call
and the last tick keepalive saw no turn running (#2391).

keepalive stamps data/.last-no-turn on every IDLE, HELD and LIMIT tick, and
the BUSY tick of a new turn removes .idle, .held and .limit alone. A bot that
waited past the threshold keeps an old tool-call marker, so timing the stall
from that marker alone paged a bot whose turn had begun seconds after such a
tick. A turn that goes past the threshold with no tool call still pages, and
messages reaching its running turn do not move the clock. Drives the REAL
keepalive tick with recording off (tests/test_keepalive_usage_limit.py's rig)
and the REAL sweep (one stub: tg-post.sh) against a throwaway plane.
"""

from __future__ import annotations

import os
import shutil
import time

import pytest

from tests.test_fleet_pulse_events_plane import (
    _await,
    _pulse,
    _pulse_lib,
    _two_dead_bots,
    needs_tmux,
)
from tests.test_fleet_pulse_usage_limit import _events
from tests.test_keepalive_usage_limit import FIXTURES, Rig, _frame, _future_reset

MIN = 60
HOUR = 3600


def _age(marker, now, age):
    """Stamp *marker* *age* seconds before *now*."""
    marker.touch()
    os.utime(marker, (now - age, now - age))


def _stamped(r):
    """The tick just run stamped .last-no-turn (_age would create it).
    Returns now."""
    now = int(time.time())
    assert now - r.data(".last-no-turn").stat().st_mtime < 5
    return now


def _rig(tmp_path):
    """A keepalive rig, resume off, beside the sweep's scene."""
    (tmp_path / "ka").mkdir()
    return Rig(tmp_path / "ka", {}, armed=False)


def _show(r, frame):
    r.show((FIXTURES / frame).read_text())


def _show_limit(r):
    """A usage-limit stop: the hook's record, and the limit still the pane's
    last word."""
    hit, reset = _future_reset()
    r.show(_frame("held", reset))
    r.hook_record(hit)


# How a tick comes to read each verdict that sees no turn running.
NO_TURN = {
    "IDLE": lambda r: _show(r, "idle-prompt.txt"),
    "HELD": lambda r: _show(r, "input-held-cr.txt"),
    "LIMIT": _show_limit,
}


def _stuck(tmp_path, scratch_plane_env, *, expect, data=None, **ages):
    """Copy each bot's data/ from *data* (a keepalive rig's), stamp the named
    markers the given seconds before now (any other keeps its copied mtime,
    or stays absent), run one sweep, and check that exactly the bots in
    *expect* paged. Returns now and each page's data."""
    root, paths = _two_dead_bots(tmp_path)
    for bot, src in (data or {}).items():
        shutil.copytree(src, paths.runtime_bots / bot / "data", dirs_exist_ok=True)
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
@pytest.mark.parametrize("verdict", NO_TURN)
def test_a_turn_begun_after_a_tick_with_no_turn_running_is_timed_from_that_tick(
    tmp_path, verdict, *, scratch_plane_env
):
    """#2391's record, after each verdict that sees no turn running: that
    tick, then a minute later the BUSY tick of a turn with no tool call yet
    (keepalive reads its pane, which shows the interrupt hint), then a sweep
    5,731 s after the last tool call. The BUSY tick removes .idle, .held and
    .limit and keeps .last-no-turn, so the sweep times the turn from that
    tick and does not page. w2 has the same tool-call marker and no
    .last-no-turn, as under a keepalive that predates the marker: it still
    pages on the tool-call clock, which also shows the sweep reached Check 5."""
    r = _rig(tmp_path)
    NO_TURN[verdict](r)
    r.tick()
    assert f"{verdict} — " in r.log()
    now = _stamped(r)
    _age(r.data(".last-no-turn"), now, 60)
    _show(r, "busy-spinner.txt")
    r.tick()
    assert "BUSY — " in r.log()
    assert not any(r.data(m).exists() for m in (".idle", ".held", ".limit"))
    assert r.data(".last-no-turn").stat().st_mtime == now - 60
    _, stuck = _stuck(
        tmp_path,
        scratch_plane_env,
        expect={"w2"},
        data={"w1": r.bot / "data"},
        w1={".last-tool-call": 5731},
        w2={".last-tool-call": 5731},
    )
    assert stuck["w2"]["last_no_turn_epoch"] is None


@needs_tmux
def test_a_turn_past_the_threshold_pages_however_many_messages_reach_it(
    tmp_path, *, scratch_plane_env
):
    """A hang still pages. w1 was last seen with no turn running 31 min ago
    and has made no tool call in two hours. Since then three messages reached
    its running turn, each typed into its box (on its last ten lines that
    frame reads HELD, with the turn above them) and then queued behind the
    turn. None of those ticks stamps .last-no-turn, so the sweep pages, timed
    from the last tick with no turn running. w2's last tool call, 31 min ago,
    came after its last such tick: it pages from the tool call."""
    r = _rig(tmp_path)
    _show(r, "idle-prompt.txt")
    r.tick()
    now = _stamped(r)
    _age(r.data(".last-no-turn"), now, 31 * MIN)
    for _ in range(3):
        for frame in ("input-typed-busy.txt", "input-queued-turn.txt"):
            _show(r, frame)
            r.tick()
    assert r.log().count("BUSY — ") >= 3
    assert r.data(".last-no-turn").stat().st_mtime == now - 31 * MIN
    assert not any(r.data(m).exists() for m in (".idle", ".held"))
    then, stuck = _stuck(
        tmp_path,
        scratch_plane_env,
        expect={"w1", "w2"},
        data={"w1": r.bot / "data"},
        w1={".last-tool-call": 2 * HOUR},
        w2={".last-tool-call": 31 * MIN, ".last-no-turn": 2 * HOUR},
    )
    w1, w2 = stuck["w1"], stuck["w2"]
    assert (w1["last_tool_call_epoch"], w1["last_no_turn_epoch"]) == (
        then - 2 * HOUR,
        now - 31 * MIN,
    )
    assert (w2["last_tool_call_epoch"], w2["last_no_turn_epoch"]) == (
        then - 31 * MIN,
        then - 2 * HOUR,
    )
    # Timed from 31 min ago, not two hours; the margin covers the run itself.
    for data in (w1, w2):
        assert 31 * MIN <= data["elapsed_seconds"] < 36 * MIN, data


@needs_tmux
def test_a_usage_limit_hold_then_unknown_ticks_is_timed_from_the_last_limit_tick(
    tmp_path, *, scratch_plane_env
):
    """The shape of four pages on 2026-10-15: LIMIT ticks for 35 min, then
    UNKNOWN ticks, the first sweep 63 s after the last LIMIT tick and 22,268 s
    after the last tool call. The UNKNOWN tick removes .limit, so the sweep
    reads the stall clock, and stamps nothing, so that clock runs from the
    last LIMIT tick. w1 carries the rig's markers and is swept about a minute
    after its LIMIT tick, as the record's first page was: it does not page.
    w2, the same hold 30 min on with still no tool call, does."""
    r = _rig(tmp_path)
    _show_limit(r)
    r.tick()
    assert "LIMIT — " in r.log()
    now = _stamped(r)
    _age(r.data(".last-no-turn"), now, 60)
    _show(r, "unknown-output.txt")
    r.tick()
    assert "UNKNOWN — " in r.log()
    assert not r.data(".limit").exists()
    assert r.data(".last-no-turn").stat().st_mtime == now - 60
    then, stuck = _stuck(
        tmp_path,
        scratch_plane_env,
        expect={"w2"},
        data={"w1": r.bot / "data"},
        w1={".last-tool-call": 22268},
        w2={".last-tool-call": 22268 + 30 * MIN, ".last-no-turn": 63 + 30 * MIN},
    )
    assert stuck["w2"]["last_no_turn_epoch"] == then - (63 + 30 * MIN)
    assert 63 + 30 * MIN <= stuck["w2"]["elapsed_seconds"] < 36 * MIN
