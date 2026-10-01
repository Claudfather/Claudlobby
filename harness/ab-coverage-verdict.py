#!/usr/bin/env python3
"""#866 coverage-honesty A/B — pre-registered analysis over the sampler rows.

Standalone stdlib module (dispatch-overdue.py / ab-comms-verdict.py precedent).
The endpoints, pairing, interval level, and decision rule are FROZEN by the
#866 pre-registration; this module implements them and nothing else:

- PRIMARY: median of per-(task,rep) paired relative length deltas
  (with-without)/without, pooled over the bounded arm; seeded bootstrap 95% CI.
- SECONDARY: identical treatment of verification-phrase density per 1k chars.
- MANIPULATION CHECK: coverage-disclosure rate per variant on the bounded arm.
- The control arm is computed identically and reported alongside; the decision
  rule reads bounded against control exactly as #866 states.

Interval machinery: the resample-medians algorithm, seed and resample count
are shared with ab-comms-verdict.py by import, but the LEVEL is computed here
at 95% — the pre-registration text says 95%, while the imported bootstrap_ci
hardcodes that module's CI_PCT=90. The first published readout carried 90%
intervals mislabeled "95%"; the #866 erratum records the correction, and this
module now computes what the registration text says.

Arm membership comes from each row's `arm` field (emitted by the harness,
beside the battery that defines it); rows from the first published run predate
the field, so a task-name fallback keeps the archived results re-analyzable.

INCONCLUSIVE is first-class: a primary CI spanning zero reports "does not
corroborate at this n" — never "no effect". A primary CI excluding zero in the
NEGATIVE direction is outside the pre-registered branch set and is reported as
exactly that, not shoehorned into a branch whose text would be false. Invalid
rows and zero-baseline pairs are excluded and disclosed (coverage honesty
applies to the analyzer too).

Exit: 0 verdict printed · 1 no analyzable pairs.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import statistics
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "ab_comms_verdict", Path(__file__).resolve().parent / "ab-comms-verdict.py"
)
_acv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_acv)
BOOTSTRAP_SEED = _acv.BOOTSTRAP_SEED
BOOTSTRAP_N = _acv.BOOTSTRAP_N

CI_PCT = 95  # the #866 registration text governs; see module docstring
_ARM_FALLBACK = {"T1": "bounded", "T2": "bounded", "T3": "control"}


def bootstrap_ci(xs: list[float]) -> tuple[float | None, float | None]:
    """Seeded bootstrap CI on the median at CI_PCT — same algorithm, seed and
    resample count as ab_comms_verdict.bootstrap_ci, level per #866."""
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


def load_rows(path: str) -> list[dict]:
    # Per-line decode guard: one bad row costs one row, not the sample.
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
                        f"ab-coverage-verdict: skipping undecodable row: {line[:80]}",
                        file=sys.stderr,
                    )
    except OSError as exc:
        print(f"ab-coverage-verdict: cannot read {path}: {exc}", file=sys.stderr)
    return rows


def _arm(row: dict) -> str | None:
    return row.get("arm") or _ARM_FALLBACK.get(row.get("task", ""))


def _length(row: dict) -> float:
    return row.get("len_chars", 0)


def _density(row: dict) -> float:
    length = row.get("len_chars") or 0
    if length <= 0:
        return 0.0
    return row.get("verif_matches", 0) / (length / 1000.0)


def paired_deltas(rows: list[dict], arm: str, value) -> tuple[list[float], int]:
    """Per-(task,rep) relative deltas (with-without)/without for valid pairs in
    <arm>. Returns (deltas, dropped): a zero-baseline pair has an undefined
    relative delta and is dropped — but COUNTED, so the readout discloses the
    shrunk n instead of silently narrowing it."""
    cells: dict[tuple[str, int, str], dict] = {}
    for r in rows:
        if r.get("valid") and _arm(r) == arm:
            cells[(r["task"], r["rep"], r["variant"])] = r
    deltas: list[float] = []
    dropped = 0
    for (task, rep, variant), wo in sorted(cells.items()):
        if variant != "without":
            continue
        w = cells.get((task, rep, "with"))
        if w is None:
            continue
        base = value(wo)
        if base == 0:
            dropped += 1
            continue
        deltas.append((value(w) - base) / base)
    return deltas, dropped


def _endpoint(rows: list[dict], arm: str, value) -> dict:
    deltas, dropped = paired_deltas(rows, arm, value)
    lo, hi = bootstrap_ci(deltas)
    return {
        "deltas": deltas,
        "dropped": dropped,
        "n": len(deltas),
        "median": statistics.median(deltas) if deltas else None,
        "lo": lo,
        "hi": hi,
    }


def _fmt(name: str, e: dict) -> str:
    note = f" ({e['dropped']} pair(s) dropped: zero baseline)" if e["dropped"] else ""
    if not e["deltas"]:
        return f"{name}: no analyzable pairs{note}"
    return (
        f"{name}: median {e['median']:+.3f}  {CI_PCT}% CI [{e['lo']:+.3f}, {e['hi']:+.3f}]  "
        f"n={e['n']} pairs{note}"
    )


def analyze(rows: list[dict]) -> tuple[str, int]:
    valid = [r for r in rows if r.get("valid")]
    invalid = [r for r in rows if not r.get("valid")]

    out = []
    out.append("── coverage-honesty A/B verdict (#866 pre-registered) " + "─" * 16)
    out.append(f"rows: {len(rows)} total, {len(valid)} valid, {len(invalid)} invalid")
    if invalid:
        detail = ", ".join(
            f"{r.get('task')}/{r.get('variant')}/rep{r.get('rep')}" for r in invalid
        )
        out.append(f"invalid rows excluded and disclosed: {detail}")

    ep = {
        "primary": _endpoint(valid, "bounded", _length),
        "primary_ctl": _endpoint(valid, "control", _length),
        "secondary": _endpoint(valid, "bounded", _density),
        "secondary_ctl": _endpoint(valid, "control", _density),
    }

    if not ep["primary"]["deltas"]:
        out.append(
            "NO ANALYZABLE BOUNDED-ARM PAIRS — nothing to conclude; see artifacts."
        )
        return ("\n".join(out), 1)

    out.append("")
    out.append(_fmt("PRIMARY   length rel-delta, bounded", ep["primary"]))
    out.append(_fmt("          length rel-delta, control", ep["primary_ctl"]))
    out.append(_fmt("SECONDARY density rel-delta, bounded", ep["secondary"]))
    out.append(_fmt("          density rel-delta, control", ep["secondary_ctl"]))

    disc = {"with": [], "without": []}
    for r in valid:
        if _arm(r) == "bounded":
            disc[r["variant"]].append(bool(r.get("disclosure")))
    out.append("")
    for variant in ("without", "with"):
        hits = disc[variant]
        if hits:
            out.append(
                f"MANIPULATION CHECK disclosure rate ({variant}): "
                f"{sum(hits)}/{len(hits)} = {sum(hits) / len(hits):.2f}"
            )

    # Decision rule, verbatim from #866. A negatively-signed exclusion is
    # outside the registered branch set and is named as such — never described
    # with the INCONCLUSIVE branch's "CI includes 0" text, which would be false.
    p_lo, p_hi = ep["primary"]["lo"], ep["primary"]["hi"]
    c_lo, c_hi = ep["primary_ctl"]["lo"], ep["primary_ctl"]["hi"]
    prim_spans_zero = p_lo <= 0 <= p_hi
    ctl_spans_zero = c_lo is None or (c_lo <= 0 <= c_hi)
    if not prim_spans_zero and p_lo > 0 and ctl_spans_zero:
        verdict = "CLAUSE-SPECIFIC EFFECT: bounded-arm length CI excludes 0 (positive); control CI does not."
    elif not prim_spans_zero and p_lo > 0:
        verdict = "GENERIC-VERBOSITY EFFECT: length CI positive on bounded AND control arms — not specific to coverage reporting."
    elif not prim_spans_zero and p_hi < 0:
        verdict = (
            "DIRECTIONAL-NEGATIVE (outside the pre-registered branch set): the "
            "bounded-arm length CI excludes 0 BELOW — the clause is associated "
            "with SHORTER output here. Flagged for interpretation, not mapped "
            "onto a registered branch."
        )
    else:
        verdict = (
            "INCONCLUSIVE: the primary CI includes 0 at this n — the A/B does not "
            "corroborate the observational claim; consistent with (not proof of) confounding."
        )
    out.append("")
    out.append(f"VERDICT: {verdict}")

    t3_med = ep["primary_ctl"]["median"]
    out.append("")
    out.append(
        f"COVERAGE_AB_RESULT primary_median={ep['primary']['median']:+.3f} "
        f"ci{CI_PCT}={p_lo:+.3f},{p_hi:+.3f} n_pairs={ep['primary']['n']} "
        f"t3_median={f'{t3_med:+.3f}' if t3_med is not None else 'none'} "
        f"disclose_without={sum(disc['without'])}/{len(disc['without'])} "
        f"disclose_with={sum(disc['with'])}/{len(disc['with'])} seed={BOOTSTRAP_SEED}"
    )
    return ("\n".join(out), 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--hash-without", default="?")
    ap.add_argument("--hash-with", default="?")
    ap.add_argument("--claude-version", default="?")
    args = ap.parse_args(argv)
    text, rc = analyze(load_rows(args.results))
    print(text)
    print(
        f"pins: guardrail_without={args.hash_without[:12]} guardrail_with={args.hash_with[:12]} "
        f"claude={args.claude_version} bootstrap_seed={BOOTSTRAP_SEED} ci_pct={CI_PCT}"
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
