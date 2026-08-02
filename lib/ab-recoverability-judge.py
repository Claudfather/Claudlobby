#!/usr/bin/env python3
"""A2 semantic judge for the #881 comms-topology eval.

Fills the slot `lib/ab-recoverability-scorer.py` deliberately left open. That
module implements the structural tier and consumes semantic verdicts from a
`--judgements` file; it never calls a model. This module produces that file.

The division is the point, and it is preserved here:

    judge  -> emits judgements          (this module; spends)
    scorer -> consumes judgements       (the other module; free)
    neither decides anything            (no threshold, no PASS/FAIL, anywhere)

The judgements contract is NOT redefined here. It is the scorer's, joined on
(task, variant, rep):

    {"task":"T2","variant":"with","rep":1,
     "in_full":true,"unre_summarised":true,"judge_model":"...","notes":"..."}

What the judge decides
----------------------
Given the compressed message, the detail compressed out of it, and the response
to a standard follow-up: did the follow-up return that detail IN FULL and
UNRE-SUMMARISED. Two independent booleans — a response can be complete and still
re-summarised, which still fails. That cell is the whole anti-lossy guard: if a
judge waves it through, A2 becomes decoration and nobody finds out.

Fail closed
-----------
An unparseable or errored judgement is OMITTED, never guessed. The scorer treats
a pair with no judgement as UNSCORED (`None`), never as a failure, so a flaky
judge degrades coverage — disclosed — instead of manufacturing a verdict.

Cost posture
------------
Two keys for real calls, mirroring the harness: `--real` AND `AB_JUDGE_REAL=1`.
Deliberately NOT `AB_EVAL_REAL` — that key gates the eval matrix, and this module
must not be able to ride in on it. `--dry-run` drives the real prompt-building
and parsing on synthetic verdicts with zero model calls, and is what CI runs.

Calibration is mandatory before any matrix run
----------------------------------------------
`--calibrate GOLD.jsonl` scores the judge against hand-labelled pairs and reports
agreement PER BOOLEAN, never pooled. Pooling hides the failure that matters:
`in_full` is comparatively easy and `unre_summarised` is the subtle one, so a
pooled number can look healthy while the load-bearing half is a coin flip. The
fixtures under tests/fixtures/ carry balanced 5/5 labels on both booleans so
agreement cannot be inflated by a skewed prior.

Usage:
  ab-recoverability-judge.py PAIRS.jsonl --dry-run [--out J.jsonl]
  ab-recoverability-judge.py PAIRS.jsonl --real [--model sonnet] [--out J.jsonl]
  ab-recoverability-judge.py PAIRS.jsonl --calibrate GOLD.jsonl [--dry-run|--real]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess

DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT_S = 180

# The rubric IS the ratified A2 definition operationalised. The two clauses that
# carry the hard cases are stated explicitly rather than left to inference:
#   - paraphrase is NOT re-summarisation  (else honest variants fail)
#   - complete-but-terse IS re-summarisation (else the guard is decoration)
# Calibration measures whether the judge can APPLY this consistently, which is
# the real question; it is not a test of whether it can guess the definition.
RUBRIC = """\
You are scoring one message pair from a communications experiment. Answer only \
with JSON.

You are given three things:
  COMPRESSED — a short outbound message that deliberately omitted detail.
  WITHHELD   — the detail that was omitted from it.
  FOLLOWUP   — the reply to a follow-up asking for that detail.

Decide two INDEPENDENT booleans about FOLLOWUP.

1. in_full — does every substantive fact in WITHHELD appear in FOLLOWUP?
   - Judge content, not wording. A faithful paraphrase still counts as present.
   - A figure that is altered, rounded, or approximated is NOT present. "about
     280 seconds" does not carry "307 seconds".
   - Missing any material fact -> false.

2. unre_summarised — does FOLLOWUP deliver the detail, or a condensed
   restatement of it?
   - true  = delivered as the detail. Rewording, reordering, restructuring, or
     adding extra material are all fine and keep this true.
   - false = compressed: gist-level prose, telegraphic shorthand, or a bare list
     that strips the relations between the facts.
   - Length alone does not decide this. A complete paraphrase of comparable
     fidelity is TRUE even if every word differs.
   - IMPORTANT: a response that contains every fact but delivers them as terse
     shorthand or a bare enumeration is FALSE. Completeness does not rescue it.
     These two booleans can legitimately be (true, false).

Reply with exactly this JSON object and nothing else:
{"in_full": <true|false>, "unre_summarised": <true|false>, "notes": "<= 20 words"}

COMPRESSED:
%(compressed)s

WITHHELD:
%(withheld)s

FOLLOWUP:
%(followup)s
"""


def _load_rows(path):
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


def build_prompt(pair):
    """Render the rubric for one pair. Pure — exercised by the dry-run path."""
    return RUBRIC % {
        "compressed": pair.get("compressed") or "",
        "withheld": pair.get("withheld") or "",
        "followup": pair.get("followup") or "",
    }


_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def parse_verdict(text):
    """Extract {in_full, unre_summarised, notes} from a model reply.

    Returns None when the reply cannot be read as a verdict — fail closed, so
    the pair lands UNSCORED in the scorer rather than being guessed.
    """
    if not text:
        return None
    m = _JSON_OBJ.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    if not isinstance(obj.get("in_full"), bool):
        return None
    if not isinstance(obj.get("unre_summarised"), bool):
        return None
    return {
        "in_full": obj["in_full"],
        "unre_summarised": obj["unre_summarised"],
        "notes": str(obj.get("notes", ""))[:200],
    }


def call_judge(prompt, model=DEFAULT_MODEL, timeout_s=DEFAULT_TIMEOUT_S):
    """One headless judge call. Returns (verdict|None, model_name|None)."""
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, None
    if proc.returncode != 0:
        return None, None
    try:
        env = json.loads(proc.stdout)
    except ValueError:
        return None, None
    if env.get("is_error"):
        return None, None
    # Resolve the model actually served, not the alias asked for — a verdict is a
    # statement about a specific model version (eval-workflow's version-pinning
    # rule), and "sonnet" is not a version.
    served = None
    usage = env.get("modelUsage") or {}
    if isinstance(usage, dict) and usage:
        served = sorted(usage)[0]
    return parse_verdict(env.get("result")), served or model


# ----------------------------------------------------------------------
# Dry run — zero model calls, drives the real prompt + parse path
# ----------------------------------------------------------------------
# The stub is DETERMINISTIC and deliberately naive: it says "yes, complete and
# unre-summarised" whenever the follow-up is at least as long as the withheld
# detail. That is exactly the rubber-stamp behaviour calibration exists to
# detect, so the dry-run path also demonstrates what a bad judge looks like
# rather than manufacturing an agreeable one.


def stub_verdict(pair):
    withheld = pair.get("withheld") or ""
    followup = pair.get("followup") or ""
    ok = len(followup) >= len(withheld)
    return {
        "in_full": ok,
        "unre_summarised": ok,
        "notes": "dry-run stub: length heuristic, not a judgement",
    }


def judge_pairs(
    pairs, dry_run=True, model=DEFAULT_MODEL, timeout_s=DEFAULT_TIMEOUT_S, progress=None
):
    """Judge every pair. Omits (never guesses) a pair the judge could not read."""
    out = []
    for i, pair in enumerate(pairs):
        if dry_run:
            verdict, served = stub_verdict(pair), "dry-run-stub"
        else:
            verdict, served = call_judge(build_prompt(pair), model, timeout_s)
        if progress:
            progress(i, pair, verdict)
        if verdict is None:
            continue  # fail closed -> scorer marks it UNSCORED
        out.append(
            {
                "task": pair.get("task", ""),
                "variant": pair.get("variant", ""),
                "rep": pair.get("rep"),
                "in_full": verdict["in_full"],
                "unre_summarised": verdict["unre_summarised"],
                "judge_model": served,
                "notes": verdict["notes"],
            }
        )
    return out


# ----------------------------------------------------------------------
# Calibration — per-boolean agreement, never pooled
# ----------------------------------------------------------------------


def calibrate(judgements, gold):
    """Agreement against hand labels, reported per boolean.

    Pooling is refused on purpose: in_full is the easy half and unre_summarised
    is the subtle one, so one blended number can read healthy while the
    load-bearing half is a coin flip.
    """
    gmap = {_key(g): g for g in gold}
    jmap = {_key(j): j for j in judgements}

    per_axis = {}
    for axis in ("in_full", "unre_summarised"):
        agree = disagree = 0
        misses = []
        for k, g in sorted(gmap.items()):
            j = jmap.get(k)
            if j is None:
                continue
            if j[axis] == g[axis]:
                agree += 1
            else:
                disagree += 1
                misses.append(
                    {
                        "task": k[0],
                        "gold": g[axis],
                        "judge": j[axis],
                        "case": g.get("case", ""),
                        "notes": j.get("notes", ""),
                    }
                )
        n = agree + disagree
        per_axis[axis] = {
            "n_compared": n,
            "n_agree": agree,
            "n_disagree": disagree,
            "agreement": (agree / n) if n else None,
            "disagreements": misses,
        }

    unjudged = [
        {"task": k[0], "case": gmap[k].get("case", "")}
        for k in sorted(gmap)
        if k not in jmap
    ]

    # The gate the dispatch names: a disagreement on unre_summarised is the one
    # that must stop the run, because that axis is what the anti-lossy guard
    # rests on. Reported as a fact, not enforced as a threshold — this module
    # decides nothing.
    return {
        "axis": "A2_semantic_judge_calibration",
        "per_axis": per_axis,
        "n_gold": len(gmap),
        "n_judged": len(jmap),
        "unjudged": unjudged,
        "unre_summarised_clean": per_axis["unre_summarised"]["n_disagree"] == 0,
        "note": (
            "Agreement is reported per boolean and never pooled. No threshold is "
            "applied here; this module emits judgements and measurements only."
        ),
    }


def render_calibration(doc):
    lines = ["## A2 semantic judge — calibration against hand labels", ""]
    lines.append("| axis | compared | agree | disagree | agreement |")
    lines.append("|---|---|---|---|---|")
    for axis in ("in_full", "unre_summarised"):
        a = doc["per_axis"][axis]
        pct = "—" if a["agreement"] is None else "%.0f%%" % (100 * a["agreement"])
        lines.append(
            "| `%s` | %d | %d | %d | %s |"
            % (axis, a["n_compared"], a["n_agree"], a["n_disagree"], pct)
        )
    lines.append("")
    if doc["unjudged"]:
        lines.append(
            "unjudged (fail-closed, omitted rather than guessed): "
            + ", ".join("%s" % u["task"] for u in doc["unjudged"])
        )
        lines.append("")
    for axis in ("in_full", "unre_summarised"):
        for d in doc["per_axis"][axis]["disagreements"]:
            lines.append(
                "- DISAGREE `%s` on %s — gold=%s judge=%s — %s%s"
                % (
                    axis,
                    d["task"],
                    d["gold"],
                    d["judge"],
                    d["case"],
                    (" — judge notes: %s" % d["notes"]) if d["notes"] else "",
                )
            )
    if doc["per_axis"]["unre_summarised"]["n_disagree"]:
        lines += [
            "",
            "**STOP — the judge disagrees on `unre_summarised`.**",
            "",
            "That is the subtle judgement and the one the whole anti-lossy guard "
            "rests on. A judge that cannot hold this axis turns A2 into "
            "decoration, and a matrix run would not reveal it. Do not proceed to "
            "a matrix run on this judge configuration.",
        ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="A2 semantic judge (#881). Emits judgements; decides nothing."
    )
    ap.add_argument("pairs", help="pairs.jsonl")
    ap.add_argument("--out", default="", help="write judgements.jsonl here")
    ap.add_argument("--dry-run", action="store_true", help="zero model calls (CI-safe)")
    ap.add_argument(
        "--real",
        action="store_true",
        help="make real judge calls; also requires AB_JUDGE_REAL=1",
    )
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--calibrate", default="", help="gold.jsonl to score against")
    ap.add_argument("--calibration-out", default="", help="write calibration.json here")
    args = ap.parse_args(argv)

    if not args.dry_run and not args.real:
        ap.error(
            "pass --dry-run (CI-safe) or --real (opt-in, also needs AB_JUDGE_REAL=1)"
        )
    if args.real and args.dry_run:
        ap.error("--real and --dry-run are mutually exclusive")
    if args.real and os.environ.get("AB_JUDGE_REAL") != "1":
        # Two keys, and deliberately not the matrix's key.
        ap.error(
            "real judging is opt-in: set AB_JUDGE_REAL=1 as well as --real. "
            "(AB_EVAL_REAL does NOT enable this and must not be used for it.)"
        )

    pairs = _load_rows(args.pairs)

    def _progress(i, pair, verdict):
        if args.real:
            state = "omitted (unreadable)" if verdict is None else "ok"
            print(
                "  judged %d/%d %s: %s" % (i + 1, len(pairs), pair.get("task"), state)
            )

    judgements = judge_pairs(
        pairs,
        dry_run=args.dry_run,
        model=args.model,
        timeout_s=args.timeout_s,
        progress=_progress,
    )

    if args.out:
        with open(args.out, "w") as fh:
            for j in judgements:
                fh.write(json.dumps(j, sort_keys=True) + "\n")

    if args.calibrate:
        doc = calibrate(judgements, _load_rows(args.calibrate))
        if args.calibration_out:
            with open(args.calibration_out, "w") as fh:
                json.dump(doc, fh, indent=2)
        print(render_calibration(doc))
        # Reported, not enforced: a non-zero exit here would be this module
        # deciding something, which is not its job.
        print(
            "UNRE_SUMMARISED_DISAGREEMENTS=%d"
            % doc["per_axis"]["unre_summarised"]["n_disagree"]
        )
    else:
        print(
            "judged %d/%d pair(s); %d omitted (fail-closed)"
            % (len(judgements), len(pairs), len(pairs) - len(judgements))
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
