"""fleet-pulse pages a bot a usage limit still holds after its reset (#996).

keepalive's LIMIT verdict writes data/.limit (first seen, reset epoch, screen;
then the limit's name and its printed reset), re-stamped every tick. Held is
expected until the reset, so that pages nobody; still held after the reset and
keepalive's resume window, with no tool call since, is the outage #996 lost
hours to, which read as idle. It pages usage_limit_held in place of
activity_stuck. Drives the REAL sweep (one stub: tg-post.sh) against a
throwaway plane, as tests/test_fleet_pulse_events_plane.py does.
"""

from __future__ import annotations

import json
import os
import time

from tests.test_fleet_pulse_events_plane import (
    _pulse,
    _pulse_lib,
    _two_dead_bots,
    needs_tmux,
)
from tests.plane_fixtures import ro


def _held(bot_dir, *, first: int, reset: int, text: str, tool_call: int):
    (bot_dir / "data" / ".limit").write_text(
        f"{first} {reset} limit -\nsession limit\n{text}\n"
    )
    marker = bot_dir / "data" / ".last-tool-call"
    marker.touch()
    os.utime(marker, (tool_call, tool_call))


def _events(root, event):
    with ro(root) as conn:
        return [
            (alias, json.loads(detail))
            for alias, detail in conn.execute(
                "SELECT subject_alias, detail FROM events WHERE event = ? ORDER BY ingest_seq",
                (event,),
            ).fetchall()
        ]


@needs_tmux
def test_a_bot_held_past_its_reset_pages_usage_limit_held_not_activity_stuck(
    tmp_path, *, scratch_plane_env
):
    root, paths = _two_dead_bots(tmp_path)
    now = int(time.time())
    # w1: the limit reset 20 minutes ago and the bot never resumed.
    _held(
        paths.runtime_bots / "w1",
        first=now - 3600,
        reset=now - 1200,
        text="10:50pm (America/New_York)",
        tool_call=now - 7200,
    )
    # w2: held, but its reset is an hour away: expected, so nothing pages.
    _held(
        paths.runtime_bots / "w2",
        first=now - 60,
        reset=now + 3600,
        text="11:59pm (America/New_York)",
        tool_call=now - 7200,
    )
    libdir = _pulse_lib(tmp_path, tmp_path / "pages")
    r = _pulse(root, libdir, scratch_plane_env=scratch_plane_env)
    assert r.returncode == 0, r.stderr
    deadline = time.monotonic() + 30
    held = []
    while time.monotonic() < deadline and not held:
        held = _events(root, "usage_limit_held")
        time.sleep(0.25)
    assert [alias.rsplit("/", 1)[-1] for alias, _d in held] == ["w1"], held
    data = held[0][1]["data"]
    assert (
        data["reset_epoch"] == now - 1200
        and data["reset"] == "10:50pm (America/New_York)"
    )
    assert data["limit"] == "session limit" and data["resume"] == "off"
    # Both bots have gone two hours without a tool call, past the 1800 s
    # threshold: without the limit both would read activity_stuck.
    assert [a for a, _d in _events(root, "activity_stuck")] == []


@needs_tmux
def test_a_stale_limit_marker_is_not_a_limit(tmp_path, *, scratch_plane_env):
    """keepalive stopped re-stamping it (the bot left LIMIT): activity_stuck's
    rules apply again, and nothing pages usage_limit_held."""
    root, paths = _two_dead_bots(tmp_path)
    now = int(time.time())
    w1 = paths.runtime_bots / "w1"
    _held(
        w1,
        first=now - 3600,
        reset=now - 1200,
        text="10:50pm (America/New_York)",
        tool_call=now - 7200,
    )
    os.utime(w1 / "data" / ".limit", (now - 900, now - 900))
    libdir = _pulse_lib(tmp_path, tmp_path / "pages")
    r = _pulse(root, libdir, scratch_plane_env=scratch_plane_env)
    assert r.returncode == 0, r.stderr
    deadline = time.monotonic() + 30
    stuck = []
    while time.monotonic() < deadline and not stuck:
        stuck = _events(root, "activity_stuck")
        time.sleep(0.25)
    assert "w1" in [a.rsplit("/", 1)[-1] for a, _d in stuck]
    assert _events(root, "usage_limit_held") == []
