"""Pure PR verdict and current-head assessment rules for task reviews.

These sampled formats preserve the historical review-state source interpretation.
GitHub author is a shared account; observed bot identity is supplied per event.
"""
from __future__ import annotations

import re
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

#: THE HEADER LINE IS WHERE A VERDICT LIVES (#2029).
#:
#: ``VERDICT_HEADER`` used to be SEARCHED over a comment's whole body, so the
#: first bold span anywhere that held a verdict word was taken as the comment's
#: own verdict. A note explaining someone else's block read as a block (#1757: a
#: comment that opens with a non-verdict label and, later in its first line,
#: names "the standing **Request Changes** review above"); on #1160 an approve in
#: a table cell read as an approve, and "**the block is live**" in a status
#: comment read as a block. The mirror is the unsafe direction: a note that only
#: MENTIONS an earlier **Approve**, once attributed to its writer (a two-bracket
#: header, or a report under ``--attribute``), superseded that writer's real
#: block, and the PR read 0 blocking.
#:
#: So a verdict is read from the comment's HEADER only: its leading markdown
#: headings and its first line that is neither blank nor a heading. The verdict
#: span must OPEN one of those lines, and the verdict word must lead the span,
#: after nothing but an optional ``[name] [VERDICT]`` tag or ``Verdict:``
#: (``HEADER_LEAD``). A heading counts because real verdicts are written as one
#: (``## **Approve**``) or follow one: #1985 and #1989 each carry a live block on
#: line 3, under a ``#`` title, and a rule that read only the very first line
#: would release both. The older header families (``**Request Changes**``,
#: ``**Verdict: Ship it**``) still count in the header: #1160's live blocks are
#: written that way. The header identity is read from the same lines, so a
#: bracket-tag header quoted in prose attributes nothing.
#:
#: Verdicts written anywhere else are not read, and are not dropped: a line that
#: opens with a verdict below the header (the last line of a structured review,
#: a labelled heading such as ``## Review: **Approve**``) is reported verbatim as
#: UNPARSED-HEADER (``verdict_lines_outside_header``). An unread approve cannot
#: clear a merge, and an unread block must reach a human.
#:
#: Why #1899's measured discriminator did not catch #1757: it asks whether the
#: token LEADS its bold span, and in #1757 it does, since the span is exactly
#: ``**Request Changes**``. What made it prose was the span's place in the
#: COMMENT, which no span-level rule can see. This rule applies both: the span
#: opens the header line, and the token leads the span.
HEADER_LEAD = re.compile(
    r"\*\*\s*(?:\[[a-z0-9][a-z0-9_-]{0,38}\]\s*\[verdict\]\s*|verdict:?\s*)?", re.I
)
_MARKDOWN_HEADING = re.compile(r"#{1,6}\s")

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
           "pre_epoch": 0, "undated": 0, "total": len(unattributed),
           "ambiguous": info.get("ambiguous", 0)}
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
        return "Observed attribution was not attempted; inspect the Plane report evidence."
    if state == ATTR_UNREACHABLE:
        return (f"Plane attribution could not be read ({info['error']}); "
                "whether these are attributable is UNKNOWN, which is not the same as "
                "'nobody was attributable'.")
    if info.get("ambiguous"):
        return (f"Plane found multiple citing reviewers for {info['ambiguous']} verdict(s); "
                "identity is AMBIGUOUS. Inspect the candidates; no reviewer was selected.")
    epoch, pre, undated = info["epoch"], info["pre_epoch"], info["undated"]
    if not epoch:
        return ("Plane attribution resolved nothing here; the plane's epoch could not "
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
    return (f"Plane attribution found no citing report for these {dated} verdict(s), which "
            f"are INSIDE the plane's epoch ({epoch}) — so this is a missing report, not an "
            f"impossible one, and a report filed later that names the verdict's URL "
            f"with --artifact would resolve it.{tail}")


# --------------------------------------------------------------------------
# Pure parsing — every rule below is offline-testable
# --------------------------------------------------------------------------


def header_lines(body: str) -> list[str]:
    """The comment's header: its leading markdown headings (marker stripped)
    and its first line that is neither blank nor a heading, in order (#2029)."""
    out: list[str] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = _MARKDOWN_HEADING.match(stripped)
        if heading:
            out.append(stripped[heading.end():].strip())
            continue
        out.append(stripped)
        break
    return out


def _opens_with_verdict(line: str):
    """``VERDICT_HEADER``'s match when the verdict span OPENS ``line`` and the
    verdict word leads the span (``HEADER_LEAD``); otherwise None."""
    match = VERDICT_HEADER.match(line)
    if not match or not HEADER_LEAD.fullmatch(line[: match.start(1)]):
        return None
    return match


def _header_verdict(body: str):
    """The verdict span on the comment's header, or None (#2029).

    A verdict named anywhere else in the comment is prose, in either
    direction; ``verdict_lines_outside_header`` reports the ones shaped like
    a verdict, so a block written below the header is never silence.
    """
    for line in header_lines(body):
        match = _opens_with_verdict(line)
        if match:
            return match
    return None


#: A line outside the header whose OPENING bold span holds a verdict word, after
#: at most a heading marker or a short ``Label:`` (#2029). Never counted as the
#: comment's verdict; reported verbatim as drift, so a block written below the
#: header, or under a labelled heading, reaches a human instead of vanishing.
#: Looser than the header rule on purpose (a detector may be loose, a classifier
#: must be tight): the word need not lead the span, because real blocks put a
#: label inside it (``**Merge-gate verdict: Request Changes.**``,
#: ``**Overall verdict: Mechanical fixes.**``, measured on the corpus).
_LINE_LABEL = re.compile(r"(?:#{1,6}\s+)?(?:[^*\n:>`|]{1,32}:\s*)?")
_FENCE = re.compile(r"(`{3,}|~{3,})")


def verdict_lines_outside_header(body: str) -> list[str]:
    """Lines that open with a verdict but are not the comment's header verdict.

    Skipped: fenced code, blockquotes and table rows, which quote rather than
    render. Empty when the header carries the verdict.
    """
    if _header_verdict(body):
        return []
    found: list[str] = []
    fence = None
    for raw in (body or "").splitlines():
        stripped = raw.strip()
        marker = _FENCE.match(stripped)
        if marker:
            fence = None if fence and stripped.startswith(fence) else (fence or marker.group(1))
            continue
        if fence or stripped.startswith((">", "|")):
            continue
        label = _LINE_LABEL.match(stripped)
        if VERDICT_HEADER.match(stripped[label.end():] if label else stripped):
            found.append(stripped[:100])
    return found


def parse_verdict(body: str) -> str | None:
    """``APPROVE`` / ``REQUEST-CHANGES`` from the header line, or None (#2029)."""
    match = _header_verdict(body)
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
    match = _header_verdict(body)
    return match.group(1) if match else None


def parse_anchor(body: str) -> str | None:
    """The SHA the reviewer said they read, or None. Verb-anchored — see SHA_ANCHOR."""
    match = SHA_ANCHOR.search(body or "")
    return match.group(1) if match else None


def parse_header_identity(body: str) -> str | None:
    """The self-reported author on the header line, or None (#2029: a bracket
    tag quoted in prose names nobody)."""
    for line in header_lines(body):
        match = HEADER_IDENTITY.match(line)
        if match:
            return match.group(1).lower()
    return None


def first_bold(body: str) -> str:
    """The leading bold span of a body, for reporting an UNPARSED header VERBATIM.

    Printing what did not match is the whole drift guard: a vocabulary gap that
    prints nothing is indistinguishable from an estate with no verdicts.
    """
    match = re.search(r"^\s*(\*\*[^*\n]{1,80}\*\*)", body or "", re.M)
    return match.group(1) if match else "(no bold header)"


def events_from_payload(payload: dict) -> list[dict]:
    """Flatten ``gh pr view --json reviews,comments`` into time-ordered events.

    Every rule here reads the FULL
    body — the SHA anchor is usually a sentence in, so an excerpt would silently
    turn every anchored verdict into NO-SHA-ANCHOR. Same reason ``source_state``
    shares a classification and never a parse: the readers want different things
    from the same bytes.
    """
    events: list[dict] = []
    for index, review in enumerate(payload.get("reviews") or []):
        events.append(
            {
                "surface": "reviews", "_event_id": ("reviews", index),
                "ts": review.get("submittedAt") or "",
                "body": review.get("body") or "",
            }
        )
    for index, comment in enumerate(payload.get("comments") or []):
        events.append(
            {
                "surface": "comments", "_event_id": ("comments", index),
                "ts": comment.get("createdAt") or "",
                "body": comment.get("body") or "",
            }
        )
    return sorted(events, key=lambda e: e["ts"])


def verdict_events(events: list[dict], observed_identity: dict | None = None,
                   *, require_observed: bool = False) -> list[dict]:
    """Every event carrying a parseable verdict, annotated.

    ``observed_identity`` maps the stable event occurrence to a fleet-qualified
    Plane actor. It is supplied from the same GitHub payload, never re-fetched.
    """
    observed_identity = observed_identity or {}
    out = []
    for event in events:
        verdict = parse_verdict(event["body"])
        if verdict is None:
            continue
        header_name = parse_header_identity(event["body"])
        observed = observed_identity.get(event["_event_id"])
        leaf = observed.rsplit("/", 1)[-1] if observed else None
        if header_name and observed and header_name != leaf:
            who, identity_flag = DISAGREEMENT, f"header={header_name} observed={observed}"
        elif require_observed and not observed:
            # A header is self-reported and has no fleet axis. It cannot
            # resolve another event's block when Plane attribution is missing.
            who, identity_flag = "UNKNOWN", None
        else:
            who, identity_flag = (observed or header_name or "UNKNOWN"), None
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
    unattributed verdict keys on its own occurrence, so it can only ever resolve
    itself — the conservative direction, and it keeps a block alive.
    """
    latest: dict[str, dict] = {}
    for event in sorted(vevents, key=lambda e: e["ts"]):
        key = event["reviewer"]
        if key in ("UNKNOWN", DISAGREEMENT):
            key = f"{key}@{event['surface']}:{event['_event_id'][1]}"
        latest[key] = event
    return latest


def assess_pr(payload: dict, observed_identity: dict | None = None, canonical: bool = False,
              attribution: dict | None = None, *, require_observed: bool = False) -> dict:
    """The whole verdict for one PR. Pure; ``payload`` is one ``gh pr view`` blob."""
    head = payload.get("headRefOid") or ""
    events = events_from_payload(payload)
    vevents = verdict_events(events, observed_identity, require_observed=require_observed)
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
    # #2029: a verdict-shaped line outside a comment's header is not its verdict,
    # but it is never dropped either. A block written below the header, or under
    # a labelled heading, reaches a human as drift, never as silence.
    for e in events:
        for line in verdict_lines_outside_header(e["body"]):
            if line not in unparsed:
                unparsed.append(line)

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
    # recorded attribution. Reporting a block without saying it might be self-resolved is
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
                "a COMMENT review posted through `gh api … /pulls/N/reviews` "
                "(the same-identity-fallback protocol) writes .reviews[]"
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
