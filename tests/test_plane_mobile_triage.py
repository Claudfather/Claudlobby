"""Chunk X — mobile triage order.

On a narrow screen the 3-column plane stacks to one column; in DOM order the
attention rail lands below the ENTIRE channel. §16 says mobile is
awareness/triage only, so the mobile media query reorders the stack:
what-needs-me (the right rail) first, then the channel, then the roster. These
pin the RULE and its DIRECTION so a later edit cannot silently drop or invert
it; the behavioural proof is the narrow-viewport screenshot in the PR.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / \
    "claudlobby" / "plane" / "ui" / "style.css"


def _mobile_block() -> str:
    s = CSS.read_text()
    m = re.search(r"@media \(max-width: 1100px\) \{(.*?)\n\}", s, re.S)
    assert m, "the mobile media query is gone"
    return m.group(1)


def _order(block: str, sel: str) -> int:
    m = re.search(re.escape(sel) + r"\s*\{[^}]*order:\s*(\d+)", block)
    assert m, f"{sel} carries no `order` in the mobile block"
    return int(m.group(1))


def test_mobile_puts_attention_first_then_channel_then_roster():
    b = _mobile_block()
    assert _order(b, "#rail-right") < _order(b, "#rail-channel") \
        < _order(b, "#rail-fleet"), \
        "triage order: attention must stack above the channel on mobile"


def test_mobile_releases_the_attention_height_cap():
    # the desktop 40% cap is relative to a full-height column that is gone once
    # stacked; leaving it would clip the rail this reorder exists to surface
    b = _mobile_block()
    assert re.search(r"#rail-attention\s*\{[^}]*max-height:\s*none", b), \
        "stacked, #rail-attention must release its desktop max-height"


def test_the_one_breakpoint_still_collapses_to_a_single_column():
    # the reorder is meaningless if the grid has not collapsed to one column
    assert "grid-template-columns: 1fr" in _mobile_block()
