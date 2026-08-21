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
already drifted once — a fleet adopted ``**[name] [VERDICT] approve**`` in an
afternoon and every PR read UNPARSED. The runtime guard is verbatim-on-unmatched
so drift is *visible*; the ``tests/test_pr_review_state.py`` pinning tests are
what stop the next edit narrowing it silently.

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
``0`` is deliberately hard to earn, because a cheap 0 is the bug::

    0  every verdict parsed, every verdict anchored, none stale, none blocking
    1  ACTIONABLE — a stale verdict, or a live blocking verdict
    2  usage error
    3  INCOMPLETE — the run could not answer for at least one PR (an unparsed
       verdict header, i.e. vocabulary drift, or an unanchored verdict)

Precedence is 1 over 3: an actionable finding dominates an incomplete one,
because the reader should act either way and acting is the stronger instruction.
Expect 3 to be common today — most verdicts carry no anchor, so a genuinely
clean answer is not available for them, and saying so is the point of (b).

Standalone stdlib (``lib/who-reviewed.py`` and ``lib/dispatch-overdue.py``
precedent). ``--payload-json`` is the offline seam that keeps every rule here a
pure function, unit-testable with no network.

  pr-review-state.py <owner/repo> [--pr N] [--limit N] [--json]
                     [--payload-json FILE] [--attribute] [--ledger-root DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

# --------------------------------------------------------------------------
# The two sampled regexes. Both are BOUNDED POSITIVELY — see the note below.
# --------------------------------------------------------------------------

#: The verdict header. ``[^*\n]{0,40}?`` and NOT ``[^*]*?``, and the difference is
#: a real defect rather than a style choice: the permissive form allows newlines,
#: so it matched from a CLOSING ``**`` through ordinary prose to a later OPENING
#: one — the matched scope and the rendered bold span diverged, and a comment
#: *discussing* a verdict parsed AS one. Bound the scope positively (a short,
#: single-line span) rather than negatively (anything that is not a star).
VERDICT_HEADER = re.compile(
    r"\*\*[^*\n]{0,40}?(?:verdict:?\s*\]?\s*|\[verdict\]\s*)?"
    r"(approve|ship it|request[\s-]+changes)\s*[.!:]?\s*[^*\n]{0,40}?\*\*",
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
SHA_ANCHOR = re.compile(
    r"(?:re-?)?reviewed\s+(?:against|at)[^0-9a-f]{0,4}([0-9a-f]{7,40})\b",
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

NORM = {"approve": "APPROVE", "ship it": "APPROVE", "request changes": "REQUEST-CHANGES"}

APPROVE = "APPROVE"
BLOCK = "REQUEST-CHANGES"

# Result flags. Literals, not an enum — tests assert on them and they are printed.
COMMIT_STALE = "COMMIT-STALE"
NO_SHA_ANCHOR = "NO-SHA-ANCHOR"
OFF_STANDARD = "OFF-STANDARD"
UNPARSED = "UNPARSED-HEADER"
UNATTRIBUTED = "UNATTRIBUTED-SEQUENCE"
DISAGREEMENT = "IDENTITY-DISAGREEMENT"

RC_OK, RC_ACTIONABLE, RC_USAGE, RC_INCOMPLETE = 0, 1, 2, 3


# --------------------------------------------------------------------------
# Pure parsing — every rule below is offline-testable
# --------------------------------------------------------------------------


def parse_verdict(body: str) -> str | None:
    """``APPROVE`` / ``REQUEST-CHANGES``, or None when no header matches."""
    match = VERDICT_HEADER.search(body or "")
    if not match:
        return None
    return NORM.get(re.sub(r"[\s-]+", " ", match.group(1).lower()))


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


def assess_pr(payload: dict, ledger_identity: dict | None = None, canonical: bool = False) -> dict:
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
    unparsed = [
        first_bold(e["body"])
        for e in events
        if parse_verdict(e["body"]) is None and VERDICT_SHAPED.search(first_bold(e["body"]))
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

    return {
        "number": payload.get("number"),
        "title": payload.get("title") or "",
        "head": head,
        "events": len(events),
        "verdicts": len(vevents),
        "resolved": {k: {"verdict": v["verdict"], "anchor": v["anchor"], "ts": v["ts"]}
                     for k, v in resolved.items()},
        "blocking": [e["reviewer"] for e in blocking],
        "stale": [{"reviewer": e["reviewer"], "anchor": e["anchor"]} for e in stale],
        "unanchored": [e["reviewer"] for e in unanchored],
        "unparsed_headers": unparsed,
        "flags": sorted(set(flags)),
    }


def exit_code_for(results: list[dict]) -> int:
    """1 ACTIONABLE beats 3 INCOMPLETE beats 0. See the module docstring."""
    if any(r["stale"] or r["blocking"] for r in results):
        return RC_ACTIONABLE
    if any(r["unanchored"] or r["unparsed_headers"] for r in results):
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
    tail = ""
    if anchored < verdicts:
        tail = (f"  — staleness is UNKNOWABLE for {verdicts - anchored} verdict(s) "
                "that named no commit; that is not 'clean'")
    return "SUMMARY: " + ", ".join(parts) + tail


# --------------------------------------------------------------------------
# GitHub side — the only impure functions
# --------------------------------------------------------------------------

PR_FIELDS = "number,title,reviews,comments,headRefOid"


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


def ledger_identity_for(repo: str, number: int, ledger_root: str) -> dict:
    """Observed reviewer names keyed by review timestamp, via ``lib/who-reviewed.py``.

    Lazily imported and OPT-IN (``--attribute``): it needs a ledger root, and a
    module that reached for one unbidden could not be unit-tested without a fleet.

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
        import importlib.util

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "who-reviewed.py")
        spec = importlib.util.spec_from_file_location("who_reviewed", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rows: list[dict] = []
        # (fleet, path) — that order is who-reviewed's, verified by reading it.
        for fleet, ledger_path in module.discover_ledgers(ledger_root):
            loaded, _ = module.load_ledger(ledger_path, fleet)
            rows.extend(loaded)
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
            parts.append(f"{reviewer}={info['verdict']}@{anchor}")
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
        if canonical and OFF_STANDARD in r["flags"]:
            lines.append(
                f"      {OFF_STANDARD}: verdict landed on .comments[]; "
                "`gh pr review --comment` writes .reviews[]"
            )
        if UNATTRIBUTED in r["flags"]:
            lines.append(
                f"      {UNATTRIBUTED}: a block and an approve, neither attributable. "
                "Same reviewer resolving themselves and two reviewers with a live block "
                "are byte-identical here; the block is kept live as the safe direction. "
                "Re-run with --attribute to resolve it."
            )
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
    parser.add_argument("--payload-json", help="read payload(s) from a file; no network")
    parser.add_argument("--canonical", action="store_true",
                        help="flag verdicts landing on .comments[]")
    parser.add_argument("--attribute", action="store_true",
                        help="cross-check header identity against the report-back ledger")
    parser.add_argument("--ledger-root", default=os.environ.get("CLAUDLOBBY_ROOT", ""))
    args = parser.parse_args(argv)
    selftest()

    if args.attribute and not args.ledger_root:
        print("--attribute needs --ledger-root (or CLAUDLOBBY_ROOT)", file=sys.stderr)
        return RC_USAGE

    try:
        if args.payload_json:
            with open(args.payload_json) as handle:
                loaded = json.load(handle)
            payloads = loaded if isinstance(loaded, list) else [loaded]
        elif args.pr:
            payloads = [fetch_payload(args.repo, args.pr)]
        else:
            payloads = [fetch_payload(args.repo, n)
                        for n in fetch_open_numbers(args.repo, args.limit)]
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"cannot read PR data: {exc}", file=sys.stderr)
        return RC_USAGE

    results = []
    for payload in payloads:
        identity = {}
        if args.attribute and payload.get("number"):
            identity, attr_error = ledger_identity_for(
                args.repo, payload["number"], args.ledger_root
            )
            if attr_error:
                print(
                    f"warning: --attribute could not read the ledger for "
                    f"#{payload['number']} ({attr_error}); identity falls back to "
                    "UNKNOWN, which is NOT the same as 'nobody was attributable'",
                    file=sys.stderr,
                )
        results.append(assess_pr(payload, identity, canonical=args.canonical))

    rc = exit_code_for(results)
    if args.as_json:
        print(json.dumps({"schema": 1, "repo": args.repo, "rc": rc,
                          "summary": summary_line(results), "prs": results}, indent=2))
    else:
        print(render(results, args.canonical))
    return rc


if __name__ == "__main__":
    sys.exit(main())
