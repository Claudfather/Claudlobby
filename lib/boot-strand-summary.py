#!/usr/bin/env python3
"""#843 boot-strand sampler summary — the statistics half of boot-strand-sampler.sh.

Standalone stdlib module (dispatch-overdue.py precedent) so the interval math
is unit-testable instead of buried in a heredoc. Reads the sampler's rows.jsonl
and prints the measurement WITH its uncertainty attached: an exact
Clopper-Pearson 95% interval on the strand rate, next to the pre-fix baseline
(2 strands in 4 tracked restarts, #843) shown with ITS interval — which spans
nearly the whole unit line, the visible form of "the null is poorly estimated
and there is no crisp threshold at which the fix becomes proven".

ARMS. This module GROUPS BY THE RECORDED ARM (`settle_s`, hoisted from each
row's `arm_knobs`) and reports the pre-registered per-arm figures rather than
one pooled rate. Pooling arms is not a lesser answer, it is a wrong one: it
averages the independent variable away and reports the result as a measurement.

PAIRING. Grouping by arm is not enough to compare arms. A ladder run one arm at
a time — whether by concatenating single-arm rows files or by any driver that
finishes an arm before starting the next — hands each arm a different hour of
ambient conditions, which on this host swings 9.7-17.7 unaided. So the module
also asks whether the arms actually ALTERNATED, and answers it from the
sequence of in-force arms over the boot index rather than from the `block` and
`pos` labels, which the interleaving loop writes about itself. Where the labels
contradict that sequence it refuses; where pairing is simply absent it prints
the per-arm rates and withholds the between-arm difference.

It REFUSES a sample it cannot attribute (exit 3), and the refusals are named
individually, because every way a sample loses its arm is silent by
construction — a mislabelled arm is undetectable from the artifact afterwards.
That is the whole reason the arm is recorded at the boot instead of asserted by
the caller, and a summarizer that quietly pooled the ambiguous cases would give
the recording back with one hand what it took with the other.

NO VERDICT IS EMITTED, deliberately. The pre-registration pins Clopper-Pearson
for the per-arm rate; it does not pin a method for the between-arm difference,
so the difference interval here is labelled with its method and marked
unratified. Deciding SUPPORTED/REFUTED from an unratified statistic would
pre-empt a ratification that has not happened (`ab-recoverability-scorer.py`
precedent). This module produces figures; a human applies the bar.

Exit: 0 summary printed · 1 no valid boots (a sample of others-only or an
empty/unreadable rows file must not read as a measurement) · 3 the sample
cannot be attributed to arms, or its block record contradicts the arms that
were in force — refused, never pooled.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from math import comb, sqrt

# The #843 pre-fix baseline: 2 strands in 4 deliberately tracked restarts
# (mason clean, astrid strand, alex strand, clog clean). n=4 — an order of
# magnitude, not an estimate.
BASELINE_STRANDS = 2
BASELINE_N = 4

# The pre-registered independent variable (pre-registration v2 §4 TARGET) and
# the row fields that carry it. IV_FIELD is hoisted by the sampler FROM
# IV_KNOB's arm record in one expression, so a row where the two disagree was
# edited after the fact — which is exactly the mislabel this module exists to
# catch, and the only way it can be caught at all.
# The independent variable, as a (knob, hoisted row field) PAIR. Selectable
# because the sampler now has two axes: the #843 settle ladder, and the #1236
# trace axis that holds settle fixed and moves only the instrumentation.
#
# Switched as a pair and never independently — the arm value is cross-checked
# against the knob record in row_arm, and a mismatched pair would make every
# row report arm-disagrees-with-record.
IV_CHOICES = {
    "settle": ("PANE_SEND_SETTLE_S", "settle_s"),
    "trace": ("PANE_VERIFY_TRACE", "trace_on"),
}
IV_KNOB = "PANE_SEND_SETTLE_S"
IV_FIELD = "settle_s"


def set_iv(axis: str) -> None:
    """Point the module at one axis. Call before any analysis."""
    global IV_KNOB, IV_FIELD
    try:
        IV_KNOB, IV_FIELD = IV_CHOICES[axis]
    except KeyError:
        raise SystemExit(f"unknown --iv {axis!r}; expected one of {sorted(IV_CHOICES)}")

# Pairing (pre-registration v2 §4 BASELINE): "interleaved blocks, randomized
# arm order within block. One block = one boot per arm."
BLOCK_FIELD = "block"
POS_FIELD = "pos"

# The pre-registered drift-exclusion band (v2 §4 BASELINE): discard any block
# whose loadavg_1m max/min across its arms exceeds this. Chosen there from the
# observed 9.7-17.7 ambient spread (ratio 1.82) — a block tighter than 1.6 is
# materially quieter than ambient's own swing. Pre-registered so it cannot be
# tuned after seeing data; it is a stated assumption, not a measurement.
DRIFT_RATIO_MAX = 1.6

REFUSE_RC = 3


def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), exact via math.comb."""
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    return sum(comb(n, i) * p**i * (1.0 - p) ** (n - i) for i in range(0, k + 1))


def _bisect(f, lo: float, hi: float, iters: int = 60) -> float:
    # 60 iterations reaches the float64 fixed point on [0,1] (measured 53-62
    # for the k/n shapes this module sees); more is pure no-op.
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


# Method label, carried into the output next to every number it produced. A
# difference interval whose method is knowable only by reading the source is
# the same defect as an arm knowable only from a filename.
MOVER_METHOD = "MOVER (Zou-Donner) over the two exact Clopper-Pearson intervals"


def mover_difference(
    k1: int, n1: int, k2: int, n2: int, conf: float = 0.95
) -> tuple[float, float, float]:
    """(point, lo, hi) for p1 - p2, combining the two per-arm CP intervals.

    MOVER builds a difference interval from the marginal intervals rather than
    from a normal approximation to the difference, so it inherits the exactness
    of its inputs and stays defined at k = 0 and k = n — where a Wald interval
    collapses to zero width and would report a spurious clean separation on
    exactly the arms a ceiling arm is meant to produce.

    UNRATIFIED. Pre-registration v2 §5 pins Clopper-Pearson for the per-arm
    rate and names no method for the difference. This is a defensible choice,
    not an authorised one; the caller is told so wherever the number appears.
    """
    p1, p2 = k1 / n1, k2 / n2
    lo1, hi1 = cp_interval(k1, n1, conf)
    lo2, hi2 = cp_interval(k2, n2, conf)
    d = p1 - p2
    return (
        d,
        d - sqrt((p1 - lo1) ** 2 + (hi2 - p2) ** 2),
        d + sqrt((hi1 - p1) ** 2 + (p2 - lo2) ** 2),
    )


def load_rows(path: str) -> list[dict]:
    # Per-line decode guard (dispatch-overdue._load_jsonl shape): a run killed
    # mid-append leaves a truncated last line, and one bad row must cost one
    # row, not the whole sample.
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    print(
                        f"boot-strand-summary: skipping undecodable row: {line[:80]}",
                        file=sys.stderr,
                    )
    except OSError as exc:
        print(f"boot-strand-summary: cannot read {path}: {exc}", file=sys.stderr)
    return rows


# ── arm attribution ───────────────────────────────────────────────────────────


def _is_number(v) -> bool:
    # bool is an int subclass in Python; a JSON true must not pass as an arm.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fmt_arm(v: float) -> str:
    return f"{v:g}"


def row_arm(row: dict) -> tuple[float | None, str | None]:
    """(arm value, refusal slug). Both None means an UNLABELLED legacy row.

    Every branch that cannot produce a trustworthy arm returns a slug rather
    than a best guess. There is no defensible default: assigning an unlabelled
    row to any arm is the mislabel, and the artifact carries no second copy of
    the fact to recover it from.
    """
    knobs = row.get("arm_knobs")
    iv = row.get(IV_FIELD)
    if knobs is None and iv is None:
        return (None, None)
    if not isinstance(knobs, dict):
        return (None, "missing-arm-record")
    rec = knobs.get(IV_KNOB)
    if not isinstance(rec, dict):
        return (None, "missing-arm-record")
    v = rec.get("v")
    if not _is_number(v) or not _is_number(iv):
        return (None, "non-numeric-arm")
    if float(v) != float(iv):
        return (None, "arm-disagrees-with-record")
    return (float(v), None)


def _covarying_knobs(rows: list[dict]) -> list[str]:
    """Forwarded knobs OTHER than the IV whose in-force value is not constant.

    Rows agreeing on the IV are not therefore one condition: two runs at
    settle=1.5 with different verify budgets concatenate into a group that
    reads as a single arm and is not one. The IV is what the ladder varied on
    purpose; this catches what it varied by accident.
    """
    seen: dict[str, set] = {}
    for r in rows:
        for knob, rec in (r.get("arm_knobs") or {}).items():
            if knob == IV_KNOB or not isinstance(rec, dict):
                continue
            seen.setdefault(knob, set()).add(json.dumps(rec.get("v"), sort_keys=True))
    return sorted(k for k, vals in seen.items() if len(vals) > 1)


def attribute_arms(
    sample: list[dict],
) -> tuple[dict[float | None, list[dict]], list[str]]:
    """(arm -> rows, named refusals). The None key is the unlabelled arm."""
    refusals: list[str] = []
    arms: dict[float | None, list[dict]] = {}
    for r in sample:
        arm, slug = row_arm(r)
        if slug:
            refusals.append(
                f"MISLABELLED-ARM/{slug}: boot {r.get('i')} "
                f"({IV_FIELD}={r.get(IV_FIELD)!r}, "
                f"arm_knobs={json.dumps(r.get('arm_knobs'))[:120]})"
            )
            continue
        arms.setdefault(arm, []).append(r)

    labelled = [a for a in arms if a is not None]
    if labelled and None in arms:
        refusals.append(
            "MISLABELLED-ARM/mixed-labelled-and-unlabelled: "
            f"{sum(len(arms[a]) for a in labelled)} row(s) carry an arm and "
            f"{len(arms[None])} do not. A file mixing recorded and unrecorded "
            "arms is precisely where a mislabel hides — the unlabelled rows "
            "cannot be assigned and must not be pooled into one that can."
        )
    for arm in labelled:
        drifting = _covarying_knobs(arms[arm])
        if drifting:
            refusals.append(
                f"MISLABELLED-ARM/covarying-knob: arm {IV_FIELD}={_fmt_arm(arm)} "
                f"holds rows that disagree on {', '.join(drifting)} — "
                "these boots did not run under one condition."
            )
    return (arms, refusals)


# ── pairing: was the ladder actually interleaved? ─────────────────────────────
#
# `block` and `pos` are written by the same sampler loop that does the
# interleaving, so alone they are a CLAIM: had the interleave broken, the
# labels would read exactly as they do now. Everything here is therefore
# derived from the sequence of IN-FORCE arms over `i` — `settle_s`, resolved
# per boot from the live environment — and the declared block record is checked
# AGAINST that, never believed. A check whose only evidence is the claim it is
# checking cannot fail, which is the defect class this whole ladder exists to
# measure. Same shape as _covarying_knobs: guard the CONDITION, not the label.
#
# The two failure modes carry deliberately different force:
#
#   CONTRADICTION — the declared blocks disagree with the in-force arms.
#       REFUSAL, exit 3. A lying artifact, the same class as
#       arm-disagrees-with-record, and it joins that taxonomy.
#
#   ABSENCE — no block record, or arms contiguous by arm.
#       The per-arm rates stand and the DIFFERENCE is suppressed. The confound
#       is in the between-arm comparison, not in the attribution: an
#       arm-sequential boot is perfectly attributable, so refusing the sample
#       would discard sound per-arm rates to punish an analysis nobody can run
#       from it anyway. This is not the soft option — §4's VERDICT table needs
#       a difference CI to reach SUPPORTED or REFUTED, so withholding it leaves
#       INCONCLUSIVE as the only reachable verdict, by construction.


def _arms_of(rows: list[dict]) -> list[float]:
    """In-force arm of every row that has one, in the order given.

    One reading of the arm per row: the `[row_arm(r)[0] for r in rows if
    row_arm(r)[1] is None]` shape resolves each row twice and states the
    valid-arm rule at every call site, which is how two of them come to
    disagree about what counts as attributable.
    """
    out = []
    for r in rows:
        arm, slug = row_arm(r)
        if slug is None and arm is not None:
            out.append(arm)
    return out


def _arm_sequence(sample: list[dict]) -> list[float]:
    """In-force arms ordered by boot index — the record independent of labels."""
    return _arms_of(sorted(sample, key=lambda r: r.get("i") or 0))


def _arm_runs(seq: list[float]) -> int:
    """Maximal contiguous stretches of one arm.

    B interleaved blocks over A arms give close to B*A of these; running each
    arm to completion gives exactly A, whatever the block labels say. That gap
    is the discriminator, and it is computed from the data alone.
    """
    runs = 0
    prev = None
    for a in seq:
        if runs == 0 or a != prev:
            runs += 1
        prev = a
    return runs


def _declared_blocks(sample: list[dict]) -> tuple[dict[int, list[dict]], list[dict]]:
    """(block id -> rows, rows carrying no block record)."""
    blocks: dict[int, list[dict]] = {}
    unblocked: list[dict] = []
    for r in sample:
        b = r.get(BLOCK_FIELD)
        if _is_number(b):
            blocks.setdefault(int(b), []).append(r)
        else:
            unblocked.append(r)
    return (blocks, unblocked)


def block_refusals(blocks: dict[int, list[dict]]) -> list[str]:
    """Ways a declared block record can CONTRADICT the boots it covers."""
    refusals: list[str] = []
    for bid in sorted(blocks):
        rows = blocks[bid]
        arms = _arms_of(rows)
        dup = sorted({a for a in arms if arms.count(a) > 1})
        if dup:
            refusals.append(
                f"UNPAIRED-SAMPLE/block-holds-duplicate-arm: block {bid} runs "
                f"{', '.join(_fmt_arm(a) for a in dup)} more than once. One block is "
                "one boot per arm, so this block yields no within-block pair."
            )
        poss = sorted(p for p in (r.get(POS_FIELD) for r in rows) if _is_number(p))
        if poss != list(range(len(rows))):
            refusals.append(
                f"UNPAIRED-SAMPLE/pos-not-a-permutation: block {bid} declares positions "
                f"{poss} for {len(rows)} boots — within-block position must be "
                "0..k-1 exactly once, so this block record does not describe its boots."
            )
        idx = sorted(r.get("i") for r in rows if _is_number(r.get("i")))
        if idx and idx[-1] - idx[0] != len(idx) - 1:
            refusals.append(
                f"UNPAIRED-SAMPLE/block-not-contiguous: block {bid} spans boots "
                f"{idx[0]}-{idx[-1]} but holds {len(idx)} of them. A block interleaved "
                "with another block's boots did not run as one pairing unit."
            )
    return refusals


def verify_pairing(
    sample: list[dict], arms: dict
) -> tuple[str, list[str], list[str], dict[int, list[dict]]]:
    """(state, refusals, report lines, blocks usable for a paired analysis).

    state: single-arm | paired | sequential | unblocked | contradicted.
    """
    labelled = sorted(a for a in arms if a is not None)
    if len(labelled) < 2:
        return ("single-arm", [], [], {})

    seq = _arm_sequence(sample)
    runs = _arm_runs(seq)
    n_arms = len(labelled)
    blocks, unblocked = _declared_blocks(sample)
    refusals = block_refusals(blocks)
    # The second conjunct is load-bearing and must not be simplified away.
    # With exactly one boot per arm, `runs == n_arms` is true for EVERY
    # possible order — the count cannot distinguish contiguous from
    # alternating, because there is nothing to alternate. Without the length
    # guard the smallest legitimate pairing (one block, one boot per arm) is
    # read as arm-sequential and refused its own between-arm figure, and the
    # failure is conservative enough that nothing else in the suite notices
    # (gated by test_minimal_paired_sample_of_one_block_is_not_read_as_sequential).
    contiguous_by_arm = runs == n_arms and len(seq) > n_arms

    lines = ["── pairing (pre-registration v2 §4 BASELINE) " + "─" * 24]
    lines.append(
        f"arm-runs observed over {len(seq)} attributable boots: {runs} "
        f"(arm-sequential would be {n_arms}; one boot per arm per block gives "
        f"up to {len(seq)})"
    )
    lines.append(
        "Derived from the in-force settle_s sequence, NOT from the block labels: a"
    )
    lines.append(
        "broken interleave would still be labelled one, so the labels are checked"
    )
    lines.append("against this rather than trusted.")

    if refusals:
        return ("contradicted", refusals, lines, {})

    if unblocked:
        lines.append("")
        lines.append(
            f"NOT PAIRED: {len(unblocked)} of {len(sample)} boots carry no block record."
        )
        if contiguous_by_arm:
            lines.append(
                "The in-force arms are contiguous by arm — this is the arm-sequential"
            )
            lines.append(
                "shape §4 BASELINE forbids by name (one invocation per settle value,"
            )
            lines.append(
                "rows files concatenated). Re-run as one invocation with --arms."
            )
        return ("unblocked", [], lines, {})

    if contiguous_by_arm:
        lines.append("")
        lines.append(
            "NOT PAIRED: blocks are declared, but the in-force arms are contiguous by"
        )
        lines.append("arm. Whatever the labels say, these boots ran arm-sequential.")
        return ("sequential", [], lines, {})

    complete: dict[int, list[dict]] = {}
    partial = 0
    for bid, rows in blocks.items():
        if set(_arms_of(rows)) == set(labelled):
            complete[bid] = rows
        else:
            partial += 1
    if partial:
        lines.append("")
        lines.append(
            f"{partial} block(s) hold fewer than one boot per arm (a run cut mid-block)."
        )
        lines.append(
            "They are excluded from the paired difference and still counted per arm."
        )
    if not complete:
        lines.append("")
        lines.append("NOT PAIRED: no block holds one boot per arm.")
        return ("unblocked", [], lines, {})

    lines.append("")
    lines.append(
        f"PAIRED: {len(complete)} complete block(s) x {n_arms} arms, arm order varying"
    )
    lines.append("within blocks.")
    return ("paired", [], lines, complete)


def drift_band(
    blocks: dict[int, list[dict]],
) -> tuple[dict[int, list[dict]], list[int], list[int]]:
    """(kept, discarded, undeterminable) per the v2 §4 drift-exclusion band.

    A block whose loadavg cannot be read is reported separately rather than
    silently kept or silently dropped: it cannot be SHOWN to sit inside the
    band, and a missing covariate must not read as a passing one.
    """
    kept: dict[int, list[dict]] = {}
    discarded: list[int] = []
    unknown: list[int] = []
    for bid, rows in sorted(blocks.items()):
        las = [r.get("loadavg_1m") for r in rows]
        if not las or not all(_is_number(la) and la > 0 for la in las):
            unknown.append(bid)
        elif max(las) / min(las) > DRIFT_RATIO_MAX:
            discarded.append(bid)
        else:
            kept[bid] = rows
    return (kept, discarded, unknown)


# ── per-arm reporting ─────────────────────────────────────────────────────────

# A boot counts toward a rate only when it reached a verdict. Everything else
# is `other` — counted and named, never folded into clean (sampler header).
VALID_OUTCOMES = ("strand", "clean")


def _rate(rows: list[dict]) -> tuple[int, int]:
    """(strands, boots that reached a verdict) over any slice of rows."""
    valid = [r for r in rows if r.get("outcome") in VALID_OUTCOMES]
    return (len([r for r in valid if r.get("outcome") == "strand"]), len(valid))


def _stratified(rows: list[dict], label: str) -> list[str]:
    """Strand rate split by retry_fired, per the pre-registration. Never pooled
    into a single line: a change that shifts strands between the two strata
    while holding the total is invisible to the pooled rate."""
    out = []
    for name, grp in (
        ("retry_fired > 0", [r for r in rows if (r.get("retry_fired") or 0) > 0]),
        ("retry_fired == 0", [r for r in rows if (r.get("retry_fired") or 0) == 0]),
    ):
        ks, n = _rate(grp)
        out.append(
            f"strand rate | {label}, {name}: "
            + (f"{ks}/{n}" if n else "no boots in this stratum")
        )
    return out


def arm_block(rows: list[dict], arm: float | None) -> tuple[list[str], str, int, int]:
    """(report lines, machine line, strands, valid) for one arm.

    The machine line is built HERE, from the values this function already
    computed, rather than re-derived by the caller. Two independent
    computations of one number is the defect this whole change closes one
    layer down — and it had already started: the retry-save filter was spelled
    `r.get("retry_fired", 0) > 0` in one place and `(r.get("retry_fired") or 0)`
    in the other, which disagree on a null field.
    """
    label = "UNLABELLED" if arm is None else f"{IV_FIELD}={_fmt_arm(arm)}"
    strands = [r for r in rows if r.get("outcome") == "strand"]
    clean = [r for r in rows if r.get("outcome") == "clean"]
    other = [r for r in rows if r.get("outcome") not in ("strand", "clean")]
    retry_saves = [r for r in clean if r.get("retry_fired", 0) > 0]
    k, valid = len(strands), len(strands) + len(clean)

    out = [f"── arm {label} " + "─" * max(4, 56 - len(label))]
    out.append(
        f"sample: {len(rows)} boots — {len(clean)} clean, {k} strand, {len(other)} other"
    )
    if other:
        detail = ", ".join(f"boot {r.get('i')}: {r.get('outcome')}" for r in other)
        out.append(
            f"other outcomes (neither clean nor strand; excluded from the rate): {detail}"
        )
    if valid == 0:
        out.append("NO VALID BOOTS in this arm — it measured nothing; see artifacts.")
        return (out, "", k, valid)

    lo, hi = cp_interval(k, valid)
    submits = [r.get("t_submit_s") for r in clean if r.get("t_submit_s") is not None]
    out.append(
        f"strand rate: {k}/{valid} = {k / valid:.3f}   95% CI [{lo:.3f}, {hi:.3f}] (Clopper-Pearson exact)"
    )
    out += _stratified(rows, label)
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
    glyph_known = [r for r in rows if r.get("glyph_at_inject") is not None]
    if glyph_known:
        out.append("")
        for gl, grp in (
            (
                "box drawn at inject",
                [r for r in glyph_known if r.get("glyph_at_inject") == 1],
            ),
            (
                "box NOT drawn at inject",
                [r for r in glyph_known if r.get("glyph_at_inject") == 0],
            ),
        ):
            gs, gn = _rate(grp)
            if gn:
                out.append(f"strand rate | {gl}: {gs}/{gn}")
    glyph_times = [r.get("t_glyph_s") for r in rows if r.get("t_glyph_s") is not None]
    if glyph_times:
        gt = sorted(glyph_times)
        out.append(
            f"input-box draw time (t_glyph): min {gt[0]}s, median {gt[len(gt) // 2]}s, max {gt[-1]}s"
            " (production injects at poller-READY = 3-9s)"
        )
    # loadavg is REPORTED, never adjusted for (pre-registration v2 §4 BASELINE)
    # — post-hoc covariate adjustment is not in that pre-registration, and
    # adding it here would be a new design smuggled in as a summary.
    las = sorted(r["loadavg_1m"] for r in rows if r.get("loadavg_1m") is not None)
    if las:
        out.append(
            f"loadavg_1m at send: min {las[0]}, median {las[len(las) // 2]}, max {las[-1]}"
            " (reported, never adjusted for)"
        )
    # The parity ledger, surfaced (header §3: parity is evidenced, not
    # asserted): one histogram line per distinct per-boot process tree.
    par = Counter(
        (r.get("parity_procs") or "").strip()
        for r in rows
        if (r.get("parity_procs") or "").strip()
    )
    for tree, count in par.most_common():
        out.append(f"per-boot process tree x{count}: {tree}")
    machine = (
        f"SAMPLER_RESULT strands={k} n={valid} ci95={lo:.3f},{hi:.3f} "
        f"other={len(other)} retry_saves={len(retry_saves)} "
        f"{IV_FIELD}={'unlabelled' if arm is None else _fmt_arm(arm)}"
    )
    return (out, machine, k, valid)


def comparison_block(
    figures: list[tuple[float, int, int]], control: float | None
) -> list[str]:
    """Between-arm differences against the control arm. Figures, not a verdict.

    Everything below the early return is reachable only from a PAIRED sample:
    it needs two arms, and summarize withholds the comparison from any sample
    whose arms did not alternate. So the no-verdict note can state that the
    drift band was applied without a flag to say so — there is no path here
    that reaches it with an unfiltered set.
    """
    usable = [(a, k, n) for a, k, n in figures if n > 0]
    if len(usable) < 2:
        return []
    ctl = control if control is not None else min(a for a, _, _ in usable)
    match = [f for f in usable if f[0] == ctl]
    if not match:
        return [
            "",
            f"BETWEEN-ARM: control {_fmt_arm(ctl)} is not present in this sample "
            f"(arms: {', '.join(_fmt_arm(a) for a, _, _ in usable)}) — no comparison computed.",
        ]
    _, ck, cn = match[0]

    out = ["", "── between-arm (control - treatment) " + "─" * 31]
    out.append(
        f"control arm: {IV_FIELD}={_fmt_arm(ctl)}"
        + ("" if control is not None else " (smallest arm; override with --control)")
    )
    out.append(f"method: {MOVER_METHOD}")
    out.append(
        "UNRATIFIED: pre-registration v2 §5 pins Clopper-Pearson for the per-arm"
    )
    out.append(
        "rate and names no difference method. These intervals are reported so the"
    )
    out.append("bar can be applied by a human; this module emits no verdict.")
    for arm, k, n in sorted(usable):
        if arm == ctl:
            continue
        d, lo, hi = mover_difference(ck, cn, k, n)
        out.append(
            f"{_fmt_arm(ctl)} - {_fmt_arm(arm)}: {ck}/{cn} - {k}/{n} = {d:+.3f}   "
            f"95% CI [{lo:+.3f}, {hi:+.3f}]"
        )
    out.append("")
    out.append(
        "NO VERDICT EMITTED. A positive interval lying entirely above 0 is what the"
    )
    out.append(
        "pre-registration calls REPAINT SUPPORTED, but that reading also requires the"
    )
    out.append(
        "ceiling-arm delivery check, which is not computed here (see NOT MEASURED"
    )
    out.append("below). The drift-exclusion band IS applied — stated above.")
    return out


# ── orchestration ─────────────────────────────────────────────────────────────


def summarize(rows: list[dict], control: float | None = None) -> tuple[str, int]:
    """(report text, exit code) for a sampler run's rows."""
    sample = [r for r in rows if r.get("kind") == "sample"]
    warmup = [r for r in rows if r.get("kind") == "warmup"]
    arms, refusals = attribute_arms(sample)
    pair_state, pair_refusals, pair_lines, pair_blocks = verify_pairing(sample, arms)

    out = ["── boot-strand sampler summary (#843) " + "─" * 30]
    if warmup:
        out.append(
            f"warm-up boot: {warmup[0].get('outcome')} (excluded from the sample)"
        )

    if refusals:
        out.append("")
        out.append(
            f"REFUSED: {len(refusals)} arm-attribution problem(s). This sample is NOT"
        )
        out.append(
            "pooled and no rate is printed — an unattributable boot is not a boot at"
        )
        out.append("the default, and a mislabelled arm cannot be recovered later.")
        out += [f"  {r}" for r in refusals]
        return ("\n".join(out), REFUSE_RC)

    # Kept separate from the attribution refusals above, because they are not
    # the same failure: those say a boot cannot be assigned to an arm, these
    # say the run claims a pairing structure its own in-force arms deny.
    if pair_refusals:
        out.append("")
        out += pair_lines
        out.append("")
        out.append(
            f"REFUSED: {len(pair_refusals)} pairing problem(s). The block record"
        )
        out.append(
            "contradicts the arms actually in force, so it describes a run that did not"
        )
        out.append(
            "happen. No between-arm figure is computed from a pairing that is not real."
        )
        out += [f"  {r}" for r in pair_refusals]
        return ("\n".join(out), REFUSE_RC)

    unlabelled = None in arms
    if unlabelled:
        out.append("")
        out.append(
            "ARM: UNLABELLED — these rows predate per-boot arm recording, so this run"
        )
        out.append(
            "cannot evidence the settle window it ran under. Single-arm reporting only;"
        )
        out.append("no between-arm statistic is reachable from it.")
    ordered = (
        [(None, arms[None])]
        if unlabelled
        else sorted(arms.items(), key=lambda kv: kv[0])
    )

    figures: list[tuple[float, int, int]] = []
    machine: list[str] = []
    for arm, arm_rows in ordered:
        block, machine_line, k, valid = arm_block(arm_rows, arm)
        out.append("")
        out += block
        if arm is not None:
            figures.append((arm, k, valid))
        if machine_line:
            machine.append(machine_line)

    # Covers BOTH empty cases: no arms at all, and arms that all measured
    # nothing. An arms-exist-but-none-valid run is the one a `not arms` guard
    # above would miss, so there is exactly one guard rather than two.
    if not machine:
        out.append("")
        out.append("NO VALID BOOTS — this run measured nothing; see artifacts.")
        return ("\n".join(out), 1)

    if pair_lines:
        out.append("")
        out += pair_lines

    if pair_state == "paired":
        kept, discarded, unknown = drift_band(pair_blocks)
        total_blocks = len(pair_blocks)
        out.append("")
        out.append(
            f"drift-exclusion band (max/min loadavg_1m within a block > "
            f"{DRIFT_RATIO_MAX}): {len(discarded)} of {total_blocks} block(s) discarded"
        )
        if discarded:
            out.append(f"  discarded blocks: {discarded}")
        if unknown:
            out.append(
                f"  {len(unknown)} block(s) carry no usable loadavg_1m and CANNOT be "
                "shown to sit inside"
            )
            out.append(
                f"  the band — excluded from the difference, not assumed to pass: {unknown}"
            )
        # §4 VERDICT names this an INCONCLUSIVE condition. Stated, not ruled on:
        # this module emits no verdict (see comparison_block).
        if total_blocks and (len(discarded) + len(unknown)) / total_blocks > 0.25:
            out.append(
                "  OVER 25% OF BLOCKS EXCLUDED — §4 VERDICT makes that an INCONCLUSIVE"
            )
            out.append("  condition on its own, whatever the interval below shows.")

        # Grouped in one pass rather than re-scanning every surviving row per
        # arm, which resolved each row once per arm on the ladder.
        paired_rows = [r for rs in kept.values() for r in rs]
        by_arm: dict[float, list[dict]] = {}
        for r in paired_rows:
            arm, slug = row_arm(r)
            if slug is None and arm is not None:
                by_arm.setdefault(arm, []).append(r)
        diff_figures = [
            (arm, *_rate(by_arm.get(arm, [])))
            for arm in sorted(a for a in arms if a is not None)
        ]
        if paired_rows:
            out.append("")
            out.append(
                f"between-arm figures below are computed over the {len(kept)} block(s)"
            )
            out.append(
                "surviving the band, NOT over every boot — the per-arm rates above are"
            )
            out.append("the full sample and the two will differ.")
            out += comparison_block(diff_figures, control)
        else:
            out.append("")
            out.append(
                "BETWEEN-ARM DIFFERENCE SUPPRESSED — no block survived the drift band,"
            )
            out.append("so every remaining pair straddles an ambient excursion.")

        # §4 BASELINE says "compare within block; aggregate block deltas"; §5's
        # amendment pins MOVER over the two MARGINAL Clopper-Pearson intervals.
        # Those are different estimators, and this module implements the pinned
        # one. Flagged rather than resolved: choosing between them is a
        # pre-registration decision, not an implementation detail, and picking
        # one silently here would be the author picking the bar.
        out.append("")
        out.append(
            "ESTIMATOR GAP, for the pre-registration owner: §4 BASELINE specifies"
        )
        out.append(
            "within-block deltas aggregated across blocks; §5 pins MOVER over the two"
        )
        out.append(
            "marginal intervals. The pinned statistic is what is computed here. Blocking"
        )
        out.append("only pairs the arms in TIME — it does not yet enter the interval.")
    elif pair_state == "single-arm":
        # Nothing to pair and nothing to suppress: comparison_block already
        # says what a one-arm sample cannot answer. Printing a suppression
        # notice here would report a confound that does not apply.
        out += comparison_block(figures, control)
    else:
        out.append("")
        out.append(
            "BETWEEN-ARM DIFFERENCE SUPPRESSED — this sample is not paired, so a"
        )
        out.append(
            "difference between arms is not attributable to the settle window. Ambient"
        )
        out.append(
            "load swings 9.7-17.7 unaided on this host, four times the probe's own"
        )
        out.append(
            "footprint, so an unpaired difference is a reading of when each arm ran."
        )
        out.append(
            "Per-arm rates above stand. §4's VERDICT needs this interval, so the only"
        )
        out.append("verdict reachable from this artifact is INCONCLUSIVE.")

    # Between-arm confound: the pre-registration holds the synthetic-load
    # offset constant across arms precisely so it cannot confound the
    # comparison. If it moved, the comparison is between two things.
    burners = {
        a: sorted({r.get("load_burners") for r in rs})
        for a, rs in arms.items()
        if a is not None
    }
    if len({tuple(v) for v in burners.values()}) > 1:
        out.append("")
        out.append(
            "BETWEEN-ARM CONFOUND: load_burners is not constant across arms "
            f"({ {_fmt_arm(a): v for a, v in burners.items()} }). The pre-registered "
            "design holds it constant; a difference here is not attributable to "
            f"{IV_FIELD} alone."
        )

    lo, hi = cp_interval(BASELINE_STRANDS, BASELINE_N)
    out.append("")
    out.append(
        f"pre-fix baseline (#843): {BASELINE_STRANDS}/{BASELINE_N} = {BASELINE_STRANDS / BASELINE_N:.2f}   95% CI [{lo:.3f}, {hi:.3f}]"
    )
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
    out.append("NOT MEASURED HERE, and required before any verdict is claimed:")
    out.append("  · the ceiling-arm delivery check (§4 REPS abort rule) — whether the")
    out.append("    knob arrived at all is a harness question, not a rate.")
    out.append("  · the difference-method ratification (§5), and the §4/§5 estimator")
    out.append("    gap flagged above.")
    if pair_state != "paired":
        out.append("  · the drift-exclusion band (§4 BASELINE) — it needs interleaved")
        out.append("    blocks, which this sample does not carry. Run with --arms.")
    out.append("")
    out += machine
    return ("\n".join(out), 0)


def main(argv: list[str]) -> int:
    # Hand-rolled rather than argparse, and NOT an oversight to tidy up: the
    # sampler now propagates this module's exit code verbatim, and argparse
    # exits 2 on a usage error — which the sampler documents as
    # "precondition/dep missing (skip)". A typo in a flag would report as a
    # skipped run. If this ever moves to argparse, catch SystemExit and remap.
    args = argv[1:]
    # Same manual style as --control, and for the same reason recorded above:
    # argparse exits 2, which the sampler documents as "skip".
    if "--iv" in args:
        idx = args.index("--iv")
        if idx + 1 >= len(args):
            print("--iv needs a value", file=sys.stderr)
            return 1
        axis = args[idx + 1]
        if axis not in IV_CHOICES:
            print(f"bad --iv: {axis!r} (expected {sorted(IV_CHOICES)})", file=sys.stderr)
            return 1
        set_iv(axis)
        del args[idx : idx + 2]
    control: float | None = None
    if "--control" in args:
        idx = args.index("--control")
        if idx + 1 >= len(args):
            print("--control needs a value", file=sys.stderr)
            return 1
        try:
            control = float(args[idx + 1])
        except ValueError:
            print(f"bad --control: {args[idx + 1]!r}", file=sys.stderr)
            return 1
        del args[idx : idx + 2]
    if len(args) != 1:
        print(
            "usage: boot-strand-summary.py [--iv settle|trace] "
            "[--control VALUE] <rows.jsonl>",
            file=sys.stderr,
        )
        return 1
    text, rc = summarize(load_rows(args[0]), control)
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
