#!/usr/bin/env python3
"""#843 boot-strand sampler summary — the statistics half of boot-strand-sampler.sh.

Standalone stdlib module (dispatch-overdue.py precedent) so the interval math
is unit-testable instead of buried in a heredoc. Reads the sampler's rows.jsonl
and prints the measurement WITH its uncertainty attached: an exact
Clopper-Pearson 95% interval on the strand rate, next to the pre-fix baseline
(2 strands in 4 tracked restarts, #843) shown with ITS interval — which spans
nearly the whole unit line, the visible form of "the null is poorly estimated
and there is no crisp threshold at which the fix becomes proven".

Exit: 0 summary printed · 1 no valid boots (a sample of others-only or an
empty/unreadable rows file must not read as a measurement).
"""

from __future__ import annotations

import json
import sys
from math import comb

# The #843 pre-fix baseline: 2 strands in 4 deliberately tracked restarts
# (mason clean, astrid strand, alex strand, clog clean). n=4 — an order of
# magnitude, not an estimate.
BASELINE_STRANDS = 2
BASELINE_N = 4


def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), exact via math.comb."""
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    return sum(comb(n, i) * p**i * (1.0 - p) ** (n - i) for i in range(0, k + 1))


def _bisect(f, lo: float, hi: float, iters: int = 200) -> float:
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if f(mid):
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


def cp_interval(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    """Exact Clopper-Pearson two-sided interval for k successes in n trials."""
    if not 0 <= k <= n or n <= 0:
        raise ValueError(f"bad k/n: {k}/{n}")
    alpha = 1.0 - conf
    # Lower: largest p with P(X >= k | p) <= alpha/2  ==  P(X <= k-1) >= 1-a/2.
    lower = (
        0.0
        if k == 0
        else _bisect(lambda p: 1.0 - _binom_cdf(k - 1, n, p) > alpha / 2, 0.0, 1.0)
    )
    # Upper: smallest p with P(X <= k | p) <= alpha/2.
    upper = (
        1.0 if k == n else _bisect(lambda p: _binom_cdf(k, n, p) < alpha / 2, 0.0, 1.0)
    )
    return (lower, upper)


def fisher_one_sided(k1: int, n1: int, k2: int, n2: int) -> float:
    """P(second group has >= k2 successes | margins) — conditional exact test.

    Illustrative only at these sizes: with the baseline at n=4 the test has
    almost no power and its p-value must not be read as a verdict.
    """
    total_k = k1 + k2
    total_n = n1 + n2
    denom = comb(total_n, total_k)
    p = 0.0
    for x in range(k2, min(n2, total_k) + 1):
        if total_k - x <= n1:
            p += comb(n2, x) * comb(n1, total_k - x) / denom
    return min(p, 1.0)


def load_rows(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except OSError as exc:
        print(f"boot-strand-summary: cannot read {path}: {exc}", file=sys.stderr)
    return rows


def summarize(rows: list[dict]) -> tuple[str, int]:
    """(report text, exit code) for a sampler run's rows."""
    sample = [r for r in rows if r.get("kind") == "sample"]
    warmup = [r for r in rows if r.get("kind") == "warmup"]
    strands = [r for r in sample if r.get("outcome") == "strand"]
    clean = [r for r in sample if r.get("outcome") == "clean"]
    other = [r for r in sample if r.get("outcome") not in ("strand", "clean")]
    retry_saves = [r for r in clean if r.get("retry_fired", 0) > 0]
    k, valid = len(strands), len(strands) + len(clean)

    out = []
    out.append("── boot-strand sampler summary (#843) " + "─" * 30)
    if warmup:
        out.append(
            f"warm-up boot: {warmup[0].get('outcome')} (excluded from the sample)"
        )
    out.append(
        f"sample: {len(sample)} boots — {len(clean)} clean, {k} strand, {len(other)} other"
    )
    if other:
        detail = ", ".join(f"boot {r.get('i')}: {r.get('outcome')}" for r in other)
        out.append(
            f"other outcomes (neither clean nor strand; excluded from the rate): {detail}"
        )
    if valid == 0:
        out.append("NO VALID BOOTS — this run measured nothing; see artifacts.")
        return ("\n".join(out), 1)

    lo, hi = cp_interval(k, valid)
    blo, bhi = cp_interval(BASELINE_STRANDS, BASELINE_N)
    p = fisher_one_sided(k, valid, BASELINE_STRANDS, BASELINE_N)
    submits = [r.get("t_submit_s") for r in clean if r.get("t_submit_s") is not None]

    out.append("")
    out.append(
        f"strand rate: {k}/{valid} = {k / valid:.3f}   95% CI [{lo:.3f}, {hi:.3f}] (Clopper-Pearson exact)"
    )
    if submits:
        out.append(
            f"time-to-submit on clean boots: min {min(submits)}s, median {sorted(submits)[len(submits) // 2]}s, max {max(submits)}s"
        )
    if retry_saves:
        out.append(
            f"clean via #837 send_retry: {len(retry_saves)} boot(s) — the retry visibly saved a would-be strand"
        )
    else:
        out.append("clean via #837 send_retry: 0 — no boot needed the retry")

    # Mechanism slice (#843 readiness-tracking hypothesis): pane_send_verified
    # cannot retry before the input box exists, so strands should concentrate
    # where injection landed on an undrawn TUI. glyph_at_inject conditions the
    # rate on that; t_glyph_s locates box-draw against the 3-9s production
    # injection window.
    glyph_known = [r for r in sample if r.get("glyph_at_inject") is not None]
    if glyph_known:
        out.append("")
        for label, grp in (
            (
                "box drawn at inject",
                [r for r in glyph_known if r.get("glyph_at_inject") == 1],
            ),
            (
                "box NOT drawn at inject",
                [r for r in glyph_known if r.get("glyph_at_inject") == 0],
            ),
        ):
            gv = [r for r in grp if r.get("outcome") in ("strand", "clean")]
            gs = [r for r in gv if r.get("outcome") == "strand"]
            if gv:
                out.append(f"strand rate | {label}: {len(gs)}/{len(gv)}")
    glyph_times = [r.get("t_glyph_s") for r in sample if r.get("t_glyph_s") is not None]
    if glyph_times:
        gt = sorted(glyph_times)
        out.append(
            f"input-box draw time (t_glyph): min {gt[0]}s, median {gt[len(gt) // 2]}s, max {gt[-1]}s"
            " (production injects at poller-READY = 3-9s)"
        )
    out.append("")
    out.append(
        f"pre-fix baseline (#843): {BASELINE_STRANDS}/{BASELINE_N} = {BASELINE_STRANDS / BASELINE_N:.2f}   95% CI [{blo:.3f}, {bhi:.3f}]"
    )
    out.append(f"fisher exact (one-sided, illustrative): p = {p:.4f}")
    out.append("")
    out.append("READ HONESTLY: the baseline is n=4, so the pre-fix rate is itself only")
    out.append(
        "known to within that near-unit-line interval — there is no crisp sample"
    )
    out.append("size at which the fix becomes proven. This sampler bounds the post-fix")
    out.append(
        "rate; it cannot sharpen the pre-fix one. The probe injects EARLIER than"
    )
    out.append("production (tokenless-canary readiness short-circuit vs the 3-9s")
    out.append("poller gate), so the sampled condition is at least as hard on the send")
    out.append("race; the Telegram poller's own network phase is not sampled.")
    out.append("")
    out.append(
        f"SAMPLER_RESULT strands={k} n={valid} ci95={lo:.3f},{hi:.3f} "
        f"other={len(other)} retry_saves={len(retry_saves)}"
    )
    return ("\n".join(out), 0)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: boot-strand-summary.py <rows.jsonl>", file=sys.stderr)
        return 1
    text, rc = summarize(load_rows(argv[1]))
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
