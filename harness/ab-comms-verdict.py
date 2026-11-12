#!/usr/bin/env python3
"""Pass-bar / verdict computation for the #729 stage-C A/B comms-eval harness.

Reads a results.jsonl (one row per task x rep x variant, written by
harness/ab-comms-eval.sh), pairs per-rep deltas by task, computes a SEEDED bootstrap
CI on the median paired relative reduction per axis, and applies the pass-bar.

Standalone claudlobby/_runtime_scripts/ Python (stdlib-only, shell-invokable) following the
claudlobby/_runtime_scripts/dispatch-overdue.py and claudlobby.transcript_usage precedent — the eval harness
shells out to it, and it is directly unit-testable (tests/test_ab_comms_eval.py).
F2 ratifies the threshold and the quality scorer; keeping this a module (not an
inline heredoc) is what lets F2 land its work in a testable place.

Pass-bar (per task type):
  PASS iff protocol_sensitive relative-reduction CI-low >= --threshold (F2 T)
       AND cost_weighted_total relative-reduction CI-low >= --cost-threshold
       (CO-PRIMARY; default 0.0 = no-regression, F2 may raise)
       AND the quality gate passes AND zero mechanical failures.
  FAIL iff a co-primary axis CI-high is below its bar, or a hard gate fails.
  STRADDLE (CI spans the bar) -> extend reps; at --reps-max it becomes
  INCONCLUSIVE. INCONCLUSIVE is NEVER rounded up to PASS. With no --threshold
  (the F2 sentinel) every task is INCONCLUSIVE by construction.
  A task with an "unscored" quality row (the F2 stub's) is INCONCLUSIVE
  ("quality unscored") whatever the threshold, token axes or mechanical check.

Pins: the models, weights, threshold, cost threshold, reps, claude version and
protocol hash, and three run pins the harness takes before any cell: the sha256
of the battery the cells got (--battery-sha256), the commit the harness tree is
at (--harness-revision, "-dirty" when its tracked files differ from it) and,
once per arm, the sha256 of what that arm's session reads in its composed bot
directory (--composed-sha256 ARM=SHA). A missing pin is recorded as null. With
--real the verdict is a real run's, and it is refused when a run pin is
missing: exit 3, each missing pin named on stderr, nothing written.

Usage:
  ab-comms-verdict.py RESULTS.jsonl [--out verdict.json] [--threshold F]
      [--cost-threshold F] [--reps-now N] [--reps-max N] [--claude-version S]
      [--proto-hash S] [--proto-placeholder] [--weights-file F]
      [--battery-sha256 S] [--harness-revision S] [--composed-sha256 ARM=S]...
      [--real]

Emits verdict.json (--out) + a markdown table on stdout + a final
ANY_STRADDLE=<0|1> line the harness stopping rule reads.
"""

import argparse
import json
import os
import random
import re
import statistics
import sys

# Pre-registered, deterministic bootstrap: seeded so dry-run and tests reproduce.
BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 0
CI_PCT = 90  # two-sided -> 5th and 95th percentiles of the bootstrap medians

REFUSED = 3  # the exit status of a real run's verdict that lacks a run pin
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


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


def rel_reductions(task_reps, axis):
    """Paired per-rep relative reductions (without - with) / without for one axis."""
    reds = []
    for _rep, variants in sorted(task_reps.items()):
        wo, wi = variants.get("without"), variants.get("with")
        if not wo or not wi:
            continue
        base = wo.get(axis)
        if base in (None, 0):
            continue
        reds.append((base - wi.get(axis, 0)) / base)
    return reds


def bootstrap_ci(xs):
    if not xs:
        return (None, None)
    if len(xs) == 1:
        return (xs[0], xs[0])
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(xs)
    meds = sorted(
        statistics.median([xs[rng.randrange(n)] for _ in range(n)])
        for _ in range(BOOTSTRAP_N)
    )
    tail = (100 - CI_PCT) / 2 / 100
    lo = meds[int(tail * len(meds))]
    hi = meds[min(int((1 - tail) * len(meds)), len(meds) - 1)]
    return (lo, hi)


def _median(xs):
    return statistics.median(xs) if xs else None


def _task_verdict(
    ps,
    cw,
    ps_ci,
    cw_ci,
    mech_ok,
    quality,
    threshold,
    cost_threshold,
    reps_now,
    reps_max,
):
    """Decide one task type. Returns (verdict, reason, sets_straddle)."""
    if quality == "unscored":
        return "INCONCLUSIVE", "quality unscored", False
    if not threshold:
        return "INCONCLUSIVE", "no F2-ratified threshold (T pending)", False
    if not ps or not cw:
        return "INCONCLUSIVE", "insufficient paired data", False
    if not mech_ok:
        return "FAIL", "mechanical invariant failure", False
    if quality != "pass":
        return "FAIL", "quality gate failure", False
    T = float(threshold)
    ps_pass = ps_ci[0] is not None and ps_ci[0] >= T
    ps_fail = ps_ci[1] is not None and ps_ci[1] < T
    cw_pass = cw_ci[0] is not None and cw_ci[0] >= cost_threshold
    cw_fail = cw_ci[1] is not None and cw_ci[1] < cost_threshold
    if ps_pass and cw_pass:
        return "PASS", "both co-primary axes clear the bar", False
    if ps_fail or cw_fail:
        return "FAIL", "a co-primary axis CI is below its bar", False
    if reps_now < reps_max:
        return "STRADDLE", "CI straddles the bar; extend reps", True
    return "INCONCLUSIVE", "CI straddles the bar at reps-max", False


def compute(
    rows, threshold, cost_threshold, reps_now, reps_max, weights=None, pins_extra=None
):
    tasks = {}
    models = set()
    for r in rows:
        tasks.setdefault(r["task"], {}).setdefault(r["rep"], {})[r["variant"]] = r
        for m in str(r.get("model", "")).split("|"):
            if m and m != "-":
                models.add(m)

    per_task = []
    any_straddle = False
    for task in sorted(tasks):
        treps = tasks[task]
        ps = rel_reductions(treps, "protocol_sensitive")
        cw = rel_reductions(treps, "cost_weighted_total")
        all_rows = [v for reps in treps.values() for v in reps.values()]
        mech_ok = all(v.get("mech_ok", False) for v in all_rows)
        gates = [(v.get("quality") or {}).get("gate") for v in all_rows]
        if "unscored" in gates:
            quality = "unscored"
        else:
            quality = "pass" if all(g == "pass" for g in gates) else "fail"
        ps_ci, cw_ci = bootstrap_ci(ps), bootstrap_ci(cw)
        verdict, reason, straddled = _task_verdict(
            ps,
            cw,
            ps_ci,
            cw_ci,
            mech_ok,
            quality,
            threshold,
            cost_threshold,
            reps_now,
            reps_max,
        )
        any_straddle = any_straddle or straddled
        per_task.append(
            {
                "task": task,
                "reps": len(ps),
                "protocol_sensitive": {
                    "median_reduction": _median(ps),
                    "ci": list(ps_ci),
                },
                "cost_weighted_total": {
                    "median_reduction": _median(cw),
                    "ci": list(cw_ci),
                },
                "quality": quality,
                "verdict": verdict,
                "reason": reason,
            }
        )

    verdicts = [t["verdict"] for t in per_task]
    if not verdicts:
        overall = "INCONCLUSIVE"
    elif "FAIL" in verdicts:
        overall = "FAIL"
    elif all(v == "PASS" for v in verdicts):
        overall = "PASS"
    elif any_straddle:
        overall = "STRADDLE"
    else:
        overall = "INCONCLUSIVE"

    pins = {
        "models": sorted(models),
        "weights": weights,
        "threshold": (float(threshold) if threshold else None),
        "cost_threshold": cost_threshold,
        "reps": reps_now,
        "reps_max": reps_max,
    }
    pins.update(pins_extra or {})
    doc = {
        "overall": overall,
        "per_task": per_task,
        "pins": pins,
        "stats": {
            "bootstrap_n": BOOTSTRAP_N,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "ci_pct": CI_PCT,
            "statistic": "median of paired per-rep relative reductions",
        },
        "note": (
            "protocol_sensitive AND cost_weighted_total are CO-PRIMARY; "
            "INCONCLUSIVE is never rounded up to PASS; quality scorer is an F2 stub."
        ),
    }
    return doc, any_straddle


def unpinned(pins, arms):
    """The run pins a real verdict lacks, as (name, value) pairs. The battery and
    each arm's composed output are sha256 digests; the harness revision is a
    commit, so a "-dirty" tree is not pinned."""
    wanted = [
        ("battery_sha256", pins["battery_sha256"], _SHA256),
        ("harness_revision", pins["harness_revision"], _COMMIT),
    ] + [
        ("composed_sha256." + arm, pins["composed_sha256"].get(arm), _SHA256)
        for arm in arms
    ]
    return [(name, v) for name, v, form in wanted if not form.fullmatch(v or "")]


def _arm_pin(text):
    arm, sep, sha = text.partition("=")
    if not sep or not arm:
        raise argparse.ArgumentTypeError("expected ARM=SHA256, got %r" % text)
    return arm, sha


def _pct(x):
    return "n/a" if x is None else "%.1f%%" % (100 * x)


def _cis(ci):
    return "n/a" if ci[0] is None else "[%s, %s]" % (_pct(ci[0]), _pct(ci[1]))


def render_table(doc):
    lines = [
        "| task | reps | ps reduction | ps CI | cwt reduction | cwt CI | quality | verdict |",
        "|------|------|--------------|-------|---------------|--------|---------|---------|",
    ]
    for t in doc["per_task"]:
        lines.append(
            "| %s | %d | %s | %s | %s | %s | %s | %s |"
            % (
                t["task"],
                t["reps"],
                _pct(t["protocol_sensitive"]["median_reduction"]),
                _cis(t["protocol_sensitive"]["ci"]),
                _pct(t["cost_weighted_total"]["median_reduction"]),
                _cis(t["cost_weighted_total"]["ci"]),
                t["quality"],
                t["verdict"],
            )
        )
    lines.append("")
    lines.append("OVERALL: %s" % doc["overall"])
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="A/B comms-eval pass-bar / verdict.")
    ap.add_argument("results", help="results.jsonl written by ab-comms-eval.sh")
    ap.add_argument("--out", default="", help="write verdict.json here")
    ap.add_argument(
        "--threshold",
        default="",
        help="F2 T: required protocol_sensitive reduction (empty -> INCONCLUSIVE)",
    )
    ap.add_argument(
        "--cost-threshold",
        type=float,
        default=0.0,
        help="required cost_weighted_total reduction (co-primary; default 0.0)",
    )
    ap.add_argument("--reps-now", type=int, default=0)
    ap.add_argument("--reps-max", type=int, default=0)
    ap.add_argument("--claude-version", default="unknown")
    ap.add_argument("--proto-hash", default="unknown")
    ap.add_argument("--proto-placeholder", action="store_true")
    ap.add_argument("--weights-file", default="")
    ap.add_argument(
        "--battery-sha256", default="", help="sha256 of the battery the cells got"
    )
    ap.add_argument(
        "--harness-revision",
        default="",
        help="the harness tree's commit (-dirty when its tracked files differ)",
    )
    ap.add_argument(
        "--composed-sha256",
        type=_arm_pin,
        action="append",
        default=[],
        metavar="ARM=SHA256",
        help="sha256 of what one arm's session reads (once per arm)",
    )
    ap.add_argument(
        "--real",
        action="store_true",
        help="a real run's verdict: refused (exit 3) when a run pin is missing",
    )
    args = ap.parse_args(argv)

    weights = None
    if args.weights_file and os.path.exists(args.weights_file):
        try:
            weights = json.load(open(args.weights_file))
        except (ValueError, OSError):
            weights = None

    rows = _load_rows(args.results)
    pins_extra = {
        "claude_version": args.claude_version,
        "protocol_file_sha256": args.proto_hash,
        "protocol_is_placeholder": args.proto_placeholder,
        "battery_sha256": args.battery_sha256 or None,
        "harness_revision": args.harness_revision or None,
        "composed_sha256": {arm: sha or None for arm, sha in args.composed_sha256},
    }
    if args.real:
        gaps = unpinned(pins_extra, sorted({r["variant"] for r in rows}))
        if gaps:
            print(
                "ab-comms-verdict: refused: a real run's verdict needs every run pin,"
                " and these are not pinned: "
                + "; ".join(
                    "%s (%s)" % (name, "none" if value is None else json.dumps(value))
                    for name, value in gaps
                ),
                file=sys.stderr,
            )
            return REFUSED
    doc, any_straddle = compute(
        rows,
        threshold=args.threshold.strip(),
        cost_threshold=args.cost_threshold,
        reps_now=args.reps_now,
        reps_max=args.reps_max,
        weights=weights,
        pins_extra=pins_extra,
    )
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(doc, fh, indent=2)
    print(render_table(doc))
    print("ANY_STRADDLE=%d" % (1 if any_straddle else 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
