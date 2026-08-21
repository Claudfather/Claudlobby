#!/usr/bin/env python3
"""Selection record for the autonomous sprint — Phase 0 of #974.

THE PROBLEM THIS EXISTS FOR. The sprint's output is WORK SELECTION, and its
failure modes do not throw. It can run flawlessly and pick the wrong things,
pick nothing, or pick busywork that looks like progress. Every signal it emits
today -- a Telegram plan, N dispatches, PRs, green CI, merges, a tidy summary --
is emitted IDENTICALLY by a working sprint and by one picking at random from a
plausible backlog.

**A selector can only be judged against what it did NOT pick.** The losers are
the evidence. Winners alone are consistent with every possible selector,
including one that ignores the mission entirely. So this module records the
whole candidate set with the score each item got, the cut, and the provenance of
the query that produced the candidates.

WHY NOT THE EXISTING SELECTOR'S RECORD SHAPE. `lib/code-audit-sweep.sh` emits
`audit_selected` carrying the winner only (repo, staleness, audit type). That is
sufficient THERE and would be worthless HERE, for a reason worth stating before
it gets copied: the audit sweep is DETERMINISTIC -- stalest repo wins -- so its
selection is auditable by re-derivation. You can recompute the answer and check
it. An LLM scoring 357 issues against a mission doc is not re-derivable: re-run
it and you get a different answer. A winner-only record of a stochastic scorer
is an unauditable ledger that LOOKS audited.

THE NOTHING-OBSERVED BUCKET. The sprint has an explicit nothing-selected exit,
and it drains every upstream defect into one beacon: a label that matches
nothing, a repo typo, a malformed score block, a scoring threshold nobody
tuned. Measured on the live backlog: the picker label shipped in the
`autonomous-runner` example (`claudna-eligible`) matches ZERO labels in
Claudlobby, and a nonexistent label returns `[]` at rc 0, silently, from a
357-issue backlog. A first run would report "No eligible work for this cadence
tick" forever, which is exactly what a cautious first run is expected to say.

So `raw=0, unfiltered=357` is a DEFECT REPORT and `raw=357, selected=0` is a
legitimate scoring result, and one beacon for both is the bug. This module
refuses to let them collapse: an empty candidate set cannot be recorded without
an independent unfiltered count and the rc of every query that produced it.

NOT A PICKER. It records and it verifies. It never scores, never selects, never
decides. Standalone stdlib module (`dispatch-overdue.py` precedent) so the
verification rungs are unit-testable offline; the issue-state seam
(`--issue-states`) keeps the mission-staleness rung network-free.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCHEMA = 1

# Outcome vocabulary. `refused` is not a third flavour of "nothing happened" --
# it means the record itself could not be trusted, which is a different fact
# from the sprint legitimately selecting nothing.
OUTCOME_SELECTED = "selected"
OUTCOME_NO_WORK = "no-work-selected"
OUTCOME_REFUSED = "refused"
VALID_OUTCOMES = frozenset({OUTCOME_SELECTED, OUTCOME_NO_WORK, OUTCOME_REFUSED})

# Verification verdicts.
OK = "OK"
DEFECT = "DEFECT"
INVALID = "INVALID"


class RecordError(ValueError):
    """The record cannot be written as given. Never silently downgraded."""


def _q(queries: list[dict], kind: str) -> dict | None:
    for q in queries:
        if q.get("kind") == kind:
            return q
    return None


def build_record(
    *,
    ts: str,
    run_id: str,
    repo: str,
    picker_version: str,
    mission_sha: str | None,
    queries: list[dict],
    candidates: list[dict],
    selected_ids: list,
    max_issues: int,
    threshold=None,
    mission_focus_refs: list | None = None,
) -> dict:
    """Assemble a selection record, refusing the shapes that would make it a lie.

    Every refusal here is a shape that reads as a successful record while
    carrying no evidence -- which is worse than no record, because a downstream
    reader cannot tell the difference.
    """
    if not isinstance(queries, list) or not queries:
        raise RecordError(
            "no queries recorded: a candidate set with no provenance cannot be "
            "distinguished from one that was never fetched"
        )

    for i, q in enumerate(queries):
        if "rc" not in q:
            raise RecordError(
                f"query[{i}] ({q.get('kind', '?')}) has no rc. An unchecked rc is how "
                "an empty result reads as an empty backlog -- the #974 nothing-observed "
                "bucket. Record the rc even when it is 0."
            )
        if "count" not in q:
            raise RecordError(f"query[{i}] ({q.get('kind','?')}) has no count")

    filtered = _q(queries, "filtered")
    unfiltered = _q(queries, "unfiltered")
    if filtered is None:
        raise RecordError("no 'filtered' query recorded -- that is the candidate source")
    if unfiltered is None:
        raise RecordError(
            "no 'unfiltered' query recorded. Without an independent count, "
            "raw=0 (a filter matching nothing -- a DEFECT) is indistinguishable "
            "from a genuinely empty backlog. This is the whole point of the record."
        )

    # THE LOSERS ARE THE EVIDENCE. A record whose candidate list contains only
    # the winners is the code-audit-sweep shape applied to a stochastic scorer.
    if candidates and len(candidates) <= len(selected_ids) and len(selected_ids) > 0:
        if {c.get("id") for c in candidates} == set(selected_ids):
            raise RecordError(
                "candidates == selected: winners-only record. A selector can only be "
                "judged against what it did NOT pick; record every candidate considered, "
                "each with the score it got."
            )

    ids = [c.get("id") for c in candidates]
    if len(ids) != len(set(ids)):
        raise RecordError("duplicate candidate ids")

    for c in candidates:
        if "scores" not in c or not isinstance(c["scores"], dict) or not c["scores"]:
            raise RecordError(
                f"candidate {c.get('id')!r} has no scores. An unscored loser is not "
                "evidence -- it cannot show WHY it lost, so it cannot distinguish a "
                "mission-driven ranking from an arbitrary one."
            )

    missing = [i for i in selected_ids if i not in set(ids)]
    if missing:
        raise RecordError(f"selected ids not present in candidates: {missing}")

    if selected_ids:
        outcome = OUTCOME_SELECTED
    else:
        outcome = OUTCOME_NO_WORK

    return {
        "schema": SCHEMA,
        "ts": ts,
        "run_id": run_id,
        "repo": repo,
        "picker_version": picker_version,
        "mission_sha": mission_sha,
        "mission_focus_refs": list(mission_focus_refs or []),
        "queries": queries,
        "candidates": candidates,
        "cut": {
            "max_issues": max_issues,
            "threshold": threshold,
            "selected_ids": list(selected_ids),
        },
        "outcome": outcome,
    }


def verify(record: dict, issue_states: dict | None = None) -> tuple[str, list[str]]:
    """Return (verdict, findings). Verdict is OK / DEFECT / INVALID.

    DEFECT and INVALID are deliberately distinct. INVALID means the record is
    malformed and says nothing about the sprint. DEFECT means the record is
    well-formed and describes a sprint run that should not be trusted -- the
    reader must be able to tell "the instrument is broken" from "the instrument
    worked and the news is bad".
    """
    findings: list[str] = []

    if record.get("schema") != SCHEMA:
        return INVALID, [f"unknown schema {record.get('schema')!r} (expected {SCHEMA})"]
    for field in ("queries", "candidates", "cut", "outcome"):
        if field not in record:
            return INVALID, [f"missing required field {field!r}"]
    if record["outcome"] not in VALID_OUTCOMES:
        return INVALID, [f"unknown outcome {record['outcome']!r}"]

    queries = record["queries"]
    candidates = record["candidates"]
    selected = record["cut"].get("selected_ids", [])

    filtered = _q(queries, "filtered")
    unfiltered = _q(queries, "unfiltered")
    if filtered is None or unfiltered is None:
        return INVALID, ["record lacks a filtered/unfiltered query pair"]

    # G2 -- rc first. A nonzero rc means the candidate list is not evidence.
    for q in queries:
        if q.get("rc") != 0:
            findings.append(
                f"query {q.get('kind')!r} exited rc={q.get('rc')}: the candidate set is "
                "not evidence. This is an instrument failure, not an empty backlog."
            )

    # G1 -- completeness. A recorded set shorter than the query returned is
    # silent truncation, which reads as exhaustive coverage.
    if len(candidates) != filtered.get("count"):
        findings.append(
            f"recorded {len(candidates)} candidates but the filtered query returned "
            f"{filtered.get('count')}: incomplete record (truncation or pagination)."
        )

    # THE NOTHING-OBSERVED BAR, higher by construction.
    if filtered.get("count") == 0:
        if unfiltered.get("count", 0) > 0:
            findings.append(
                f"DEFECT: filter matched 0 of {unfiltered.get('count')} open items. "
                "This is a broken filter, NOT an empty backlog -- do not emit a "
                "no-eligible-work beacon for it."
            )
        else:
            findings.append(
                "filtered=0 and unfiltered=0: genuinely empty backlog, "
                "confirmed by an independent count."
            )

    # Losers must be present and scored.
    if selected and len(candidates) == len(selected):
        findings.append(
            "winners-only record: no rejected candidates retained, so the "
            "selection cannot be evaluated against anything."
        )
    unscored = [c.get("id") for c in candidates if not c.get("scores")]
    if unscored:
        findings.append(f"candidates with no scores: {unscored}")

    # The cut must respect its own bound.
    if len(selected) > record["cut"].get("max_issues", len(selected)):
        findings.append(
            f"cut selected {len(selected)} > max_issues "
            f"{record['cut'].get('max_issues')}"
        )

    # Mission staleness (#974 M7 / W6). A picker scoring alignment against
    # CLOSED work is confidently wrong, and NO selection metric can detect it --
    # the error is in the yardstick, so alignment is measured against the same
    # stale document that caused it.
    if issue_states:
        stale = [
            str(ref)
            for ref in record.get("mission_focus_refs", [])
            if str(issue_states.get(str(ref), "")).upper() == "CLOSED"
        ]
        if stale:
            findings.append(
                f"DEFECT: mission focus references CLOSED work: {stale}. The picker "
                "sourced its goal from completed items; no selection metric can see this."
            )

    if not findings:
        return OK, []
    return DEFECT, findings


def parse_focus_refs(mission_text: str) -> list[str]:
    """Extract issue refs from a mission doc's Current sprint focus section.

    Matches `#NNN` only, never a bare number -- bare digits collide with task
    ids and version numbers, the same rule `who-reviewed.py` learned the hard way.
    """
    # Two header forms, because the real doc uses the one the obvious regex
    # misses. Measured: PROJECT_MISSION.md writes `**Current sprint focus:**`
    # as BOLD TEXT, not a heading, so a heading-only pattern returned [] against
    # the live file and the staleness rung silently checked nothing. A parser
    # that finds no refs is indistinguishable from a doc with no stale refs --
    # the nothing-observed bucket, one layer down.
    m = re.search(
        r"(?:^##+[ \t]*|^\*\*)Current sprint focus:?\*{0,2}[ \t]*$"
        r"(.*?)(?=^##+[ \t]|^\*\*[A-Z]|\Z)",
        mission_text,
        re.M | re.S | re.I,
    )
    if not m:
        return []
    # A FOCUS ITEM IS A NUMBERED LIST ENTRY. Prose in this section -- a
    # freshness note, a "recently shipped" line, a caveat about which items lack
    # refs -- is guidance ABOUT the list, and scraping it is not a corner case:
    # both prose forms were written here while building this, and both produced
    # a false DEFECT by reporting deliberately-retired issues as active focus.
    # The note added to prevent staleness manufactured the staleness alarm.
    # So the contract is narrow and stated in the doc: cite the issue ON the
    # numbered line, or the item is not staleness-checkable.
    items = [
        ln for ln in m.group(1).splitlines() if re.match(r"\s*\d+\.\s", ln)
    ]
    return sorted(set(re.findall(r"#(\d{2,5})\b", "\n".join(items))))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="verify a selection record (JSON on stdin or --file)")
    v.add_argument("--file")
    v.add_argument("--issue-states", help="JSON map {issue_number: OPEN|CLOSED} (offline seam)")

    f = sub.add_parser("focus-refs", help="print issue refs in a mission doc's sprint focus")
    f.add_argument("mission")

    args = ap.parse_args(argv)

    if args.cmd == "focus-refs":
        print("\n".join(parse_focus_refs(Path(args.mission).read_text())))
        return 0

    raw = Path(args.file).read_text() if args.file else sys.stdin.read()
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"INVALID: not JSON: {e}", file=sys.stderr)
        return 2

    states = json.loads(Path(args.issue_states).read_text()) if args.issue_states else None
    verdict, findings = verify(record, states)
    print(verdict)
    for finding in findings:
        print(f"  - {finding}")
    return {OK: 0, DEFECT: 1, INVALID: 2}[verdict]


if __name__ == "__main__":
    raise SystemExit(main())
