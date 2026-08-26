#!/usr/bin/env python3
"""Rewrite `@handle` out of GitHub-bound text so a bot cannot notify a stranger.

Consumed by ``lib/gh-mention-guard.sh`` (#1019). Standalone stdlib, no imports
from claudlobby — the ``dispatch-overdue.py`` precedent — because a fence-and-
inline-span parser inside a security control needs real unit tests, and `sed`
cannot carry the invariant below legibly.

WHY ANY OF THIS EXISTS
    A bot wrote `@vera` in a PR comment. GitHub resolved it to a
    real person with no connection to this project, and emailed her. She asked
    us to stop. Every one of the fleet's bot names is a real account, and the
    harm is not limited to bot names: `Botfather` is a real user, and
    it appears in our issues only because we documented Telegram's BotFather.
    The class is "any @word we write that happens to be a real handle", which
    is unbounded and grows without us doing anything.

THE RULE
    1. A composed bot name is ALWAYS rewritten. It cannot be allowlisted.
    2. Any other handle is rewritten UNLESS explicitly allowlisted.

    (1) is a deny-override and it beats (2) deliberately. Without it, someone
    eventually adds a bot's name to the allowlist meaning *our* bot, and
    silently re-arms the original bug.

────────────────────────────────────────────────────────────────────────────
THE INVARIANT — read this before editing the parser
────────────────────────────────────────────────────────────────────────────
    WHEN UNCERTAIN WHETHER TEXT IS INSIDE CODE, REWRITE IT.

Never the other way. This is the rule that decides which way to break the
parser, and it is not an implementation detail.

Why skipping code is legitimate at all: GitHub does not linkify mentions inside
fenced blocks or inline spans. That is the entire reason `` `vera` `` is a fix
rather than a cosmetic change. So a mention inside GENUINE code is already
harmless and rewriting it would only corrupt a code sample.

Why the uncertainty must resolve toward rewriting: the dangerous direction is
the parser WRONGLY believing something is code. Then a live mention passes
through untouched and emails a stranger — and "we only ever rewrite, never
block" does nothing to bound that, because the harm is the thing we failed to
do. Costs, plainly:

    wrongly rewrote real code  →  a corrupted sample in a comment.
                                  Visible, harmless, fixable by a human.
    wrongly skipped real prose →  a stranger gets an email.
                                  Not visible to us, and not undoable.

So: an unterminated fence does NOT open a code region. An unmatched backtick
does NOT open an inline span. Anything the parser cannot resolve is prose.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# GitHub's own handle grammar: 1-39 chars, alphanumeric and hyphens, no leading
# or trailing hyphen. Narrower than \w+ on purpose — `body=@-` and
# `user@example.com` must not read as mentions.
_HANDLE = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"

# MATCH UNLESS CLEARLY INSIDE A WORD — a denylist, deliberately not an allowlist.
#
# This was an ALLOWLIST (`(?<![^\s(\[{"'])`): the `@` matched only after
# whitespace, a bracket, a paren or a quote. It therefore covered the RARE
# positions and missed the UNIVERSAL ones. Measured against the shipped
# rewriter, these all escaped: `**` `*` `_` `__` `~~` `>` `-` `#` `|` `` ` ``
# `/` `:` — i.e. bold, italic, strikethrough, blockquote, bullet, heading,
# table cell. House style opens every verdict with `**`, every bullet with `-`
# and every table cell with `|`, so the guard missed nearly everything it was
# written for. It emailed a real GitHub user twice (#1019, #1329); the second
# replied on the PR to say we had the wrong person.
#
# The fix is an INVERSION, not a longer allowlist. Adding the markdown
# characters one at a time is the same defect with more entries, and it fails
# again on the next character nobody enumerated. The safe direction is to match
# unless an alphanumeric precedes, which preserves the real intent —
# `user@example.com` and `-F body=@-` stay untouched.
#
# THE DENYLIST IS ALPHANUMERIC ONLY, and `_`/`-` are excluded on evidence:
# including them re-broke `_@bot_` (markdown italic) and `-@bot`, while buying
# nothing, because `my-email@bot` and `snake_case@bot` are already blocked by
# the alphanumeric immediately before the `@`.
#
# THE TRAILING ANCHOR IS A SECOND, INDEPENDENT DEFECT. It was `\b`, which fails
# when the handle is followed by `_` — there is no word boundary between `a` and
# `_`, so `_@bot_` stayed unmatched even after the lookbehind was fixed. A
# lookahead for handle-continuation characters says "the handle ends here"
# without depending on what counts as a word character.
#
# BACKTICK IS NOT THIS PATTERN'S JOB. A `` ` `` before a mention is decided by
# the fence/span parser below, on whether the span actually CLOSES — a character
# test cannot see the closing tick. Under this pattern a lone backtick that
# never closes now correctly REWRITES, and a closed span is correctly left
# alone; that behaviour moved here from the lookbehind, where it never belonged.
_MENTION = re.compile(rf"(?<![A-Za-z0-9])@({_HANDLE})(?![A-Za-z0-9-])")

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
# A URL run — never rewrite inside one; an @ there is userinfo, not a mention.
_URL = re.compile(r"\b(?:https?://|www\.)\S+")


def _fenced_regions(text: str) -> list[tuple[int, int]]:
    """Character ranges inside CLOSED fenced code blocks.

    An unterminated fence yields NOTHING — per the invariant, text after a fence
    that never closes is treated as prose and gets rewritten. A stray triple
    backtick in a PR body would otherwise hide every mention after it.
    """
    regions: list[tuple[int, int]] = []
    pos = 0
    open_at: int | None = None
    marker = ""
    for line in text.splitlines(keepends=True):
        end = pos + len(line)
        m = _FENCE.match(line)
        if open_at is None:
            if m:
                open_at, marker = pos, m.group(1)[0] * 3
        elif m and m.group(1).startswith(marker):
            regions.append((open_at, end))
            open_at = None
        pos = end
    # open_at is not None here => unterminated fence => deliberately dropped.
    return regions


def _inline_regions(text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Character ranges inside PAIRED inline code spans, outside fenced blocks.

    An unmatched backtick opens nothing, same reasoning as above.
    """
    regions: list[tuple[int, int]] = []
    ticks = [
        m.start()
        for m in re.finditer(r"`", text)
        if not any(a <= m.start() < b for a, b in skip)
    ]
    for i in range(0, len(ticks) - 1, 2):
        regions.append((ticks[i], ticks[i + 1] + 1))
    # A trailing odd tick pairs with nothing and is deliberately ignored.
    return regions


def protected_regions(text: str) -> list[tuple[int, int]]:
    """Every range a mention inside is genuinely inert (or is a URL)."""
    fenced = _fenced_regions(text)
    return (
        fenced
        + _inline_regions(text, fenced)
        + [(m.start(), m.end()) for m in _URL.finditer(text)]
    )


def rewrite(
    text: str,
    bot_names: set[str],
    allowlist: set[str],
    *,
    style: str = "backtick",
) -> str:
    """Return *text* with notifying mentions defused.

    ``style="backtick"``  →  ``@vera`` becomes ``\\`vera\\```   (MCP / JSON fields)
    ``style="bare"``      →  ``@vera`` becomes ``vera``        (shell commands)

    The shell surface must NOT get backticks: a comment body normally sits
    inside a double-quoted shell string, where a backtick is command
    substitution — the naive rewrite would make the shell EXECUTE the handle,
    turning a notification bug into arbitrary code execution.
    """
    if not text:
        return text
    lowered_bots = {n.lower() for n in bot_names}
    lowered_alw = {n.lower() for n in allowlist}
    guarded = protected_regions(text)

    def _sub(m: re.Match) -> str:
        if any(a <= m.start() < b for a, b in guarded):
            return m.group(0)
        handle = m.group(1)
        low = handle.lower()
        # Deny-override: a bot name is rewritten even if someone allowlisted it.
        if low not in lowered_bots and low in lowered_alw:
            return m.group(0)
        return handle if style == "bare" else f"`{handle}`"

    return _MENTION.sub(_sub, text)


def report(text: str, bot_names: set[str], allowlist: set[str]) -> list[tuple[int, str]]:
    """(line_no, handle) for every mention that WOULD be rewritten.

    Used to explain a refusal. A block that says "this file has a mention" is
    barely better than a silent rewrite; one that names the line and the handle
    lets the author fix it in the same turn without re-deriving anything.

    Evaluated over the WHOLE text, never line by line. A first cut scanned each
    line independently, which loses fence context and reported a mention inside
    a closed code block — so a file would have been REFUSED for a mention that
    could never have notified. Report and rewrite must agree exactly about what
    counts, or the refusal is arguing with the fix it recommends.
    """
    lowered_bots = {n.lower() for n in bot_names}
    lowered_alw = {n.lower() for n in allowlist}
    guarded = protected_regions(text)
    starts = [m.start() for m in re.finditer(r"\n", text)]

    def line_of(pos: int) -> int:
        return sum(1 for s in starts if s < pos) + 1

    hits: list[tuple[int, str]] = []
    for m in _MENTION.finditer(text):
        if any(a <= m.start() < b for a, b in guarded):
            continue
        low = m.group(1).lower()
        if low not in lowered_bots and low in lowered_alw:
            continue
        hits.append((line_of(m.start()), m.group(1)))
    return hits


def _load(path: str | None) -> set[str]:
    if not path:
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    except OSError:
        return set()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bots", help="file of composed bot names, one per line")
    ap.add_argument("--allow", help="file of intended-mention handles, one per line")
    ap.add_argument("--style", choices=("backtick", "bare"), default="backtick")
    ap.add_argument(
        "--report",
        action="store_true",
        help="print line:handle for mentions that would be rewritten; exit 1 if any",
    )
    ap.add_argument(
        "--field",
        action="append",
        default=[],
        help="JSON mode: rewrite this key (repeatable)",
    )
    args = ap.parse_args(argv)

    bots, allow = _load(args.bots), _load(args.allow)
    raw = sys.stdin.read()

    if args.report:
        hits = report(raw, bots, allow)
        for n, h in hits:
            print(f"{n}:{h}")
        return 1 if hits else 0

    if args.report:
        hits = report(raw, bots, allow)
        for n, h in hits:
            print(f"{n}:{h}")
        return 1 if hits else 0

    if not args.field:
        sys.stdout.write(rewrite(raw, bots, allow, style=args.style))
        return 0

    obj = json.loads(raw)
    for key in args.field:
        if isinstance(obj.get(key), str):
            obj[key] = rewrite(obj[key], bots, allow, style=args.style)
    json.dump(obj, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
