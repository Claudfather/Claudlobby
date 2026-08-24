#!/usr/bin/env python3
"""Score a permission probe from a Claude Code SESSION TRANSCRIPT.

Why a transcript and never a pane
---------------------------------
The interactive arm of the permissions-effectiveness harness must be scored from
transcript records, not from pane text (#970 ruling). ``_BUSY_PATTERN_BASE`` --
the DECLARED SSOT for "is this pane busy" -- matches **zero** panes on this fleet
(#838, open since 2026-07-28); ``pane_is_busy`` returns idle for a session that is
mid-tool-call. A harness that scrapes panes inherits a known-dead instrument whose
failure mode is silence, which is the one failure nobody sees. ``boot-strand-sampler``
set the precedent: ground truth is a transcript record a pane cannot contradict.

The three outcomes, and the two that hide inside "not blocked"
--------------------------------------------------------------
A probe has THREE informative outcomes -- it EXECUTED, it was DENIED, or it was
NEVER ATTEMPTED -- and a two-way split hides one of them. "Not blocked" is not an
observation; it is the union of *executed* and *never tried*, which have opposite
meanings for a permission verdict.

Two further states are UNINFORMATIVE and are modelled explicitly rather than being
folded into a verdict:

``NO_RESULT``
    The tool was invoked and no paired result exists. An invocation with no result
    tells you as little as no invocation at all, so it must not score as EXECUTED.

``ERROR_OTHER``
    The result errored, but not with a permission denial -- a missing binary, a bad
    path. **This is the load-bearing fail-closed rung.** Scoring any error as DENIED
    reads a broken probe as working enforcement, in the reassuring direction. It is
    not hypothetical: this harness's first deny probe was ``factor 12``, which turned
    out to be *ungranted as well as denied*, so it came back "not executed" in all
    four arms including one with no config file at all, and discriminated nothing.

Matching is EXACT, never substring
----------------------------------
The probe is identified by an exact match on the invocation payload. "A Bash call
happened" is satisfied by a model running something adjacent to what was asked, and
a loose check turns that into evidence. Pairing is by ``tool_use_id`` and never by
document order, so an interleaved or retried call cannot be credited to the wrong
invocation.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# --- outcomes -----------------------------------------------------------------

EXECUTED = "EXECUTED"
DENIED = "DENIED"
NOT_ATTEMPTED = "NOT_ATTEMPTED"
NO_RESULT = "NO_RESULT"
ERROR_OTHER = "ERROR_OTHER"
AMBIGUOUS = "AMBIGUOUS"
UNVALIDATED_MODE = "UNVALIDATED_MODE"

#: Outcomes that carry signal about the permission system.
INFORMATIVE = frozenset({EXECUTED, DENIED, NOT_ATTEMPTED})

#: Outcomes that mean "this cell did not measure anything". A caller must REFUSE on
#: these rather than degrade to a verdict -- that degradation is the whole defect
#: this module exists to prevent.
UNINFORMATIVE = frozenset({NO_RESULT, ERROR_OTHER, AMBIGUOUS, UNVALIDATED_MODE})

#: Substrings that mark a tool_result as a PERMISSION denial rather than a failure.
#: Measured verbatim on claude 2.1.240 from a real denied Bash call:
#:     "Permission to use Bash with command factor 12 has been denied."
#: Kept as a tuple of lowercase fragments that must ALL appear, so a wording change
#: degrades to ERROR_OTHER (uninformative, refused) rather than to EXECUTED (a
#: silent false clean). Fail toward refusing, never toward reassurance.
DENIAL_SIGNATURES: tuple[tuple[str, ...], ...] = (
    ("permission to use", "has been denied"),
    ("permission denied by", "deny rule"),
    ("blocked by", "permission"),
)


#: Modes whose tool_result SHAPE has been validated against REAL captured
#: specimens -- one confirmed denial AND one confirmed success, captured in that
#: mode and diffed against each other.
#:
#: Both entries are earned, not assumed. Captured on claude 2.1.240, 2026-08-24,
#: throwaway project + disposable CLAUDE_CONFIG_DIR, deny ``Bash(factor *)`` and
#: allow ``Bash(touch:*)`` composed together so one session yields both outcomes:
#:
#:   headless     -- ``claude -p``
#:   interactive  -- real ``claude`` TUI in tmux, driven via ``pane_send_verified``
#:
#: The two modes produced BYTE-IDENTICAL record shapes: same ``is_error`` flag,
#: same denial string, same ``tool_use.id`` -> ``tool_result.tool_use_id`` pairing.
#: That identity is a MEASUREMENT, not an assumption, and it is the only reason
#: this scorer may be pointed at an interactive transcript at all.
#:
#: Adding a mode here without capturing both specimens in it would recreate the
#: exact defect this module exists to prevent, one level up: a confident verdict
#: derived from record shapes nobody has seen.
#:
#: The refusal below is deliberately a REFUSAL and not a documented caveat. A
#: caveat is recall-bound -- it competes with whatever the reader already believes,
#: and this estate has watched three managers walk straight past a written "do not
#: rely on this" line the day after it was written. A refusal is act-bound and
#: fires whether or not anyone remembers it.
SIGNATURES_VALIDATED_FOR: frozenset[str] = frozenset({"headless", "interactive"})

def verdict_identity(cause: str, mode: str, permission_mode: str) -> str:
    """Mode is PART OF THE VERDICT, never a footnote attached to one.

    There is no bare "ENFORCED" -- only "ENFORCED (interactive, auto)" and
    "ENFORCED (headless, auto)", which are different values rather than one value
    with an asterisk. A verdict that does not name the mode and the permission mode
    that produced it is not a verdict, because nothing stops a reader from
    transferring it to the mode they care about.
    """
    return f"{cause} ({mode}, permission-mode={permission_mode})"


@dataclass(frozen=True)
class ProbeResult:
    """One probe's outcome, plus the evidence that produced it."""

    outcome: str
    tool_use_id: str | None = None
    detail: str = ""
    matches: int = 0

    @property
    def informative(self) -> bool:
        return self.outcome in INFORMATIVE


@dataclass
class Cell:
    """A named arm of the matrix, carrying the facts a verdict must name."""

    name: str
    mode: str                      # "interactive" | "headless" -- NEVER defaulted
    permission_mode: str           # as read from composed bot.conf
    binary_version: str
    composed_denies: list[str] = field(default_factory=list)
    probes: dict[str, ProbeResult] = field(default_factory=dict)


# --- transcript reading -------------------------------------------------------


def load_transcript(path: str) -> list[dict[str, Any]]:
    """Read a JSONL transcript. Malformed lines are skipped, never guessed at."""
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _content_blocks(rows: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for row in rows:
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    yield block


def _result_text(block: dict[str, Any]) -> str:
    """Flatten a tool_result payload, which is a str in some records and a list of
    blocks in others. Both shapes appear in real transcripts."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return " ".join(parts)
    if content is None:
        return ""
    return str(content)


def is_denial(text: str) -> bool:
    """True only for an explicit PERMISSION denial.

    Anything else -- a missing binary, a bad path, a timeout -- is a failure, not
    enforcement, and must not be scored as DENIED.
    """
    lowered = text.lower()
    return any(all(frag in lowered for frag in sig) for sig in DENIAL_SIGNATURES)


# --- the scorer ---------------------------------------------------------------


def classify_probe(
    rows: Sequence[dict[str, Any]],
    *,
    tool_name: str,
    payload_key: str,
    payload_value: str,
    mode: str = "headless",
) -> ProbeResult:
    """Classify one probe by EXACT match on ``input[payload_key]``.

    ``payload_key``/``payload_value`` identify the invocation exactly -- e.g.
    ``("command", "touch /tmp/MARK-A")`` for Bash or ``("file_path", "/abs/x")``
    for Read. A substring or "some call to this tool" test is deliberately not
    offered: it is satisfied by an adjacent action and would manufacture evidence.
    """
    if mode not in SIGNATURES_VALIDATED_FOR:
        # Refuse BEFORE reading anything. Producing a confident outcome from record
        # shapes never seen in this mode is the false-clean this module exists to
        # prevent, one level up from the probe.
        return ProbeResult(
            UNVALIDATED_MODE,
            detail=(
                f"tool_result shape for mode={mode!r} has never been captured; "
                f"validated modes: {sorted(SIGNATURES_VALIDATED_FOR)}. "
                "Capture one real denial AND one real success in this mode, diff them, "
                "then add the mode here."
            ),
        )

    uses: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}

    for block in _content_blocks(rows):
        btype = block.get("type")
        if btype == "tool_use":
            if block.get("name") != tool_name:
                continue
            payload = block.get("input")
            if not isinstance(payload, dict):
                continue
            if payload.get(payload_key) == payload_value:   # EXACT, not `in`
                uses.append(block)
        elif btype == "tool_result":
            use_id = block.get("tool_use_id")
            if isinstance(use_id, str):
                results[use_id] = block

    if not uses:
        return ProbeResult(NOT_ATTEMPTED, detail="no tool_use matched the exact payload")

    outcomes: list[ProbeResult] = []
    for use in uses:
        use_id = use.get("id")
        result = results.get(use_id) if isinstance(use_id, str) else None
        if result is None:
            # vera's tightening: invoked-with-no-result is as uninformative as
            # never invoked. It must not fall through to EXECUTED.
            outcomes.append(ProbeResult(NO_RESULT, use_id, "tool_use has no paired tool_result"))
            continue
        text = _result_text(result)
        if result.get("is_error"):
            if is_denial(text):
                outcomes.append(ProbeResult(DENIED, use_id, text[:200]))
            else:
                outcomes.append(ProbeResult(ERROR_OTHER, use_id, text[:200]))
        else:
            outcomes.append(ProbeResult(EXECUTED, use_id, text[:200]))

    distinct = {o.outcome for o in outcomes}
    if len(distinct) == 1:
        first = outcomes[0]
        return ProbeResult(first.outcome, first.tool_use_id, first.detail, len(outcomes))
    # A retry that was denied and then allowed (or the reverse) is a real state and
    # is NOT resolved by preferring one -- a tiebreak here would be a guess wearing
    # arithmetic. Report it and let the caller refuse.
    return ProbeResult(
        AMBIGUOUS,
        None,
        "conflicting outcomes for the same probe: " + ",".join(sorted(distinct)),
        len(outcomes),
    )


def control_verdict(positive: ProbeResult, negative: ProbeResult) -> tuple[bool, str]:
    """Are the controls sound enough for this cell's measurements to mean anything?

    The positive control must have been DENIED and the negative control EXECUTED.
    Any other combination means the instrument cannot produce the outcome it exists
    to detect, and the cell must refuse rather than report its measurements.
    """
    if positive.outcome != DENIED:
        return False, f"positive control did not fire (got {positive.outcome}) -- deny is not observable in this cell"
    if negative.outcome != EXECUTED:
        return False, f"negative control did not run (got {negative.outcome}) -- a blanket failure would read as enforcement"
    return True, "controls sound"


# --- CLI ----------------------------------------------------------------------


_SELFTEST_ROWS = {
    "executed": [{"message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "touch /tmp/MARK"}}]}},
        {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": False,
             "content": "(Bash completed with no output)"}]}}],
    "denied": [{"message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "factor 12"}}]}},
        {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2", "is_error": True,
             "content": "Permission to use Bash with command factor 12 has been denied."}]}}],
    "error_other": [{"message": {"content": [
        {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "nope"}}]}},
        {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "t3", "is_error": True,
             "content": "bash: nope: command not found"}]}}],
    "no_result": [{"message": {"content": [
        {"type": "tool_use", "id": "t4", "name": "Bash", "input": {"command": "hangs"}}]}}],
    "adjacent": [{"message": {"content": [
        {"type": "tool_use", "id": "t5", "name": "Bash", "input": {"command": "touch /tmp/MARK-OTHER"}}]}},
        {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "t5", "is_error": False, "content": ""}]}}],
}


def _self_test() -> int:
    """Drive the REAL scoring path on synthetic rows at zero model cost.

    This is the ``--dry-run`` contract: it calls ``classify_probe`` itself, not a
    parallel reimplementation. A dry run that exercises a path production never
    takes certifies a dead path.
    """
    cases = [
        ("executed", "touch /tmp/MARK", EXECUTED),
        ("denied", "factor 12", DENIED),
        ("error_other", "nope", ERROR_OTHER),
        ("no_result", "hangs", NO_RESULT),
        ("adjacent", "touch /tmp/MARK", NOT_ATTEMPTED),   # exact-match guard
    ]
    failures = 0
    for key, command, expected in cases:
        got = classify_probe(
            _SELFTEST_ROWS[key], tool_name="Bash", payload_key="command", payload_value=command
        )
        ok = got.outcome == expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {key:12s} -> {got.outcome:14s} (expected {expected})")
    # The mode gate: an interactive transcript must REFUSE, not score, until a
    # real specimen of each outcome has been captured in that mode.
    unval = classify_probe(
        _SELFTEST_ROWS["denied"], tool_name="Bash", payload_key="command",
        payload_value="factor 12", mode="ssh-tty",
    )
    ok_mode = unval.outcome == UNVALIDATED_MODE and not unval.informative
    failures += 0 if ok_mode else 1
    print(f"  [{'ok' if ok_mode else 'FAIL'}] {'mode-gate':12s} -> {unval.outcome:14s} "
          f"(an unvalidated mode refuses on a record it COULD have parsed)")

    ident = verdict_identity("ENFORCED", "interactive", "auto")
    ok_ident = ident == "ENFORCED (interactive, permission-mode=auto)"
    failures += 0 if ok_ident else 1
    print(f"  [{'ok' if ok_ident else 'FAIL'}] {'identity':12s} -> {ident}")

    ok, why = control_verdict(
        classify_probe(_SELFTEST_ROWS["denied"], tool_name="Bash", payload_key="command", payload_value="factor 12"),
        classify_probe(_SELFTEST_ROWS["executed"], tool_name="Bash", payload_key="command", payload_value="touch /tmp/MARK"),
    )
    print(f"  [{'ok' if ok else 'FAIL'}] control_verdict -> {why}")
    failures += 0 if ok else 1
    print(f"\n{'PASS' if not failures else 'FAIL'}: {len(cases) + 3} checks, {failures} failed")
    return 0 if not failures else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--transcript", help="path to a session JSONL transcript")
    parser.add_argument("--tool", default="Bash", help="tool name, e.g. Bash or Read")
    parser.add_argument("--payload-key", default="command", help="input field identifying the probe")
    parser.add_argument("--payload-value", help="EXACT value that field must have")
    parser.add_argument("--dry-run", action="store_true", help="drive the real scorer on synthetic rows, zero cost")
    args = parser.parse_args(argv)

    if args.dry_run:
        return _self_test()

    if not args.transcript or args.payload_value is None:
        parser.error("--transcript and --payload-value are required unless --dry-run")

    result = classify_probe(
        load_transcript(args.transcript),
        tool_name=args.tool,
        payload_key=args.payload_key,
        payload_value=args.payload_value,
    )
    print(json.dumps({
        "outcome": result.outcome,
        "informative": result.informative,
        "tool_use_id": result.tool_use_id,
        "matches": result.matches,
        "detail": result.detail,
    }, indent=2))
    # rc 0 = scored informatively; rc 3 = the cell measured nothing.
    return 0 if result.informative else 3


if __name__ == "__main__":
    sys.exit(main())
