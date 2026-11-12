"""keepalive marks a pane it cannot read, and fleet-pulse pages pane_unknown (#997).

keepalive reads UNKNOWN when a pane's last lines show no running turn, no prompt, no held
box and no usage limit. That verdict raised nothing: it was recorded only as the minute's
heartbeat sample, and after an IDLE tick it leaves data/.idle in place, which keeps
activity_stuck quiet however long the pane stays that way. So an UNKNOWN tick writes
data/.unknown, the epoch of the first UNKNOWN tick of the run, re-stamped on every UNKNOWN
tick as data/.held is, and every other verdict removes it (start-bot.sh removes it when a
session starts, which harness/validate-bot-change.sh checks). fleet-pulse pages
pane_unknown once the run has lasted OBSERVABILITY_PANE_UNKNOWN_THRESHOLD (default 1800 s,
the activity_stuck default). Drives the REAL keepalive tick with recording off
(tests/test_keepalive_usage_limit.py's rig) and the REAL sweep (one stub: tg-post.sh)
against a throwaway plane.
"""

from __future__ import annotations

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

MIN = 60
HOUR = 3600


def _first(r) -> int:
    return int(r.data(".unknown").read_text())


def test_keepalive_marks_a_run_of_unknown_ticks_from_its_first(tmp_path):
    """An IDLE tick, then two UNKNOWN ticks: keepalive keeps .idle, so
    activity_stuck stays quiet, and writes .unknown with the first UNKNOWN
    tick's epoch, which the second keeps. A BUSY tick ends the run and removes
    the marker, a later UNKNOWN tick starts a new run, and an IDLE tick removes
    it again."""
    r = _rig(tmp_path)
    _show(r, "idle-prompt.txt")
    r.tick()
    assert "IDLE — " in r.log() and r.data(".idle").exists()
    _show(r, "unknown-output.txt")
    r.tick()
    assert "UNKNOWN — " in r.log()
    first = _first(r)
    assert abs(first - time.time()) < 10, first
    time.sleep(1)
    r.tick()
    assert _first(r) == first
    assert r.data(".idle").exists()
    _show(r, "busy-spinner.txt")
    r.tick()
    assert not r.data(".unknown").exists()
    _show(r, "unknown-output.txt")
    r.tick()
    assert _first(r) > first
    _show(r, "idle-prompt.txt")
    r.tick()
    assert not r.data(".unknown").exists()


@needs_tmux
def test_a_pane_unreadable_past_the_threshold_raises_one_pane_unknown(tmp_path, *, scratch_plane_env):
    """Both bots read idle after their last tool call, two hours ago, so
    activity_stuck stays quiet, and both panes are now unreadable: keepalive's
    marker, re-stamped this minute, holds each run's first epoch. w1's run
    began 31 min ago: one pane_unknown, with the run's start and length. w2's
    began a minute ago, under the threshold: nothing."""
    root, paths = _two_dead_bots(tmp_path)
    now = int(time.time())
    for bot, began in (("w1", 31 * MIN), ("w2", MIN)):
        data = paths.runtime_bots / bot / "data"
        (data / ".unknown").write_text(f"{now - began}\n")
        _age(data / ".last-tool-call", now, 2 * HOUR)
        _age(data / ".idle", now, HOUR)
    p = _pulse(root, _pulse_lib(tmp_path, tmp_path / "pages"), scratch_plane_env=scratch_plane_env)
    assert p.returncode == 0, p.stderr
    _await(root, "SELECT COUNT(*) FROM events WHERE event = 'pane_unknown'", 1)
    unknown = _events(root, "pane_unknown")
    assert [alias.rsplit("/", 1)[-1] for alias, _ in unknown] == ["w1"], unknown
    data = unknown[0][1]["data"]
    assert data["unknown_since_epoch"] == now - 31 * MIN
    assert 31 * MIN <= data["unknown_seconds"] < 36 * MIN, data
    assert _events(root, "activity_stuck") == []
