#!/usr/bin/env python3
"""Dispatch-time visibility for an undeclared supersession (#1032).

``--supersedes`` (#1027) retires a re-dispatched row so it cannot age out and
page about work that already shipped. It is opt-in, and **measured on this
estate it retired zero rows in a week because nobody passed it** — 25 of 43
mispaired rows were re-dispatch shaped, exactly its case. That is a usage gap,
and a usage gap closed by intending to remember is the prose-is-not-a-control
failure this repo keeps rediscovering. So the tool says it, at the only moment
the intent exists: the dispatch.

WHY THIS IS TWO TIERS AND NOT ONE WARNING — the whole design, and it comes from
a measurement rather than taste. Over 462 id'd dispatches on this host:

    open rows >= 1 at dispatch time ............ 234  (51%)
    open rows >= 2 ............................. 179  (39%)
    open rows >= 1 with no progress report ..... 199  (43%)
    NEW dispatch shares a ref with an open row .. 51  (11%)

A note on 51% of dispatches is not a signal; it is the exact defect #1032's own
thread names — *a signal that fires on every input carries no information about
the input* — and it would be tuned out inside a day, taking the real cases with
it. None of the obvious narrowings rescue it: 39% and 43% are no better. Only
the shared-reference test lands somewhere a human can act on, so that is what
speaks, and everything else is recorded instead of said.

Note the 51% is itself partly a SYMPTOM: rows strand, so open rows accumulate
(75 dispatches went out to a bot already holding 5+). If the stranding is fixed
the quiet tier should fall on its own, which is one way to read whether it worked.

WHAT THIS DELIBERATELY DOES NOT DO. It never decides that a dispatch supersedes
anything, never rewrites ``--supersedes``, and never blocks a send. Queueing two
tasks on one bot is legitimate and common — both are owed, and
``test_a_queued_fifo_dispatch_is_NOT_retired`` exists to keep it that way. The
tool cannot see intent (that is #1027's thesis: the ledger records what was SENT,
never what was MEANT), so it states a fact and names the choice. A tool that
silently picks a row is worse than one that says it cannot tell.

The shared-reference test is an AFFORDANCE, not a verdict. Two dispatches naming
one issue may well be parallel work, and the phrasing must never imply otherwise
— its job is to put the id a caller would need within copy-paste reach, not to
accuse them of forgetting.

Openness is decided by ``dispatch-overdue.py::open_dispatches`` — the shipped
door (#904) — never re-derived here. This module reads the dispatch log directly
only to recover the TASK TEXT of ids that door already returned, for reference
comparison and display.

Standalone stdlib (``dispatch-overdue.py`` / ``who-reviewed.py`` precedent).
Wrapped by ``tests/test_dispatch_supersede_hint.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent


def _load_overdue():
    """Import the hyphenated sibling so openness comes from the shipped door."""
    path = LIB_DIR / "dispatch-overdue.py"
    spec = importlib.util.spec_from_file_location("_dispatch_overdue", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_dispatch_overdue"] = mod
    spec.loader.exec_module(mod)
    return mod


#: A GitHub-ish reference in a STORED dispatch payload. `#NNN` only, and that is
#: a measurement rather than a simplification: the ledger's `task` field holds the
#: raw payload, while the envelope's `ref:<url>` is assembled separately for the
#: pane and never stored. A `ref:https?://…/(\d+)` pattern matched **0 rows across
#: the entire 463-row log** — it was dead code in the first cut of this file, and
#: only running it against real data showed that. The surviving `#NNN` form
#: carries the whole measured 11% signal on its own (verified: hash-only reproduces
#: 51/463 exactly).
#:
#: Bounded at 3-5 digits: a bare `#1` collides with ordinary prose ("#1 priority")
#: far more often than it names an issue, and this feeds a note a human reads — a
#: false match costs a glance, a miss costs nothing that was not already missing.
#: The lookbehind excludes `/` so a URL path segment is not read as a reference,
#: and the trailing guard keeps `#1032` from matching inside a longer run.
_REF_HASH = re.compile(r"(?<![\w/])#(\d{3,5})(?!\d)")

#: The trailing number of an envelope `--ref` URL. Applied ONLY to the incoming
#: dispatch, never to stored rows (which have none). This is what lets a new
#: `ref:…/issues/1032` match an open row whose prose says `#1032` — information
#: dispatch-task.sh already holds, so it costs nothing to use.
_REF_TAIL = re.compile(r"/(\d{1,7})/?$")


def refs(text: str) -> set[str]:
    """Reference tokens in a stored dispatch payload."""
    if not text:
        return set()
    return set(_REF_HASH.findall(text))


def ref_from_url(ref: str) -> set[str]:
    """The issue/PR number at the tail of an envelope `--ref` URL, if any."""
    if not ref:
        return set()
    m = _REF_TAIL.search(ref.strip())
    return {m.group(1)} if m else set()


def plane_task_texts(mod, bot: str) -> dict[str, str]:
    """``{task_id: task text}`` from the PLANE — the only source (F18 R2a):
    the open reader's session, the work items' titles. Empty on any plane
    failure — the hint must never be why a dispatch fails (then the loud
    tier simply cannot fire, and the quiet tier still counts)."""
    try:
        p = mod.open_plane()
    except Exception:
        return {}
    try:
        return p.pr.task_texts(p.conn, p.fleet, bot)
    except Exception:
        return {}
    finally:
        try:
            p.close()
        except Exception:
            pass


def hint(
    bot: str,
    new_task: str,
    new_ref: str = "",
    overdue_mod=None,
) -> tuple[int, list[str], str]:
    """``(open_count, matching_ids, note)`` for a dispatch about to be sent.

    ``open_count`` is the quiet tier — recorded, never spoken. ``matching_ids``
    is the loud tier: open rows sharing a reference with *new_task*. ``note`` is
    empty unless there is something worth saying.
    """
    mod = overdue_mod or _load_overdue()
    try:
        rows = mod.open_dispatches(bot)          # the plane, the only source (F18 R2a)
    except Exception:
        return 0, [], ""  # fail open: never break a dispatch over a hint
    open_ids = [tid for _da, _eb, tid in rows]
    if not open_ids:
        return 0, [], ""

    mine = refs(new_task) | ref_from_url(new_ref)
    texts = plane_task_texts(mod, bot)
    matching = [tid for tid in open_ids if mine and (mine & refs(texts.get(tid, "")))]
    if not matching:
        return len(open_ids), [], ""

    # The overflow count is derived from what is ACTUALLY shown, never from a
    # second copy of the cap. Deriving it from `len(matching) - 3` while `shown`
    # is sliced separately means changing the cap in one place leaves the note
    # claiming "+2 more" beside five listed ids — a disclosure that lies about
    # its own truncation, which is worse than not disclosing. Caught by mutation:
    # widening the slice left the message unchanged, so nothing was watching it.
    shown = matching[:3]
    more = (
        "" if len(shown) == len(matching) else f" (+{len(matching) - len(shown)} more)"
    )
    plural = "" if len(shown) == 1 else "s"
    note = (
        f"dispatch-task: {bot} already has an open dispatch{plural} referencing "
        f"the same thing: {' '.join(shown)}{more}\n"
        f"                if this REPLACES it, re-send with --supersedes {shown[0]}; "
        f"if it is additional work, nothing to do."
    )
    return len(open_ids), matching, note


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bot", required=True)
    ap.add_argument("--task", default="", help="the payload about to be sent")
    ap.add_argument("--ref", default="", help="the envelope --ref URL, if any")
    ap.add_argument(
        "--count-only",
        action="store_true",
        help="print only the open-row count (the quiet tier)",
    )
    args = ap.parse_args(argv)

    count, _matching, note = hint(args.bot, args.task, args.ref)
    if args.count_only:
        print(count)
        return 0
    if note:
        print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
