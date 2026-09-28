#!/usr/bin/env python3
"""True PR review state on a single-identity fleet — is the blocking verdict still live?

WHAT THIS ANSWERS, AND WHY NOTHING ELSE CAN
-------------------------------------------
A PR sitting on Request Changes and a PR being actively revised are the **same
GitHub state**: OPEN, green CI, MERGEABLE, identical colour. Claudlobby#1160 sat
EIGHT DAYS carrying a blocking verdict because of exactly that. The gap is not
attention; it is that the discriminator is not in any field GitHub serves here.

``reviewDecision`` and ``reviewRequests`` are **DEAD FIELDS on this estate**, and
that is structural rather than a configuration mistake:

* One shared PAT means GitHub blocks ``--approve``/``--request-changes`` on
  self-authored PRs, so verdicts land as prose via the same-identity fallback.
  ``reviewDecision`` reads ``NO_REVIEW`` forever on a repo being reviewed hard.
* Reviews route by **tmux dispatch**, not by GitHub's reviewer field — and per
  Claudlobby#1062 a bot name must not be passed to a person-valued field at all.
  So ``reviewRequests`` reads 0 whether a PR has three reviewers or none.
* A same-identity verdict posted with ``gh pr comment`` is an ISSUE COMMENT and
  carries **no** ``commit_id``. ``gh api .../reviews`` returns nothing for it.

So the commit a reviewer actually looked at exists **only as prose they typed**.
This module reads that prose. That is not a design preference; it is the only
anchor that exists.

THREE BOUNDS, STATED BY THE PROTOTYPE'S AUTHOR, AND (b) DECIDES WHAT A CLEAN RUN IS WORTH
-----------------------------------------------------------------------------------------
(b) FIRST, because it is the one that gets forgotten and then cited as coverage:

**(b) Staleness is only detectable where the reviewer WROTE the SHA.** That is a
CONVENTION on one fleet as of 2026-08-21, **not an enforced property**. A verdict
with no anchor is ``NO-SHA-ANCHOR`` — *unknowable*, never *clean*. This is why
the summary always prints the anchored-vs-total denominator and why an
unanchored verdict moves the exit code off 0: a reader who greps for ``STALE``,
finds nothing, and concludes nothing is stale must be wrong only when the tool
actually checked.

**(a) The verdict regex is SAMPLED from live formats, not a spec.** It has
already drifted twice — a fleet adopted ``**[name] [VERDICT] approve**`` in an
afternoon and every PR read UNPARSED; then ``**Blocking — do not merge yet.**``
went unread because ``block`` was not a verdict token (fixed #1700).
And it never read the vocabulary the library itself teaches: two of the four
verdicts in ``library/protocols/review-flow.md`` (and
``library/expertise/code-review.md``), ``Mechanical fixes`` and
``Architectural concerns``, were not tokens until #1895. Both mean "do not
merge yet", and on a 1,506-event corpus 29 of them read as nothing, as a block
only by matching an unrelated later bold span, or once (Claudlobby#465) as
APPROVE.

The runtime guard is verbatim-on-unmatched so drift is *visible* — **but that
claim was measured FALSE in its first form and is only true now because there
are TWO channels.** The original guard keyed on the structural families the
parser already covered, so it could report drift only inside vocabulary the
parser understood: on a 44-PR corpus it fired **0 times against 3 real misses**.
A guard derived from the classifier inherits the classifier's blind spot.
``DECISION_SHAPED`` is therefore a deliberately WIDER lexicon, maintained apart
from ``NORM`` and never derived from it — see the note beside it. The
``tests/test_pr_review_state.py`` pinning tests are what stop the next edit
narrowing either channel silently.

**(c) One repo per invocation.** No cross-repo sweep.

WHY THE LAST VERDICT CHRONOLOGICALLY IS THE WRONG ANSWER
--------------------------------------------------------
The prototype took the newest verdict on the PR. On Claudlobby#1311 that is
*correct by accident*: Request Changes 03:57Z then Approve 04:39Z, **same
reviewer**, so latest-wins and per-reviewer agree. Reverse the reviewers — A
blocks, B approves later — and latest-wins reports APPROVE over an unresolved
block, which is the failure this tool exists to prevent, produced by the tool.

Passing is not handling. Resolution is therefore **per reviewer**: each
reviewer's own latest verdict stands, and a PR is blocked while *any* reviewer's
latest is REQUEST-CHANGES. ``test_reversing_the_reviewers_flips_the_answer``
pins it against the shape that the accidental pass hides.

IDENTITY HAS TWO SOURCES AND THEY ARE NOT INTERCHANGEABLE
----------------------------------------------------------
A verdict header may name its author (``**[rajan] [VERDICT] approve**``); the
report-back ledger observes who was dispatched (``lib/who-reviewed.py``). The
header is **self-reported** — a bot copying a verdict template writes whatever
the template said — while the ledger is **observed**. When both exist and
disagree the answer is ``DISAGREEMENT``, never a winner: a wrong attribution
makes a reader act, an absent one only makes them look, and the first is the
original failure this estate already had.

EXIT CODES — THE FAILURE DIRECTION IS IN THE CODE, NOT ONLY THE OUTPUT
-----------------------------------------------------------------------
``0`` is meant to be hard to earn, because a cheap 0 is the bug::

    0  every verdict parsed, every verdict anchored, none stale, none blocking —
       and no PR that HAD events yielded nothing
    1  ACTIONABLE — a stale verdict, or a live blocking verdict
    2  usage error
    3  INCOMPLETE — the run could not answer for at least one PR (an unparsed
       verdict header, i.e. vocabulary drift; an unanchored verdict; or a PR
       carrying comment/review events from which NOTHING was recognised)

**That last rung is #1700 and the sentence above it did not used to be true.**
Every other rung keys on something the parser had already recognised, so a PR
the tool could not read at all produced an empty flag list, an empty blocking
list and exit ``0`` — reported identically to a genuinely clean PR. Recognition
gated every finding, so a miss produced silence and silence scored clean, and
the worse the miss the cleaner the score. Measured on a 44-PR corpus before the
fix: 13 PRs exited 0, and **7 of those 13 carried events from which NOTHING
was recognised.** (Nine had no verdict recognised, but two of those carried no
events at all -- legitimately clean, not missed; see the discriminator below.)

The matching defect in the OUTPUT was the coverage caveat, which was gated on
``anchored < verdicts`` — at zero recognition, ``0 < 0``, false. The one
sentence written to prevent a false-clean reading was suppressed precisely in
the total-miss case; its volume tracked how well the run had already gone. It is
now inverted and fires hardest where recognition is worst (``summary_line``).

A PR with NO events is still legitimately ``0``: nothing was said, so nothing
was missed. The discriminator is events-without-recognition, not emptiness —
the same presence-not-emptiness line ``source_state`` draws.

Precedence is 1 over 3: an actionable finding dominates an incomplete one,
because the reader should act either way and acting is the stronger instruction.
Expect 3 to be common today — most verdicts carry no anchor, so a genuinely
clean answer is not available for them, and saying so is the point of (b).

Standalone stdlib (``lib/who-reviewed.py`` and ``lib/dispatch-overdue.py``
precedent). ``--payload-json`` is the offline seam that keeps every rule here a
pure function, unit-testable with no network.

  pr-review-state.py <owner/repo> [--pr N] [--limit N] [--json]
                     [--payload-json FILE] [--attribute] [--plane-root DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# The two sampled regexes. Both are BOUNDED POSITIVELY — see the note below.
# --------------------------------------------------------------------------

#: The verdict header. ``[^*\n]{0,40}?`` and NOT ``[^*]*?``, and the difference is
#: a real defect rather than a style choice: the permissive form allows newlines,
#: so it matched from a CLOSING ``**`` through ordinary prose to a later OPENING
#: one — the matched scope and the rendered bold span diverged, and a comment
#: *discussing* a verdict parsed AS one. Bound the scope positively (a short,
#: single-line span) rather than negatively (anything that is not a star).
#: ``block``/``blocking``/``blocked`` joined the token set in #1700. It is the
#: plain-English way to say the one verdict this tool exists to keep alive, and
#: every verdict miss on the 44-PR corpus that day was this family — including a
#: reviewer's own ``**Blocking — do not merge yet.**`` on a PR the tool then
#: reported as ``0 blocking``.
#:
#: ``(?!\s+on\b)`` separates the two senses of the word and was found by an
#: EXISTING test rather than reasoned out: "**Status: blocked on a policy
#: decision, not awaiting a reviewer.**" is a real Claudlobby#1160 header, and
#: "blocked ON something" is a STATE THE PR IS IN — the author is reporting what
#: they are waiting for, not rendering a verdict. "Blocking — do not merge yet."
#: is the author blocking. Without the guard the first reads as REQUEST-CHANGES,
#: which invents a reviewer objection nobody made.
#:
#: The ``non-``/``un-`` lookbehinds are load-bearing, not defensive dressing:
#: "**Non-blocking observation**" is house style for the OPPOSITE verdict, and
#: the leading ``[^*\n]{0,40}?`` is non-greedy, so without them it would skip the
#: prefix and read a non-blocking note AS a block. Pinned by
#: ``test_a_non_blocking_note_is_not_a_block``.
#:
#: The ``{0,40}`` tail is deliberately NOT widened to admit longer headers such
#: as ``**1. Blocking: the rung reported a default, not the state in force.**``
#: (52 chars of tail, still missed). Width is what bounds the cross-span match
#: the positive bounding exists to prevent — ``**a** approve more **b**`` — so
#: trading it away for coverage would re-open a false-POSITIVE hole to close a
#: false-negative one. Those headers are caught by the drift channel below
#: instead, which is the honest place: it says "this is a decision I could not
#: classify" without risking classifying it wrong.
#:
#: ``mechanical fixes`` and ``architectural concerns`` joined in #1895: the two of
#: the four verdicts ``library/protocols/review-flow.md`` step 5 teaches that the
#: set had never held. Both map to REQUEST-CHANGES, and the reviewer's own words
#: travel beside the state (``verdict_words``). Once the author pushes, an
#: anchored one reads COMMIT-STALE, the manager's cue to re-check. The block
#: itself clears only when the reviewer's later verdict supersedes it, which needs
#: attribution: measured on Claudlobby#1823, ``1 stale, 1 blocking`` without
#: ``--attribute`` and ``0 blocking`` with it (the unattributable case is #1691).
#: ``[\s-]+`` mirrors ``request[\s-]+changes``, so the bracket-tagged family's
#: ``mechanical-fixes`` reads too; no live verdict spells it that way yet.
VERDICT_HEADER = re.compile(
    r"\*\*[^*\n]{0,40}?(?:verdict:?\s*\]?\s*|\[verdict\]\s*)?"
    r"(approve|ship it|request[\s-]+changes"
    r"|mechanical[\s-]+fixes|architectural[\s-]+concerns"
    r"|(?<!non-)(?<!non )(?<!un)block(?:ing|ed)?\b(?!\s+on\b))"
    r"\s*[.!:]?\s*[^*\n]{0,40}?\*\*",
    re.I,
)

#: The SHA anchor. **Verb-anchored on purpose** — a bare-hex pattern is not a
#: near-miss, it is wrong on real input: Claudlobby#1311's approve body contains
#: both a genuine anchor (``Re-reviewed at b27ffc2``) and a decoy in prose
#: (``swapped the pre-fix (7a49f7c) doc back in``). A hex-only matcher takes
#: whichever comes first and reports a verdict as stale against a commit nobody
#: reviewed. ``test_a_decoy_hex_in_prose_is_not_an_anchor`` pins that with the
#: real body.
#:
#: Both alternatives are SAMPLED FROM LIVE VERDICTS, not invented:
#:   "reviewed against `<sha>`"   — the older phrasing
#:   "Re-reviewed at b27ffc2"     — Claudlobby#1311, which the older pattern MISSED
#: The miss under-claimed (NO-SHA-ANCHOR on a perfectly anchored verdict), which
#: is the safe direction and is exactly why it survived unnoticed for a day.
#:
#: The LEADING ``\b`` is load-bearing and its absence was an inert mutant: dropping
#: ``(?:re-?)?`` passed every test, because "reviewed at" already matches inside
#: "Re-reviewed at". But with no leading boundary the verb also fired MID-WORD —
#: measured, ``"unreviewed at 3a4f5b6"`` yielded an anchor, so a NEGATION was read
#: as an AFFIRMATION and a verdict explicitly saying it had not reviewed a commit
#: would be scored as having reviewed it. Found by clog reviewing #1322.
#:
#: ``\b`` is the fix. The ``(?:re-?)?`` prefix stays INERT on every observed
#: phrasing even now, and that is measured rather than assumed: the hyphen in
#: "Re-reviewed" is already a word boundary, so ``\breviewed`` matches inside it.
#: The prefix earns its place on exactly one shape — unhyphenated "Rereviewed" —
#: which nobody has written. Kept because it names the intent, and pinned by
#: ``test_the_re_prefix_is_inert_on_observed_phrasings`` so the next reader gets
#: that from an executable check rather than re-deriving it from a mutation run
#: that comes back green.
#:
#: STEM WIDTH IS THE DIFFERENCE BETWEEN WORKING ON A DISCIPLINED FLEET AND NOT.
#: The first stem set was ``reviewed at|against`` alone. Measured on this repo's
#: own recent corpus (10 PRs, 14 verdict-shaped comments) it found 2 (14%);
#: widened it finds 4 (29%). Both recoveries are our own house phrasing —
#: ``Verified at <sha>`` — which the narrow stem could not see. That inverts the
#: usual intuition: the better disciplined a fleet, the more consistently it
#: phrases things, so a narrow matcher misses ALL of them at once rather than a
#: scattered few. A systematic miss also hides better than a random one, because
#: the output stays plausible.
#:
#: THE TAXONOMY IS THE RULE; THE STEM LIST IS ONLY ITS CURRENT SAMPLE. Three
#: categories, and a phrasing is admitted because of which one it falls into:
#:
#:   EXAMINATION — ``reviewed at``, ``verified against``. The reviewer says what
#:     they READ. Admitted.
#:   BINDING — ``anchored to``, ``pinned to``, ``SHA-anchored to``. The reviewer
#:     says what their verdict IS BOUND TO. Admitted, and added #1700.
#:   PRODUCTION — ``Merging at``, ``Fixed at``, ``Rebased onto``. These name a
#:     commit somebody MADE, not one a reviewer assessed. REJECTED: admitting
#:     them would raise the hit rate and anchor verdicts to the wrong commit —
#:     the decoy failure with extra steps.
#:
#: BINDING was missing and its absence was not an oversight in the sample — it
#: was a hole in the RULE. The old rule sorted candidates into examination
#: (admit) and production (reject), and ``anchored to`` is neither: it is the
#: purest possible anchor, and it fell outside the dichotomy entirely. So the
#: fleet standardised on ``anchored to`` on 2026-09-21 and the matcher could not
#: grow into the convention by correctly applying its own stated principle —
#: every verdict using it read NO-SHA-ANCHOR. Measured that day: 5 of 7 real
#: anchor misses on a 44-PR corpus were binding verbs.
#:
#: That is why the categories are written down here rather than only the stems:
#: the next correct phrasing should be admitted by the PRINCIPLE, and a reader
#: adding a stem is being asked which of the three it is — a question with an
#: answer — instead of whether it "looks like" the others.
#:
#: STILL MISSED, KNOWN AND MEASURED, because neither is a taxonomy gap and
#: neither is fixed here (#1700 scope):
#:   ``Verified `<sha>` ``          — right category, no preposition; the stem
#:                                    requires one, and dropping that requirement
#:                                    widens the decoy surface.
#:   ``verified against `main @ <sha>` `` — right category AND right preposition,
#:                                    defeated by the ``[^0-9a-f]{0,6}`` gap.
#: Both are argued in #1700 rather than patched silently here.
SHA_ANCHOR = re.compile(
    r"\b(?:"
    # EXAMINATION: what the reviewer read.
    r"(?:re-?)?(?:verification\s+)?(?:review(?:ed)?|verif(?:ied|ication))\s+(?:against|at)"
    r"|"
    # BINDING: what the verdict is bound to (#1700).
    r"(?:re-?)?(?:sha[\s-]*)?(?:anchor(?:ed|ing)?|pinned)\s+(?:to|at|on)"
    r")[^0-9a-f]{0,6}([0-9a-f]{7,40})\b",
    re.I,
)

#: Self-reported author inside the header: ``**[rajan] [VERDICT] approve**``.
HEADER_IDENTITY = re.compile(r"\*\*\s*\[([a-z0-9][a-z0-9_-]{0,38})\]\s*\[", re.I)

#: A bold span that is SHAPED like a verdict header but did not map to one — the
#: drift signal. Narrow on purpose: the first version flagged ANY comment leading
#: with bold, which on a real PR (Claudlobby#1160) produced three false drift
#: reports from ordinary status comments ("**Escalating rather than ruling.**").
#: A drift signal that cries wolf trains people to ignore the one real instance it
#: exists for. Both live formats are covered — the bracket-tagged
#: ``**[name] [VERDICT] x**`` and the older ``**Verdict: x**`` — so a genuinely new
#: vocabulary in either family still surfaces.
VERDICT_SHAPED = re.compile(r"\*\*[^*\n]*?(?:\[[^\]\n]{1,20}\]\s*\[[^\]\n]{1,20}\]|verdict)",
                            re.I)

#: THE SECOND DRIFT CHANNEL, AND THE REASON THERE ARE TWO (#1700).
#:
#: ``VERDICT_SHAPED`` above keys on the two STRUCTURAL families the parser
#: already covers, so it can only ever report drift *within* vocabulary the
#: parser understands. Measured on a 44-PR corpus: it fired **0 times against 3
#: real verdict misses**. A guard that inherits the classifier's blind spot is
#: not a guard; it is a second copy of the same assumption.
#:
#: So this one is a LEXICON, maintained deliberately WIDER than ``NORM`` and
#: never derived from it. The asymmetry is the design: a DETECTOR may be loose
#: because its output is "a human should look", while a CLASSIFIER must be tight
#: because its output is a verdict. Wiring the detector to the classifier's
#: vocabulary — the obvious DRY move — is exactly what produced the 0/3.
#:
#: Deliberately EXCLUDED to keep it from crying wolf, which is the failure that
#: killed the first version of the guard above: bare ``merge`` (the selftest's
#: own ``**Merge note**`` is a non-verdict), and bare ``verdict`` (already
#: covered structurally). Every addition here costs a false positive somewhere,
#: so the corpus false-positive count is measured, not assumed — see #1700.
DECISION_SHAPED = re.compile(
    r"\b(?:(?<!non-)(?<!non )(?<!un)block(?:ing|ed)?\b(?!\s+on\b)"
    r"|hold|held|approv(?:e[sd]?|al)|lgtm|ship\s?it"
    r"|request[\s-]*changes?|reject(?:ed|ing)?|veto|do not merge|merge held"
    r"|signs?\s*off|signed\s*off|clear(?:ed|ing))\b",
    re.I,
)

NORM = {"approve": "APPROVE", "ship it": "APPROVE", "request changes": "REQUEST-CHANGES",
        "mechanical fixes": "REQUEST-CHANGES", "architectural concerns": "REQUEST-CHANGES",
        "block": "REQUEST-CHANGES", "blocking": "REQUEST-CHANGES",
        "blocked": "REQUEST-CHANGES"}

APPROVE = "APPROVE"
BLOCK = "REQUEST-CHANGES"

# Result flags. Literals, not an enum — tests assert on them and they are printed.
COMMIT_STALE = "COMMIT-STALE"
NO_SHA_ANCHOR = "NO-SHA-ANCHOR"
OFF_STANDARD = "OFF-STANDARD"
UNPARSED = "UNPARSED-HEADER"
NO_RECOGNITION = "NO-RECOGNITION"
UNATTRIBUTED = "UNATTRIBUTED-SEQUENCE"
DISAGREEMENT = "IDENTITY-DISAGREEMENT"

RC_OK, RC_ACTIONABLE, RC_USAGE, RC_INCOMPLETE = 0, 1, 2, 3

#: Attribution states (#1699). ``render()`` had NO ``--attribute`` awareness, so it
#: advised re-running with a flag the invocation had already used — and for rows the
#: plane cannot reach it named a remedy that CANNOT work, sending the reader to do
#: the thing they just did and inviting the wrong conclusion ("the flag is broken")
#: over the right one ("these rows predate the plane; attribution is unavailable
#: forever"). These four must never share an output string: an attribution never
#: attempted and one that is impossible need opposite responses from the reader.
ATTR_NOT_ATTEMPTED = "not-attempted"   # no --attribute; the advice is live and correct
ATTR_ATTEMPTED = "attempted"           # ran; whether it COULD have worked is the epoch question
ATTR_UNREACHABLE = "unreachable"       # the plane could not be read — not an empty answer


def parse_instant(value: str):
    """An ISO-8601 instant as an aware UTC ``datetime``, or ``None``.

    Parsed, never compared lexically. The plane stamps mixed forms — measured on
    this host, ``MIN(occurred_at)`` is ``2026-09-20T13:41:06-04:00`` while other
    rows carry ``+00:00`` — and GitHub hands back ``...Z``. A string compare of
    those is right only by accident of the date digits and wrong at the boundary,
    which is the one place this comparison is load-bearing. A naive stamp is read
    as UTC rather than refused: the alternative is dropping a real row, and every
    caller here already treats an unparseable instant as "cannot say".
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def attribution_state(unattributed: list[dict], attribution: dict | None) -> dict:
    """Classify an unattributed sequence against the plane's epoch. Pure.

    Counts rather than a boolean, because a PR can straddle the epoch and
    "some of these are impossible" is a different instruction from "all of them
    are". ``undated`` is carried for the same reason: a verdict whose timestamp
    will not parse has NOT been shown to predate anything, and folding it into
    either side would be a claim the data does not support.
    """
    info = dict(attribution or {})
    state = info.get("state", ATTR_NOT_ATTEMPTED)
    out = {"state": state, "error": info.get("error"), "epoch": info.get("epoch"),
           "pre_epoch": 0, "undated": 0, "total": len(unattributed)}
    epoch = parse_instant(out["epoch"] or "")
    if out["epoch"] and epoch is None:
        # #1709 review (vera). Null the FIELD, not just the local. The advice's
        # guard is `if not epoch`, which catches a falsy epoch and NOT a truthy
        # one that is not an instant — so an unparseable string sailed past it
        # into "INSIDE the plane's epoch" and printed itself verbatim as if it
        # were a boundary. That is the over-claim-from-an-unreliable-instrument
        # shape this module exists to refuse, reached through a different door.
        # The reason is recorded too: without it the surviving message says the
        # epoch "could not be read", which is false here — it was read and did
        # not parse, and those send a reader to different places.
        out["error"] = out["error"] or f"epoch is not an instant: {out['epoch']!r}"
        out["epoch"] = None
    if state != ATTR_ATTEMPTED or epoch is None:
        return out
    for event in unattributed:
        ts = parse_instant(event.get("ts") or "")
        if ts is None:
            out["undated"] += 1
        elif ts < epoch:
            out["pre_epoch"] += 1
    return out


def attribution_advice(info: dict) -> str:
    """The one line that tells the reader what to DO. Four states, four strings.

    #1699: the old text was a single unconditional "Re-run with --attribute",
    emitted even when the flag had just been used, and even for rows no flag can
    ever reach. The reader does the thing they just did, it fails again, and the
    available inference is that the flag is broken — which is wrong, and blocks
    the true one. So the impossible case says STOP and the merely-unresolved case
    says what would change it.
    """
    state, total = info["state"], info["total"]
    if state == ATTR_NOT_ATTEMPTED:
        return "Re-run with --attribute to resolve it."
    if state == ATTR_UNREACHABLE:
        return (f"--attribute ran but the plane could not be read ({info['error']}); "
                "whether these are attributable is UNKNOWN, which is not the same as "
                "'nobody was attributable'.")
    epoch, pre, undated = info["epoch"], info["pre_epoch"], info["undated"]
    if not epoch:
        return ("--attribute ran and resolved nothing here; the plane's epoch could not "
                f"be read ({info.get('error') or 'reason not reported'}), so whether these "
                "are permanently unattributable cannot be determined from this run.")
    tail = f" ({undated} verdict(s) carry no parseable timestamp and are counted in neither)" if undated else ""
    dated = total - undated
    if dated and pre == dated:
        return (f"PERMANENTLY UNATTRIBUTABLE: all {pre} dated verdict(s) here predate the "
                f"plane's earliest record ({epoch}) — the F18 clean epoch, #1444. No flag "
                f"resolves these, now or ever; stop looking.{tail}")
    if pre:
        return (f"MIXED: {pre} of {dated} dated verdict(s) predate the plane's earliest "
                f"record ({epoch}) and are permanently unattributable (#1444); the other "
                f"{dated - pre} verdict(s) are inside the epoch and simply have no citing "
                f"report.{tail}")
    return (f"--attribute ran and found no citing report for these {dated} verdict(s), which "
            f"are INSIDE the plane's epoch ({epoch}) — so this is a missing report, not an "
            f"impossible one, and a report filed later would resolve it.{tail}")


# --------------------------------------------------------------------------
# Pure parsing — every rule below is offline-testable
# --------------------------------------------------------------------------


def parse_verdict(body: str) -> str | None:
    """``APPROVE`` / ``REQUEST-CHANGES``, or None when no header matches."""
    match = VERDICT_HEADER.search(body or "")
    if not match:
        return None
    return NORM.get(re.sub(r"[\s-]+", " ", match.group(1).lower()))


def verdict_words(body: str) -> str | None:
    """The verdict as the reviewer wrote it (``Mechanical fixes``), or None.

    ``parse_verdict`` maps three of the four taught verdicts onto REQUEST-CHANGES.
    The words are what still tell a manager which one it is: a send-back
    (``Mechanical fixes``), a substantive objection (``Request changes``), or an
    escalation to the manager and a human (``Architectural concerns``, #1895).
    """
    match = VERDICT_HEADER.search(body or "")
    return match.group(1) if match else None


def parse_anchor(body: str) -> str | None:
    """The SHA the reviewer said they read, or None. Verb-anchored — see SHA_ANCHOR."""
    match = SHA_ANCHOR.search(body or "")
    return match.group(1) if match else None


def parse_header_identity(body: str) -> str | None:
    """The self-reported author inside a verdict header, or None."""
    match = HEADER_IDENTITY.search(body or "")
    return match.group(1).lower() if match else None


def first_bold(body: str) -> str:
    """The leading bold span of a body, for reporting an UNPARSED header VERBATIM.

    Printing what did not match is the whole drift guard: a vocabulary gap that
    prints nothing is indistinguishable from an estate with no verdicts.
    """
    match = re.search(r"^\s*(\*\*[^*\n]{1,80}\*\*)", body or "", re.M)
    return match.group(1) if match else "(no bold header)"


def events_from_payload(payload: dict) -> list[dict]:
    """Flatten ``gh pr view --json reviews,comments`` into time-ordered events.

    Deliberately NOT ``who-reviewed.py::events_from_payload``, which truncates
    each body to a 72-char excerpt for display. Every rule here reads the FULL
    body — the SHA anchor is usually a sentence in, so an excerpt would silently
    turn every anchored verdict into NO-SHA-ANCHOR. Same reason ``source_state``
    shares a classification and never a parse: the readers want different things
    from the same bytes.
    """
    events: list[dict] = []
    for review in payload.get("reviews") or []:
        events.append(
            {
                "surface": "reviews",
                "ts": review.get("submittedAt") or "",
                "body": review.get("body") or "",
            }
        )
    for comment in payload.get("comments") or []:
        events.append(
            {
                "surface": "comments",
                "ts": comment.get("createdAt") or "",
                "body": comment.get("body") or "",
            }
        )
    return sorted(events, key=lambda e: e["ts"])


def verdict_events(events: list[dict], ledger_identity: dict | None = None) -> list[dict]:
    """Every event carrying a parseable verdict, annotated.

    ``ledger_identity`` maps an event timestamp to an observed reviewer name (from
    ``who-reviewed.py``). Passed in rather than fetched so this stays pure.
    """
    ledger_identity = ledger_identity or {}
    out = []
    for event in events:
        verdict = parse_verdict(event["body"])
        if verdict is None:
            continue
        header_name = parse_header_identity(event["body"])
        observed = ledger_identity.get(event["ts"])
        if header_name and observed and header_name != observed:
            who, identity_flag = DISAGREEMENT, f"header={header_name} ledger={observed}"
        else:
            who, identity_flag = (header_name or observed or "UNKNOWN"), None
        out.append(
            {
                **event,
                "verdict": verdict,
                "said": verdict_words(event["body"]),
                "reviewer": who,
                "identity_note": identity_flag,
                "anchor": parse_anchor(event["body"]),
            }
        )
    return out


def resolve_per_reviewer(vevents: list[dict]) -> dict[str, dict]:
    """Each reviewer's OWN latest verdict.

    The correction to latest-wins. A PR is blocked while ANY reviewer's latest is
    REQUEST-CHANGES, regardless of who spoke most recently.

    An ``UNKNOWN`` reviewer is NOT collapsed into one bucket, because that would
    let one unattributable approve overwrite another unattributable block. Each
    unattributed verdict keys on its own timestamp, so it can only ever resolve
    itself — the conservative direction, and it keeps a block alive.
    """
    latest: dict[str, dict] = {}
    for event in sorted(vevents, key=lambda e: e["ts"]):
        key = event["reviewer"]
        if key in ("UNKNOWN", DISAGREEMENT):
            key = f"{key}@{event['ts']}"
        latest[key] = event
    return latest


def assess_pr(payload: dict, ledger_identity: dict | None = None, canonical: bool = False,
              attribution: dict | None = None) -> dict:
    """The whole verdict for one PR. Pure; ``payload`` is one ``gh pr view`` blob."""
    head = payload.get("headRefOid") or ""
    events = events_from_payload(payload)
    vevents = verdict_events(events, ledger_identity)
    resolved = resolve_per_reviewer(vevents)

    flags: list[str] = []
    blocking = [e for e in resolved.values() if e["verdict"] == BLOCK]
    stale: list[dict] = []
    unanchored: list[dict] = []

    for event in resolved.values():
        anchor = event["anchor"]
        if not anchor:
            unanchored.append(event)
        elif head and not head.startswith(anchor):
            stale.append(event)
        if canonical and event["surface"] == "comments":
            flags.append(OFF_STANDARD)
        if event["identity_note"]:
            flags.append(DISAGREEMENT)

    # An unparsed header is vocabulary drift, and it is reported VERBATIM. Only
    # events that carry no verdict AND lead with a bold span are candidates —
    # ordinary prose comments are not failed verdict parses.
    # TWO channels, unioned. VERDICT_SHAPED catches drift inside a known family;
    # DECISION_SHAPED catches a decision word in a header the parser could not
    # classify at all, which is the case the single-channel version could not see
    # by construction (#1700).
    unparsed = [
        first_bold(e["body"])
        for e in events
        if parse_verdict(e["body"]) is None
        and (VERDICT_SHAPED.search(first_bold(e["body"]))
             or DECISION_SHAPED.search(first_bold(e["body"])))
    ]

    # Two or more DISAGREEING verdicts that nobody could attribute. This is the
    # honest middle of defect 1: with identity, per-reviewer resolution answers it;
    # without, a block followed by an approve is EITHER one reviewer resolving
    # themselves (Claudlobby#1311 — answer APPROVE) or two reviewers with a block
    # still standing (answer BLOCKED), and the bytes are identical.
    #
    # The block is kept LIVE rather than resolved, because the two errors are not
    # symmetric: a false live block sends a reader to look, a false clear lets an
    # unresolved objection merge. But it is FLAGGED, so the reader is told the
    # answer is unresolvable rather than confirmed — and told the remedy, which is
    # --attribute. Reporting a block without saying it might be self-resolved is
    # how a tool built to end false confidence acquires its own.
    unattributed = [e for e in resolved.values() if e["reviewer"].startswith("UNKNOWN")]
    if len(unattributed) > 1 and len({e["verdict"] for e in unattributed}) > 1:
        flags.append(UNATTRIBUTED)

    if stale:
        flags.append(COMMIT_STALE)
    if unanchored:
        flags.append(NO_SHA_ANCHOR)
    if unparsed:
        flags.append(UNPARSED)
    # THE SILENCE FLAG (#1700). A PR that HAD events and yielded no verdict at
    # all was not assessed — and under the old contract that state produced an
    # empty flag list, an empty blocking list and exit 0, i.e. it was reported
    # exactly like a genuinely clean PR. Recognition gated every finding, so a
    # miss produced silence and silence scored clean. `events` is the
    # discriminator rather than a comment count: a PR nobody has commented on is
    # legitimately unassessed and says so with `(no parseable verdict)`; a PR
    # with six comments and no recognised verdict is an instrument failure.
    no_recognition = bool(events) and not resolved
    if no_recognition:
        flags.append(NO_RECOGNITION)

    return {
        "number": payload.get("number"),
        "title": payload.get("title") or "",
        "head": head,
        "events": len(events),
        "verdicts": len(vevents),
        "resolved": {k: {"verdict": v["verdict"], "said": v["said"], "anchor": v["anchor"],
                         "ts": v["ts"]}
                     for k, v in resolved.items()},
        "blocking": [e["reviewer"] for e in blocking],
        "stale": [{"reviewer": e["reviewer"], "anchor": e["anchor"]} for e in stale],
        "unanchored": [e["reviewer"] for e in unanchored],
        "unparsed_headers": unparsed,
        "no_recognition": no_recognition,
        "flags": sorted(set(flags)),
        # #1699. Carried even when UNATTRIBUTED is absent: --json consumers need to
        # tell "attribution ran and there was nothing to resolve" from "it never ran".
        "attribution": attribution_state(unattributed, attribution),
    }


def exit_code_for(results: list[dict]) -> int:
    """1 ACTIONABLE beats 3 INCOMPLETE beats 0. See the module docstring.

    ``no_recognition`` is the #1700 rung and it is the one that makes ``0``
    genuinely expensive. Before it, exit 0 was not "hard to earn" as the
    docstring claimed — it was the DEFAULT for a PR the tool could not read,
    because every other rung keys on something the parser had already
    recognised. A vocabulary miss therefore scored clean, and the worse the
    miss, the cleaner the score.

    Read with ``.get`` so a caller assembling a result dict by hand (the tests
    do) cannot silently lose the rung by omitting the key — the failure would be
    a green run, which is this rung's own subject.
    """
    if any(r["stale"] or r["blocking"] for r in results):
        return RC_ACTIONABLE
    if any(r["unanchored"] or r["unparsed_headers"] or r.get("no_recognition")
           for r in results):
        return RC_INCOMPLETE
    return RC_OK


def summary_line(results: list[dict]) -> str:
    """The coverage sentence. Requirement 4: an empty STALE list must not read as clean.

    States the denominator every time — how many verdicts were anchored out of how
    many found — so "no stale verdicts" can never be mistaken for "nothing is
    stale" when the truth is "staleness was unknowable for 8 of 11".
    """
    # RESOLVED verdicts, not verdict EVENTS. Per-reviewer resolution collapses a
    # reviewer's superseded verdicts, so those were never assessed for staleness —
    # counting them in the denominator claimed coverage the run did not have. On
    # Claudlobby#1311 the event count is 2 and the resolved count is 1, and the
    # first version printed "2/2 anchored" for a run that checked one verdict.
    resolved_total = sum(len(r["resolved"]) for r in results)
    anchored = resolved_total - sum(len(r["unanchored"]) for r in results)
    verdicts = resolved_total
    superseded = sum(r["verdicts"] for r in results) - resolved_total
    stale = sum(len(r["stale"]) for r in results)
    blocking = sum(len(r["blocking"]) for r in results)
    unparsed = sum(len(r["unparsed_headers"]) for r in results)
    parts = [
        f"{len(results)} PR(s)",
        f"{verdicts} live verdict(s)"
        + (f" ({superseded} superseded)" if superseded else ""),
        f"{anchored}/{verdicts} anchored",
        f"{stale} stale",
        f"{blocking} blocking",
    ]
    if unparsed:
        parts.append(f"{unparsed} UNPARSED header(s) — vocabulary drift, printed above")
    # THE DISCLOSURE IS INVERTED (#1700). It used to be gated on
    # `anchored < verdicts`, which at zero recognition is `0 < 0` — false. So the
    # one sentence written to stop a false-clean reading was SUPPRESSED exactly
    # in the total-miss case, and the caveat's volume tracked how well the run
    # had already gone. It now fires hardest where recognition is WORST.
    clauses = []
    blind = [r for r in results if r.get("no_recognition")]
    if blind:
        blind_events = sum(r["events"] for r in blind)
        clauses.append(
            f"{len(blind)} PR(s) carried {blind_events} comment/review event(s) and "
            "produced NO recognised verdict — those PRs were NOT assessed, and the "
            "counts above describe only what was RECOGNISED"
        )
    if anchored < verdicts:
        clauses.append(
            f"staleness is UNKNOWABLE for {verdicts - anchored} verdict(s) whose "
            "anchor was not recognised; that is not 'clean'"
        )
    tail = ("  — " + "; ".join(clauses)) if clauses else ""
    return "SUMMARY: " + ", ".join(parts) + tail


# --------------------------------------------------------------------------
# GitHub side — the only impure functions
# --------------------------------------------------------------------------

#: The fields ``fetch_payload`` requests, and therefore the exact shape
#: ``--payload-json`` must be handed. Split into a tuple so the validator and the
#: tests derive from ONE list rather than restating it — a second copy is how the
#: fixture drifts away from production again.
PR_FIELDS = "number,title,reviews,comments,headRefOid"
PR_FIELD_LIST = tuple(PR_FIELDS.split(","))

#: The command a user must run to produce a valid ``--payload-json`` file. Printed
#: on refusal, because naming the missing field without naming the fix sends
#: someone to guess a second time.
PAYLOAD_COMMAND = (
    "gh pr view <N> --repo <owner/repo> --json " + PR_FIELDS + " > payload.json"
)


def missing_payload_fields(payload: dict) -> list[str]:
    """Documented fields absent from a payload.

    Exists because a hand-built payload is the realistic input and it is EASY to
    build a slightly short one: ``gh pr view <N> --json reviews,comments,headRefOid``
    is the obvious command — it names every field the tool visibly reads — and it
    omits ``number``, which only the renderer touches. That produced an uncaught
    ``TypeError`` from an f-string, i.e. a traceback rather than a refusal.

    The tests could not catch it: their fixture supplied ``number`` by DEFAULT, so
    every test fed a payload strictly MORE COMPLETE than a user's. A fixture kinder
    than production hides exactly the bugs a user hits first, and a green run says
    nothing about it. ``test_fixture_is_not_kinder_than_the_documented_command``
    now pins the fixture's key set to ``PR_FIELD_LIST`` so it cannot drift kind
    again.
    """
    return [f for f in PR_FIELD_LIST if f not in payload]


def _gh(args: list[str]) -> dict | list:
    proc = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout or "[]")


def fetch_payload(repo: str, number: int) -> dict:
    return _gh(["pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS])


def fetch_open_numbers(repo: str, limit: int) -> list[int]:
    rows = _gh(["pr", "list", "--repo", repo, "--state", "open", "--json", "number",
                "--limit", str(limit)])
    return [r["number"] for r in rows]


def ledger_identity_for(repo: str, number: int, plane_root: str, *, module=None) -> dict:
    """Observed reviewer names keyed by review timestamp, via ``lib/who-reviewed.py``
    — the PLANE's report rows under *plane_root* (F18 closure R2b-1: the ledgers
    are gone, and the first plane-only who-reviewed left this caller reaching for
    its deleted loaders, so attribution failed soft on every PR — the spec lens).

    Lazily imported and OPT-IN (``--attribute``): it needs a plane root, and a
    module that reached for one unbidden could not be unit-tested without a fleet.
    *module* is the seam the test drives (a preloaded who-reviewed stand-in).

    Returns ``(mapping, error)``. It fails SOFT but never SILENT, and that shape is
    scar tissue from writing it the other way first: the original swallowed every
    exception and returned ``{}``, so a reversed tuple unpack —
    ``discover_ledgers`` yields ``(fleet, path)``, not ``(path, fleet)`` — became a
    clean-looking "no attribution available" instead of the ``IsADirectoryError``
    it actually was. Losing attribution and being unable to look for it are
    different facts with different remedies, which is ``source_state``'s rule; the
    first version of this function broke it inside the module written to enforce it.
    """
    try:
        if module is None:
            module = _load_who_reviewed()
        # (rows, why) — an unreachable plane is a reason, never an empty answer
        rows, why = module.load_plane_rows(plane_root)
        if why is not None:
            return {}, f"the plane is unreachable: {why}"
        events = module.fetch_events(repo, number)
        # "bot" is present only on the MATCH path; UNKNOWN and AMBIGUOUS omit it,
        # and neither may be turned into a name here — who-reviewed refuses a
        # nearest-wins tiebreak deliberately and this must not re-add one.
        return {
            e["ts"]: e["bot"]
            for e in module.attribute(events, rows, repo, number)
            if e.get("bot")
        }, None
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"


#: The earliest instant the plane holds ANYTHING. Not the earliest PR-CITING row,
#: which is the tempting query and the wrong one: a plane whose first citing report
#: happens to land late would report every earlier verdict as "predates the plane",
#: collapsing the two states this change exists to separate. This bound supports
#: exactly one sound claim, in one direction — before it, no report can exist, so
#: attribution is impossible forever. After it, attribution merely found nothing,
#: which is a different fact with a different remedy.
PLANE_EPOCH_SQL = "SELECT MIN(occurred_at) FROM events"


def plane_epoch(plane_root: str, *, module=None) -> tuple[str | None, str | None]:
    """``(epoch, error)`` — the F18 clean-epoch boundary (#1444), read not assumed.

    Derived from the db rather than pinned to the known 2026-09-20 cutover date,
    because a hardcoded epoch is correct on exactly one host until the day someone
    re-seeds a plane, and then it is confidently wrong with nothing to notice.

    Fails SOFT but never SILENT, ``ledger_identity_for``'s shape: an unreachable
    plane returns a reason, and the caller renders "cannot say" rather than
    "permanently unattributable". Claiming permanence from an instrument that could
    not be read is the exact over-claim #1699 is about, one level up.
    """
    try:
        if module is None:
            module = _load_who_reviewed()
        pr = module._readers()
        conn = pr.connect(plane_root)
        try:
            row = conn.execute(PLANE_EPOCH_SQL).fetchone()
        finally:
            conn.close()
        epoch = row[0] if row else None
        if not epoch:
            return None, "the plane holds no events at all"
        return epoch, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _load_who_reviewed():
    """The lazy sibling-module import, in ONE place — two callers now."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "who-reviewed.py")
    spec = importlib.util.spec_from_file_location("who_reviewed", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render(results: list[dict], canonical: bool) -> str:
    lines = [
        "reviews[] UNION comments[]; reviewDecision and reviewRequests IGNORED — "
        "both are dead fields on a single-identity fleet."
    ]
    for r in results:
        head = (r["head"] or "")[:7]
        parts = []
        for reviewer, info in sorted(r["resolved"].items()):
            anchor = info["anchor"][:7] if info["anchor"] else "no-anchor"
            parts.append(f'{reviewer}={info["verdict"]}("{info["said"]}")@{anchor}')
        lines.append(
            f"  #{r['number']:<5} head={head}  events={r['events']:<3} "
            f"{' '.join(parts) or '(no parseable verdict)'}"
        )
        for stale in r["stale"]:
            lines.append(
                f"      {COMMIT_STALE}: {stale['reviewer']} reviewed {stale['anchor'][:7]}, "
                f"head is {head} — the verdict's demand may already be met"
            )
        for reviewer in r["unanchored"]:
            lines.append(
                f"      {NO_SHA_ANCHOR}: {reviewer} named no commit — "
                "staleness is UNKNOWABLE, not clean"
            )
        for header in r["unparsed_headers"]:
            lines.append(f"      {UNPARSED} (verbatim): {header}")
        if r.get("no_recognition"):
            lines.append(
                f"      {NO_RECOGNITION}: {r['events']} event(s) on this PR, none "
                "recognised as a verdict — this PR was NOT assessed. A blocking "
                "review may be live and unread; open the comments."
            )
        if canonical and OFF_STANDARD in r["flags"]:
            lines.append(
                f"      {OFF_STANDARD}: verdict landed on .comments[]; "
                "`gh pr review --comment` writes .reviews[]"
            )
        if UNATTRIBUTED in r["flags"]:
            lines.append(
                f"      {UNATTRIBUTED}: a block and an approve, neither attributable. "
                "Same reviewer resolving themselves and two reviewers with a live block "
                "are byte-identical here; the block is kept live as the safe direction."
            )
            # The advice is its own line and its own function (#1699): four states
            # that must not share a string, and the old one was unconditional.
            lines.append(f"      -> {attribution_advice(r['attribution'])}")
        if DISAGREEMENT in r["flags"]:
            lines.append(
                f"      {DISAGREEMENT}: header and ledger name different reviewers — "
                "reported, never resolved toward either"
            )
    lines.append(summary_line(results))
    return "\n".join(lines)


#: Real verdict text from Claudlobby#1311, kept HERE and not only in the test file
#: on purpose. `tests/` runs in CI; this runs on the operator's machine at the
#: moment they are reading the output. Prototype author's rationale, kept intact:
#: validation-by-live-case cannot distinguish a clean estate from a dead detector,
#: because both print nothing. A fixture fires whether or not the estate is dirty,
#: so a live hit CONFIRMS the detector rather than being the only evidence it works.
_SELFTEST_HEAD = "b27ffc2c16e9dc3972332a550925b33f1b6143b1"
_SELFTEST_CASES = [
    ("**Request Changes**", BLOCK, None),
    ("**Approve**\n\nRe-reviewed at b27ffc2. Both changes address the round-1 "
     "blocking finding directly.\n\n- swapped the pre-fix (7a49f7c) doc back in",
     APPROVE, "b27ffc2"),
    ("**Verdict: Ship it**", APPROVE, None),
    # With the line above: the four verdicts library/protocols/review-flow.md
    # teaches, verbatim. Two of them read as nothing until #1895.
    ("**Verdict: Mechanical fixes**", BLOCK, None),
    ("**Verdict: Request changes**", BLOCK, None),
    ("**Verdict: Architectural concerns**", BLOCK, None),
    ("**[branden] [VERDICT] approve** reviewed against `ee29406`", APPROVE, "ee29406"),
    ("**Merge note**\n\nThe reviewer will approve once CI clears.\n\n**Status**",
     None, None),
]


def selftest() -> None:
    """Positive control on EVERY invocation, so tomorrow's silence is readable."""
    for body, want_verdict, want_anchor in _SELFTEST_CASES:
        got = parse_verdict(body)
        assert got == want_verdict, f"SELFTEST: verdict {body[:32]!r} -> {got!r}, want {want_verdict!r}"
        got_anchor = parse_anchor(body)
        assert got_anchor == want_anchor, (
            f"SELFTEST: anchor {body[:32]!r} -> {got_anchor!r}, want {want_anchor!r}")
    # staleness both directions, against the real head
    assert not _SELFTEST_HEAD.startswith("ee29406"), "SELFTEST: stale case is not stale"
    assert _SELFTEST_HEAD.startswith("b27ffc2"), "SELFTEST: current case reads stale"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="owner/repo")
    parser.add_argument("--pr", type=int, help="one PR; default is every open PR")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--payload-json",
        help="read payload(s) from a file instead of calling gh; produce it with: "
        + PAYLOAD_COMMAND,
    )
    parser.add_argument("--canonical", action="store_true",
                        help="flag verdicts landing on .comments[]")
    parser.add_argument("--attribute", action="store_true",
                        help="cross-check header identity against the plane's report rows")
    parser.add_argument("--plane-root", default=os.environ.get("CLAUDLOBBY_ROOT", ""),
                        help="the CLAUDLOBBY_ROOT whose state/plane/plane.db holds the reports")
    args = parser.parse_args(argv)
    selftest()

    if args.attribute and not args.plane_root:
        print("--attribute needs --plane-root (or CLAUDLOBBY_ROOT)", file=sys.stderr)
        return RC_USAGE

    try:
        if args.payload_json:
            with open(args.payload_json) as handle:
                loaded = json.load(handle)
            payloads = loaded if isinstance(loaded, list) else [loaded]
            for index, payload in enumerate(payloads):
                if not isinstance(payload, dict):
                    print(f"payload[{index}] is not an object", file=sys.stderr)
                    return RC_USAGE
                gaps = missing_payload_fields(payload)
                if gaps:
                    print(
                        f"payload[{index}] is missing required field(s): "
                        f"{', '.join(gaps)}\n  produce a valid one with:\n    "
                        f"{PAYLOAD_COMMAND}",
                        file=sys.stderr,
                    )
                    return RC_USAGE
        elif args.pr:
            payloads = [fetch_payload(args.repo, args.pr)]
        else:
            payloads = [fetch_payload(args.repo, n)
                        for n in fetch_open_numbers(args.repo, args.limit)]
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"cannot read PR data: {exc}", file=sys.stderr)
        return RC_USAGE

    # ONE epoch read per run, not per PR: it is a property of the plane, not of any
    # PR, and re-deriving it per row would multiply the open while letting two rows
    # in one run disagree about where the boundary is.
    epoch, epoch_error = (None, None)
    if args.attribute:
        epoch, epoch_error = plane_epoch(args.plane_root)
        if epoch_error:
            print(f"warning: could not read the plane's epoch ({epoch_error}); "
                  "unattributable rows cannot be reported as permanent",
                  file=sys.stderr)

    results = []
    for payload in payloads:
        identity = {}
        attribution = {"state": ATTR_NOT_ATTEMPTED}
        if args.attribute and payload.get("number"):
            identity, attr_error = ledger_identity_for(
                args.repo, payload["number"], args.plane_root
            )
            if attr_error:
                print(
                    f"warning: --attribute could not read the plane for "
                    f"#{payload['number']} ({attr_error}); identity falls back to "
                    "UNKNOWN, which is NOT the same as 'nobody was attributable'",
                    file=sys.stderr,
                )
                attribution = {"state": ATTR_UNREACHABLE, "error": attr_error}
            else:
                attribution = {"state": ATTR_ATTEMPTED, "epoch": epoch,
                               "error": epoch_error}
        results.append(assess_pr(payload, identity, canonical=args.canonical,
                                 attribution=attribution))

    rc = exit_code_for(results)
    if args.as_json:
        print(json.dumps({"schema": 1, "repo": args.repo, "rc": rc,
                          "summary": summary_line(results), "prs": results}, indent=2))
    else:
        print(render(results, args.canonical))
    return rc


if __name__ == "__main__":
    sys.exit(main())
