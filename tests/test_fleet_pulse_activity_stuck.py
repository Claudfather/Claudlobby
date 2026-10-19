"""fleet-pulse times activity_stuck from the later of a bot's last tool call
and its last prompt (#2358).

bot-vitals.sh touches data/.last-tool-call on every tool call and, as the
UserPromptSubmit hook, data/.last-prompt on every prompt. A bot that sat idle
past the threshold and was then woken carries an old tool-call marker; once
keepalive's BUSY tick removes data/.idle, timing the stall from that marker
alone paged a bot that had begun its turn seconds earlier. A turn that has
gone past the threshold with no tool call still pages. Drives the REAL sweep
(one stub: tg-post.sh) against a throwaway plane, as
tests/test_fleet_pulse_usage_limit.py does.
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

MIN = 60
HOUR = 3600


def _markers(bot_dir, now, *, tool_call, prompt=None, idle=None):
    """Age a bot's markers to the given seconds before *now*; None leaves one absent."""
    for name, age in (
        (".last-tool-call", tool_call),
        (".last-prompt", prompt),
        (".idle", idle),
    ):
        if age is not None:
            marker = bot_dir / "data" / name
            marker.touch()
            os.utime(marker, (now - age, now - age))


def _stuck(tmp_path, scratch_plane_env, *, expect, **ages):
    """Age each bot's markers, run one sweep, and check that exactly the bots
    in *expect* paged. Returns *now* and each page's data."""
    root, paths = _two_dead_bots(tmp_path)
    now = int(time.time())
    for bot, bot_ages in ages.items():
        _markers(paths.runtime_bots / bot, now, **bot_ages)
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
def test_a_prompt_40_s_old_holds_the_page_however_old_the_last_tool_call(
    tmp_path, *, scratch_plane_env
):
    """#2358's measured case: idle two hours, then a prompt. keepalive's BUSY
    tick has removed .idle and the bot is 40 s into the turn with no tool call
    yet, which is not a hang. w2 has the same tool-call marker and no prompt
    marker, as a bot does before its settings carry the hook: it still pages
    on the tool-call clock, which also shows the sweep reached Check 5."""
    _, stuck = _stuck(
        tmp_path,
        scratch_plane_env,
        expect={"w2"},
        w1=dict(tool_call=2 * HOUR, prompt=40),
        w2=dict(tool_call=2 * HOUR),
    )
    assert stuck["w2"]["last_prompt_epoch"] is None


@needs_tmux
def test_a_prompt_31_min_old_pages_timed_from_the_later_marker(
    tmp_path, *, scratch_plane_env
):
    """A hang still pages: 31 min into a turn with no tool call, past the
    1800 s threshold. The stall runs from the later of the two markers: w1's
    prompt, and w2's last tool call, which came after its prompt."""
    now, stuck = _stuck(
        tmp_path,
        scratch_plane_env,
        expect={"w1", "w2"},
        w1=dict(tool_call=2 * HOUR, prompt=31 * MIN),
        w2=dict(tool_call=31 * MIN, prompt=2 * HOUR),
    )
    w1, w2 = stuck["w1"], stuck["w2"]
    assert (w1["last_tool_call_epoch"], w1["last_prompt_epoch"]) == (
        now - 2 * HOUR,
        now - 31 * MIN,
    )
    assert (w2["last_tool_call_epoch"], w2["last_prompt_epoch"]) == (
        now - 31 * MIN,
        now - 2 * HOUR,
    )
    # Timed from 31 min ago, not two hours; the margin covers the sweep's own run.
    for data in (w1, w2):
        assert 31 * MIN <= data["elapsed_seconds"] < 36 * MIN, data


@needs_tmux
def test_1591_a_fresh_idle_marker_still_silences_a_long_tool_call(
    tmp_path, *, scratch_plane_env
):
    """#1591, still open, is the same clock failing the other way, kept here
    as #2358 asks. A tool call 31 min long touched .last-tool-call only when
    it began; once that marker leaves keepalive's 180 s window, keepalive
    reads the bot IDLE, and the .idle it touches skips Check 5 however long
    the call runs. The prompt came before the call, so its marker changes
    nothing. w2, the same bot without .idle, pages. A change to how Check 5
    reads .idle shows here."""
    _stuck(
        tmp_path,
        scratch_plane_env,
        expect={"w2"},
        w1=dict(tool_call=31 * MIN, prompt=40 * MIN, idle=30),
        w2=dict(tool_call=31 * MIN, prompt=40 * MIN),
    )
