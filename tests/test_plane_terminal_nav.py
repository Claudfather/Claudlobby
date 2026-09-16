"""Chunk Y — terminal navigation: the focus pane reads scrollback; alive is green.

Two friction fixes found in real use:
  1. the focus overlay had nothing to scroll — the sampler only ever read the
     VISIBLE screen (`capture-pane -p -e`, no `-S`), so no history existed to
     scroll. Focus now reads deep scrollback (`-S -N`); grid thumbnails stay
     visible-only (cheap, glanceable).
  2. the grid marked an ALIVE pane with `.dot.live`, but that class was only
     defined scoped to `.actor`, so in the grid it fell through to the red base
     — an idle-but-healthy fleet read as a wall of alarm.
"""

from __future__ import annotations

import re
from pathlib import Path

from claudlobby.plane import sampler as sampler_mod
from claudlobby.plane.sampler import PaneSampler, FOCUS_LINES, THUMB_LINES

PANE = {"socket": "sock1", "bot": "ramanujan", "fleet": "f"}
CSS = Path(__file__).resolve().parents[1] / \
    "claudlobby" / "plane" / "ui" / "style.css"


def _sampler() -> PaneSampler:
    # explicit tmux so the test does not depend on tmux being installed
    return PaneSampler(root=Path("/x"), tmux="tmux")


def test_focus_capture_reads_scrollback():
    argv = _sampler()._capture_argv(PANE, scrollback=FOCUS_LINES)
    assert "-S" in argv, "focus must extend the capture into the scrollback"
    # -S must name a NEGATIVE offset (lines back into history)
    assert argv[argv.index("-S") + 1] == f"-{FOCUS_LINES}"


def test_the_focus_loop_actually_requests_scrollback():
    # the argv builder SUPPORTS scrollback, but the focus path must ASK for it —
    # pin the call site so a revert there cannot pass while _capture_argv's own
    # test still goes green (the gap the argv-only pins leave open)
    src = Path(sampler_mod.__file__).read_text()
    assert re.search(r"_capture\(pane,\s*FOCUS_LINES,\s*scrollback=", src), \
        "the focus capture must pass scrollback=FOCUS_LINES to _capture()"


def test_grid_thumbnail_reads_only_the_visible_screen():
    argv = _sampler()._capture_argv(PANE, scrollback=0)
    assert "-S" not in argv, "the grid stays a cheap visible-screen glance"


def test_focus_is_far_deeper_than_a_thumbnail():
    # the whole point: focus holds real history to scroll, the grid a glance
    assert FOCUS_LINES > THUMB_LINES * 10


def test_capture_stays_read_only():
    # -p print, -e keep colours, -t the session; capture-pane mutates nothing
    argv = _sampler()._capture_argv(PANE, scrollback=0)
    assert "capture-pane" in argv and "-p" in argv and "-e" in argv
    assert argv[argv.index("-t") + 1] == "ramanujan"


def test_an_alive_dot_is_green_not_the_red_base():
    # the grid marks up panes `.dot.live`, but that class was only defined
    # scoped to `.actor`, so a GLOBAL rule is what the grid needs. Anchor to the
    # start of a line so a pre-existing `.actor .dot.live` cannot satisfy this
    # (that ambiguity made the first version of this pin inert — it matched the
    # scoped rule and passed even with the fix reverted).
    s = CSS.read_text()
    assert re.search(r"(?m)^\.dot\.live\s*\{[^}]*background:\s*var\(--ok\)", s), \
        "a GLOBAL .dot.live (line-anchored) must be green (var(--ok))"
