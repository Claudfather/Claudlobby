#!/usr/bin/env python3
"""A2 recoverability scorer for the #881 comms-topology eval.

A2 is the anti-lossy guard on the routing experiment: A1 (operator cost) is
trivially gamed by saying less, so A2 measures whether the detail that was
compressed out is still reachable. The ratified definition is BEHAVIOURAL, not
structural — after a compressed message, a follow-up prompt must retrieve the
full detail:

    Per compressed message, issue a standard follow-up ("why?", "show me the
    detail", "what was the evidence?") and score the response for whether the
    detail that was compressed out comes back IN FULL and UNRE-SUMMARISED.

"Unre-summarised" is load-bearing. A follow-up that answers with a shorter
summary of the summary is a FAIL, because the rule A2 gates is
`library/protocols/token-efficiency.md`: "When someone asks for detail, give it
in full — an explicit request is never re-summarized." A2 is the empirical gate
on that sentence, which until now was unenforced prose.

Standalone lib/ Python (stdlib-only, shell-invokable) following the
lib/ab-comms-verdict.py and lib/dispatch-overdue.py precedent — unit-testable
without the harness (tests/test_ab_recoverability_scorer.py), so the follow-up
that lands the live judge lands it in a tested place.

Two tiers, deliberately separated so the cheap half runs without spend
--------------------------------------------------------------------
STRUCTURAL (this module; zero model calls, no tunables):
  - `expanded`      — did the follow-up actually return more than the compressed
                      message? This is a SOUND ONE-WAY REFUTATION: a follow-up
                      no longer than the message it expands cannot have returned
                      the withheld detail in full, so it scores not-recovered
                      with no judge call. It can only refute, never confirm.
  - `address_present` — is a resolvable address carried by the compressed
                      message? RECORDED, NOT GATING. This was the *proposed*
                      structural A2, which the ratifier replaced with the
                      behavioural test; a message can carry a perfectly valid
                      link and still fail to answer on follow-up. Kept because
                      it is free and it is the diagnostic for *why* a pair
                      failed, not part of the pass rule.
  - raw length ratios — reported, never thresholded here.

SEMANTIC (a judge model; NOT performed by this module):
  - `in_full`         — did the withheld detail come back complete?
  - `unre_summarised` — did it come back as the detail, rather than as a shorter
                        summary of the summary?

This module NEVER calls a model. Judge verdicts are consumed from a file
(--judgements), so the spend seam sits in the harness behind the harness's
existing two keys rather than being re-invented here. That is why there is no
second key on this module: `--dry-run` is the CI-safe path, and there is no
real-mode key because there is nothing here that can spend. Wiring a live judge
is follow-up work and is deliberately not done here.

Unresolved is not failure
-------------------------
With no judgement for a pair, `recovered` is None (UNSCORED) — never False.
Silently scoring an unjudged pair as a failure would let a missing judge
manufacture an A2 regression, the mirror of the INCONCLUSIVE-never-PASS rule
ab-comms-verdict.py holds. Unscored pairs are excluded from the score and
disclosed in the output.

Score, not verdict
------------------
A1's bar is ratified (25% CI-low). **A2 has no numeric bar** — its direction is
"must not degrade versus control". So this module emits a COMPARABLE SCORE per
(task, variant) plus the control-vs-treatment delta, and deliberately emits NO
PASS/FAIL. Inventing a bar here would pre-empt a ratification that has not
happened.

Usage:
  ab-recoverability-scorer.py PAIRS.jsonl [--judgements J.jsonl]
      [--structural-only] [--out scores.json] [--emit-rows RESULTS.jsonl]
  ab-recoverability-scorer.py --dry-run [--out ...] [--emit-rows ...]

Input PAIRS.jsonl — one row per message pair:
  {"task":"T2","variant":"with","rep":1,
   "compressed":"<the outbound message>",
   "withheld":"<the detail compressed out of it>",
   "followup_prompt":"show me the detail",
   "followup":"<the response to that follow-up>"}

Input JUDGEMENTS.jsonl — one row per pair, joined on (task, variant, rep):
  {"task":"T2","variant":"with","rep":1,
   "in_full":true,"unre_summarised":true,"judge_model":"...","notes":"..."}

Emits scores.json (--out) + a markdown table on stdout, and appends A2 rows to
the harness RESULTS.jsonl (--emit-rows) on the same (task, variant, rep) join
keys the A1 rows use.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics

# The standard follow-up prompts the ratified A2 definition names. Recorded so a
# pair built with an off-battery prompt is visible as such rather than silently
# scored against a different question.
STANDARD_FOLLOWUPS = (
    "why?",
    "show me the detail",
    "what was the evidence?",
)

# A resolvable address: the durable pointer a compressed message is required to
# carry in place of the detail it dropped. Diagnostic only — see module docstring.
_ADDRESS_PATTERNS = (
    r"https?://\S+",  # PR / issue / doc URL
    r"\b(?:PR\s*)?#\d+\b",  # #944, PR #954
    r"\bdata/worklog/[\w.\-]+\.md\b",  # the spec's per-task worklog convention
    r"\b[\w.\-/]+\.(?:md|py|sh|jsonl|json|toml|ya?ml)\b",  # a repo path
)
_ADDRESS_RE = re.compile("|".join(_ADDRESS_PATTERNS), re.IGNORECASE)


def _load_rows(path):
    """Read a JSONL file, skipping blank and unparseable lines."""
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _key(row):
    return (row.get("task", ""), row.get("variant", ""), row.get("rep"))


def has_address(text):
    """Is a resolvable address present? Diagnostic, never gating (see docstring)."""
    return bool(_ADDRESS_RE.search(text or ""))


def score_structural(pair):
    """The zero-spend tier. Facts and one sound refutation; no tunables.

    `expanded` is the only structural signal that participates in the pass rule,
    and it participates in one direction only: a follow-up that is not longer
    than the message it was asked to expand cannot have returned the withheld
    detail in full. It refutes; it never confirms.
    """
    compressed = pair.get("compressed") or ""
    withheld = pair.get("withheld") or ""
    followup = pair.get("followup") or ""

    len_compressed = len(compressed)
    len_withheld = len(withheld)
    len_followup = len(followup)

    return {
        "len_compressed": len_compressed,
        "len_withheld": len_withheld,
        "len_followup": len_followup,
        # Raw ratios: reported for calibration, never thresholded in this module.
        "expansion_ratio": (len_followup / len_compressed) if len_compressed else None,
        "detail_coverage_ratio": (
            (len_followup / len_withheld) if len_withheld else None
        ),
        "expanded": len_followup > len_compressed,
        "address_present": has_address(compressed),
        "standard_followup": (pair.get("followup_prompt") or "").strip().lower()
        in STANDARD_FOLLOWUPS,
    }


def score_pair(pair, judgement=None, structural_only=False):
    """Score one message pair.

    recovered = expanded AND in_full AND unre_summarised

    Returns `recovered=False` on a structural refutation without consulting a
    judge, `recovered=None` (UNSCORED) when the semantic tier is unresolved, and
    a boolean otherwise. None is never coerced to False — see module docstring.
    """
    st = score_structural(pair)
    out = dict(st)
    out.update(
        {
            "task": pair.get("task", ""),
            "variant": pair.get("variant", ""),
            "rep": pair.get("rep"),
            "in_full": None,
            "unre_summarised": None,
            "judge_model": None,
            "recovered": None,
            "reason": "",
        }
    )

    # Structural refutation short-circuits: sound without a judge, and the whole
    # point of separating the tiers is that this costs nothing.
    if not st["expanded"]:
        out["recovered"] = False
        out["reason"] = "structural: follow-up did not expand on the message"
        return out

    if structural_only:
        out["reason"] = "structural-only: semantic tier not evaluated"
        return out

    if not judgement:
        out["reason"] = "unscored: no judgement for this pair"
        return out

    in_full = judgement.get("in_full")
    unre = judgement.get("unre_summarised")
    out["judge_model"] = judgement.get("judge_model")
    if in_full is None or unre is None:
        out["reason"] = "unscored: judgement missing in_full/unre_summarised"
        return out

    out["in_full"] = bool(in_full)
    out["unre_summarised"] = bool(unre)
    out["recovered"] = bool(in_full) and bool(unre)
    if not out["recovered"]:
        # Name which half failed — "returned but re-summarised" is the failure
        # mode the ratified wording exists to catch, and it is worth telling
        # apart from "did not return the detail at all".
        if not in_full:
            out["reason"] = "semantic: detail did not come back in full"
        else:
            out["reason"] = "semantic: detail came back re-summarised"
    return out


def compute(pairs, judgements=None, structural_only=False):
    """Score every pair, aggregate per (task, variant), and pair up the deltas."""
    jmap = {_key(j): j for j in (judgements or [])}
    scored = [
        score_pair(p, jmap.get(_key(p)), structural_only=structural_only) for p in pairs
    ]

    # Per (task, variant): the comparable score is the recovery RATE over the
    # pairs that could be scored. Unscored pairs are excluded and counted.
    groups = {}
    for s in scored:
        groups.setdefault((s["task"], s["variant"]), []).append(s)

    per_group = {}
    for (task, variant), rows in sorted(groups.items()):
        decided = [r["recovered"] for r in rows if r["recovered"] is not None]
        unscored = sum(1 for r in rows if r["recovered"] is None)
        per_group["%s/%s" % (task, variant)] = {
            "task": task,
            "variant": variant,
            "n_pairs": len(rows),
            "n_scored": len(decided),
            "n_unscored": unscored,
            "n_recovered": sum(1 for d in decided if d),
            # The comparable score. None when nothing could be scored — an
            # absent score, never a zero, which would read as total failure.
            "recovery_rate": (
                (sum(1 for d in decided if d) / len(decided)) if decided else None
            ),
            "address_present_rate": (
                statistics.fmean([1.0 if r["address_present"] else 0.0 for r in rows])
                if rows
                else None
            ),
        }

    # Control-vs-treatment delta per task. Reported as a magnitude and direction
    # only: A2's bar is not ratified, so this module states the comparison and
    # stops short of judging it.
    deltas = {}
    tasks = sorted({t for (t, _v) in groups})
    for task in tasks:
        wo = per_group.get("%s/without" % task, {}).get("recovery_rate")
        wi = per_group.get("%s/with" % task, {}).get("recovery_rate")
        deltas[task] = {
            "without": wo,
            "with": wi,
            "delta": (wi - wo) if (wo is not None and wi is not None) else None,
            "direction": (
                None
                if (wo is None or wi is None)
                else ("degraded" if wi < wo else ("improved" if wi > wo else "equal"))
            ),
        }

    total_unscored = sum(g["n_unscored"] for g in per_group.values())
    doc = {
        "axis": "A2_recoverability",
        "definition": (
            "after a compressed message, a follow-up must return the withheld "
            "detail in full and unre-summarised"
        ),
        "bar": None,
        "bar_status": (
            "NOT RATIFIED — A2 is must-not-degrade vs control; this module emits "
            "a comparable score and no verdict"
        ),
        "structural_only": bool(structural_only),
        "pairs": scored,
        "per_group": per_group,
        "control_vs_treatment": deltas,
        "coverage": {
            "n_pairs": len(scored),
            "n_unscored": total_unscored,
            "judgements_supplied": len(jmap),
            # Coverage-honesty: an unscored pair is disclosed here rather than
            # being absorbed into the rate as if it had been measured.
            "note": (
                "unscored pairs are excluded from recovery_rate and are NOT "
                "counted as failures"
            ),
        },
    }
    return doc


# ----------------------------------------------------------------------
# Dry-run fixtures — synthetic inputs that drive the REAL scoring logic
# ----------------------------------------------------------------------
# Zero model calls. Every branch of score_pair is exercised: a clean recovery, a
# structural refutation, a re-summarised follow-up (the load-bearing failure),
# and an unscored pair. CI runs this path.


def _dry_run_fixtures():
    long_detail = (
        "348 collection errors; 2930 collected against a real suite of 2156; "
        "774 foreign tests adopted from three bots' projects/ checkouts; 307s "
        "wall clock. Node-ID sets diffed byte-identical after the fix."
    )
    pairs = [
        # Recovers cleanly: expands, and the judge confirms both halves.
        {
            "task": "T3",
            "variant": "with",
            "rep": 1,
            "compressed": "pytest collection fixed. PR #954",
            "withheld": long_detail,
            "followup_prompt": "show me the detail",
            "followup": long_detail + " Full run log at data/worklog/t-1.md.",
        },
        # Structural refutation: the follow-up did not expand at all. Scored
        # False with NO judgement supplied, which is the point of the cheap tier.
        {
            "task": "T3",
            "variant": "with",
            "rep": 2,
            "compressed": "pytest collection fixed, see the PR for numbers. #954",
            "withheld": long_detail,
            "followup_prompt": "why?",
            "followup": "As above.",
        },
        # The load-bearing failure: it expanded, but into a shorter summary of
        # the summary. Structurally indistinguishable from a pass; only the judge
        # catches it.
        {
            "task": "T2",
            "variant": "with",
            "rep": 1,
            "compressed": "Audit done. #892",
            "withheld": long_detail,
            "followup_prompt": "what was the evidence?",
            # Every fact from `long_detail` is technically present, but restated
            # tersely instead of returned. Longer than the message, so the
            # structural tier passes it through; only the judge separates this
            # from a genuine expansion.
            "followup": (
                "Roughly: 348 errors, ~2930 vs 2156 collected, ~774 foreign "
                "tests, ~307s, and the node IDs matched after the fix."
            ),
        },
        # Control arm, recovers.
        {
            "task": "T3",
            "variant": "without",
            "rep": 1,
            "compressed": "pytest collection fixed.",
            "withheld": long_detail,
            "followup_prompt": "show me the detail",
            "followup": long_detail,
        },
        {
            "task": "T2",
            "variant": "without",
            "rep": 1,
            "compressed": "Audit done.",
            "withheld": long_detail,
            "followup_prompt": "what was the evidence?",
            "followup": long_detail,
        },
        # Unscored: expands, but no judgement exists. Must land as None.
        {
            "task": "T1",
            "variant": "with",
            "rep": 1,
            "compressed": "Review posted. #944",
            "withheld": long_detail,
            "followup_prompt": "why?",
            "followup": long_detail,
        },
    ]
    judgements = [
        {
            "task": "T3",
            "variant": "with",
            "rep": 1,
            "in_full": True,
            "unre_summarised": True,
            "judge_model": "dry-run-stub",
        },
        {
            # Every point survives, but delivered as a compressed restatement
            # rather than as the detail — in_full true, unre_summarised false.
            # This is the case the ratified wording exists to catch.
            "task": "T2",
            "variant": "with",
            "rep": 1,
            "in_full": True,
            "unre_summarised": False,
            "judge_model": "dry-run-stub",
        },
        {
            "task": "T3",
            "variant": "without",
            "rep": 1,
            "in_full": True,
            "unre_summarised": True,
            "judge_model": "dry-run-stub",
        },
        {
            "task": "T2",
            "variant": "without",
            "rep": 1,
            "in_full": True,
            "unre_summarised": True,
            "judge_model": "dry-run-stub",
        },
    ]
    return pairs, judgements


def _fmt(x, pct=False):
    if x is None:
        return "—"
    return ("%.0f%%" % (100.0 * x)) if pct else ("%.2f" % x)


def render_table(doc):
    lines = []
    lines.append("## A2 — recoverability (score, not verdict)")
    lines.append("")
    lines.append("bar: %s" % doc["bar_status"])
    if doc["structural_only"]:
        lines.append("mode: STRUCTURAL-ONLY — semantic tier not evaluated")
    lines.append("")
    lines.append("| task | variant | pairs | scored | unscored | recovery |")
    lines.append("|---|---|---|---|---|---|")
    for _k, g in sorted(doc["per_group"].items()):
        lines.append(
            "| %s | %s | %d | %d | %d | %s |"
            % (
                g["task"],
                g["variant"],
                g["n_pairs"],
                g["n_scored"],
                g["n_unscored"],
                _fmt(g["recovery_rate"], pct=True),
            )
        )
    lines.append("")
    lines.append("| task | control | treatment | delta | direction |")
    lines.append("|---|---|---|---|---|")
    for task, d in sorted(doc["control_vs_treatment"].items()):
        lines.append(
            "| %s | %s | %s | %s | %s |"
            % (
                task,
                _fmt(d["without"], pct=True),
                _fmt(d["with"], pct=True),
                _fmt(d["delta"], pct=True),
                d["direction"] or "—",
            )
        )
    cov = doc["coverage"]
    lines.append("")
    lines.append(
        "coverage: %d pair(s), %d unscored (excluded from recovery, not counted "
        "as failures), %d judgement(s) supplied"
        % (cov["n_pairs"], cov["n_unscored"], cov["judgements_supplied"])
    )
    return "\n".join(lines)


def emit_rows(doc, path):
    """Append A2 rows to the harness RESULTS.jsonl on the A1 join keys."""
    with open(path, "a") as fh:
        for p in doc["pairs"]:
            fh.write(
                json.dumps(
                    {
                        "task": p["task"],
                        "variant": p["variant"],
                        "rep": p["rep"],
                        "axis": "A2_recoverability",
                        "a2_recovered": p["recovered"],
                        "a2_expanded": p["expanded"],
                        "a2_in_full": p["in_full"],
                        "a2_unre_summarised": p["unre_summarised"],
                        "a2_address_present": p["address_present"],
                        "a2_len_compressed": p["len_compressed"],
                        "a2_len_withheld": p["len_withheld"],
                        "a2_len_followup": p["len_followup"],
                        "a2_reason": p["reason"],
                        "judge_model": p["judge_model"],
                    },
                    sort_keys=True,
                )
                + "\n"
            )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="A2 recoverability scorer (#881). Emits a score, not a verdict."
    )
    ap.add_argument("pairs", nargs="?", default="", help="pairs.jsonl")
    ap.add_argument("--judgements", default="", help="judgements.jsonl from the judge")
    ap.add_argument(
        "--structural-only",
        action="store_true",
        help="run only the zero-spend tier (no judgements consulted)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="CI-safe: drive the real scoring logic on synthetic inputs, zero model calls",
    )
    ap.add_argument("--out", default="", help="write scores.json here")
    ap.add_argument("--emit-rows", default="", help="append A2 rows to RESULTS.jsonl")
    args = ap.parse_args(argv)

    if args.dry_run:
        pairs, judgements = _dry_run_fixtures()
    else:
        if not args.pairs:
            ap.error("PAIRS.jsonl is required (or use --dry-run)")
        pairs = _load_rows(args.pairs)
        judgements = _load_rows(args.judgements) if args.judgements else []

    doc = compute(pairs, judgements, structural_only=args.structural_only)

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(doc, fh, indent=2)
    if args.emit_rows:
        emit_rows(doc, args.emit_rows)

    print(render_table(doc))
    # Deliberately NOT a verdict line. A2's bar is unratified; the harness reads
    # the score and the delta, and a human decides what they mean.
    print("A2_UNSCORED=%d" % doc["coverage"]["n_unscored"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
