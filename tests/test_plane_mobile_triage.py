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


def test_workspace_phone_keeps_attention_before_the_channel():
    # The workspace sheet loads last, so checking style.css alone misses a
    # later reversal of the triage order.
    workspace = CSS.with_name("workspace.css").read_text()
    mobile = re.search(r"@media\s*\(max-width:\s*650px\)\s*\{(.*?)\n\}\n\n@media",
                       workspace, re.S)
    assert mobile, "the workspace phone breakpoint is gone"
    b = mobile.group(1)
    assert _order(b, ".plane-workspace #rail-right") < \
        _order(b, ".plane-workspace #rail-channel") < \
        _order(b, ".plane-workspace #rail-fleet")


def test_workspace_desktop_resets_legacy_tablet_rail_order():
    # At 1001-1100px workspace still uses three columns, while style.css's
    # 1100px media query changes the order. Its id rules must be overridden
    # before workspace's own 1000px breakpoint or the roster moves right.
    workspace = CSS.with_name("workspace.css").read_text()
    desktop = workspace.split("@media", 1)[0]
    m = re.search(r"\.plane-workspace #rail-fleet,\s*"
                  r"\.plane-workspace #rail-channel,\s*"
                  r"\.plane-workspace #rail-right\s*\{([^}]*)\}", desktop)
    assert m and re.search(r"order:\s*0\b", m.group(1)), \
        "workspace must own all three rail orders above its tablet breakpoint"


def _workspace_media(query: str) -> str:
    workspace = CSS.with_name("workspace.css").read_text()
    blocks = re.findall(r"@media\(" + re.escape(query) + r"\) \{(.*?)\n\}", workspace, re.S)
    assert blocks, f"workspace.css has no @media({query}) block"
    return "\n".join(blocks)


def test_workspace_phone_has_one_scroller():
    # A phone scrolls the page only: no fixed-height team column, no capped teams list or composer,
    # so a swipe never moves a box inside a box.
    phone = _workspace_media("max-width:760px")
    assert "calc(100dvh" not in phone, "the phone team column must not have a fixed height"
    assert re.search(r"#rail-fleet \{[^}]*max-height:none;[^}]*overflow:visible", phone)
    assert re.search(r"#rail-channel \{[^}]*height:auto", phone)
    assert re.search(r"#trust \{[^}]*overflow:visible", phone), "the views inside the column flow in the page"
    assert re.search(r"\.plane-workspace #work-loop, \.plane-workspace \.room-bot-inspector \{[^}]*max-height:none;"
                     r"[^}]*overflow:visible", phone)
    assert not re.search(r"max-height:\s*(30|48)dvh", phone)


def test_workspace_wide_conversation_keeps_its_share():
    # Wide screens: main fills what the header leaves (no guessed header height), the conversation
    # keeps a minimum share and the composer shrinks and scrolls inside itself first.
    wide = _workspace_media("min-width:761px")
    assert "calc(100dvh - 78px)" not in wide
    assert re.search(r"main \{[^}]*flex:1 1 0;", wide)
    assert re.search(r"#channel \{[^}]*min-height:max\(140px, 45%\)", wide)
    assert re.search(r"#work-loop \{[^}]*flex:0 1 auto;[^}]*min-height:0", wide)


def test_workspace_has_no_dead_wide_side_space_rule():
    # The 5vw side space for 1450px and wider never applied: the later room-first rule set the padding.
    workspace = CSS.with_name("workspace.css").read_text()
    assert "5vw" not in workspace and "min-width:1450px" not in workspace


def _workspace_rules():
    """Every style rule in workspace.css as (media condition or None, selector, declarations, source order)."""
    css = re.sub(r"/\*.*?\*/", "", CSS.with_name("workspace.css").read_text(), flags=re.S)
    rules, order, i = [], 0, 0

    def block(text, media):
        nonlocal order
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            decls = dict((k.strip(), v.strip()) for k, v in
                         (d.split(":", 1) for d in body.split(";") if ":" in d))
            for selector in selectors.split(","):
                order += 1
                rules.append((media, " ".join(selector.split()), decls, order))

    while i < len(css):
        at = css.find("@media", i)
        if at < 0:
            block(css[i:], None)
            break
        block(css[i:at], None)
        open_brace = css.index("{", at)
        depth, j = 1, open_brace + 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        block(css[open_brace + 1:j - 1], css[at + 6:open_brace].strip())
        i = j
    return rules


def _specificity(selector):
    bare = re.sub(r"\([^()]*\)", "", selector)
    return (bare.count("#"), len(re.findall(r"\.[\w-]|\[|:(?!:)[\w-]", bare)),
            len(re.findall(r"(?:^|[\s>+~])[a-z]", bare)))


def _applies(media, width):
    if media is None:
        return True
    for kind, px in re.findall(r"(min|max)-width:\s*(\d+)px", media):
        if (kind == "min" and width < int(px)) or (kind == "max" and width > int(px)):
            return False
    return "width" in media


def _computed(prop, width, targets):
    """The cascade's winner for prop at a viewport width, among selectors that match the element."""
    found = [(_specificity(sel), order, decls[prop]) for media, sel, decls, order in _workspace_rules()
             if prop in decls and re.fullmatch(targets, sel) and _applies(media, width)]
    assert found, f"no rule sets {prop} at {width}px"
    return max(found)[2]


def test_workspace_bot_inspector_scrolls_with_the_page_on_a_phone():
    # The phone override must win the cascade over the general rule later in the file, not merely exist.
    inspector = r"(?:\.plane-workspace )?\.room-bot-inspector"
    assert _computed("max-height", 375, inspector) == "none"
    assert _computed("overflow", 375, inspector) == "visible"
    assert _computed("max-height", 1280, inspector) == "30dvh"


def test_workspace_folded_composer_row_never_shrinks_beside_a_strip():
    # Beside a parts or setup strip (data-setup-focus="shared") the folded row keeps its full height.
    wide = _workspace_media("min-width:761px")
    assert re.search(r"#work-loop:not\(:has\(> #work-composer\[open\]\)\) \{[^}]*flex-shrink:0", wide)
    folded = r"\.plane-workspace #work-loop:not\(:has\(> #work-composer\[open\]\)\)"
    assert _computed("flex-shrink", 1280, folded) == "0"
    # The composer's larger share follows its open state, never where focus is.
    assert "focus-within" not in wide
    assert re.search(r"#rail-channel:has\(> #work-loop > #work-composer\[open\]\) > #channel,\s*"
                     r"\.plane-workspace #rail-channel\[data-setup-focus=\"shared\"\] > #channel \{[^}]*"
                     r"min-height:max\(120px, 30%\)", wide)


def test_workspace_phone_row_and_targets():
    # The folded row is one 44px target, focus scrolls clear of it by its measured height, Hide is 44px tall.
    phone = _workspace_media("max-width:760px")
    assert re.search(r"#work-composer > summary \{[^}]*min-height:44px", phone)
    assert re.search(r"html:has\(#work-composer:not\(\[open\]\)\) \{[^}]*"
                     r"scroll-padding-bottom:calc\(var\(--work-loop-height", phone)
    assert re.search(r"\.fleet-hide \{[^}]*min-height:44px", phone)
    work_loop = CSS.with_name("work-loop.js").read_text()
    assert '"--work-loop-height"' in work_loop, "work-loop.js measures the row for the scroll padding"
