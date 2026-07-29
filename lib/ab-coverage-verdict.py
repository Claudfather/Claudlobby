#!/usr/bin/env python3
"""#866 coverage-honesty A/B — pre-registered analysis over the sampler rows.

Standalone stdlib module (dispatch-overdue.py / ab-comms-verdict.py precedent).
The endpoints, pairing, interval machinery, and decision rule are FROZEN by the
#866 pre-registration; this module implements them and nothing else:

- PRIMARY: median of per-(task,rep) paired relative length deltas
  (with-without)/without, pooled over the bounded tasks T1+T2; seeded
  bootstrap 95% CI (bootstrap_ci imported from ab-comms-verdict.py so the seed
  and machinery are shared, pinned in the output).
- SECONDARY: identical treatment of verification-phrase density per 1k chars
  (matches counted upstream with the frozen regex; density derived here).
- MANIPULATION CHECK: coverage-disclosure rate per variant on T1+T2.
- T3 is the discriminant-validity control, computed identically, reported
  alongside; the decision rule reads T1+T2 against T3 exactly as #866 states.

INCONCLUSIVE is first-class: a primary CI spanning zero reports "does not
corroborate at this n" — never "no effect". Invalid rows are excluded and
disclosed with their cells (coverage honesty applies to the analyzer too).

Exit: 0 verdict printed · 1 no analyzable pairs.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "ab_comms_verdict", Path(__file__).resolve().parent / "ab-comms-verdict.py"
)
_acv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_acv)
bootstrap_ci = _acv.bootstrap_ci
BOOTSTRAP_SEED = _acv.BOOTSTRAP_SEED

BOUNDED = ("T1", "T2")
CONTROL = "T3"


def load_rows(path: str) -> list[dict]:
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


def _density(row: dict) -> float:
    length = row.get("len_chars") or 0
    if length <= 0:
        return 0.0
    return row.get("verif_matches", 0) / (length / 1000.0)


def paired_deltas(
    rows: list[dict], tasks: tuple[str, ...], value
) -> tuple[list[float], int]:
    """Per-(task,rep) relative deltas (with-without)/without for valid pairs.

    Returns (deltas, dropped): a zero-baseline pair has an undefined relative
    delta and is dropped — but COUNTED, so the readout can disclose the shrunk
    n instead of silently narrowing it.
    """
    cells: dict[tuple[str, int, str], dict] = {}
    for r in rows:
        if r.get("valid") and r.get("task") in tasks:
            cells[(r["task"], r["rep"], r["variant"])] = r
    deltas: list[float] = []
    dropped = 0
    for task, rep, variant in sorted(cells):
        if variant != "without":
            continue
        w = cells.get((task, rep, "with"))
        wo = cells[(task, rep, "without")]
        if w is None:
            continue
        base = value(wo)
        if base == 0:
            dropped += 1
            continue
        deltas.append((value(w) - base) / base)
    return deltas, dropped


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

    length = lambda r: r.get("len_chars", 0)  # noqa: E731
    prim, prim_drop = paired_deltas(valid, BOUNDED, length)
    prim_ctl, prim_ctl_drop = paired_deltas(valid, (CONTROL,), length)
    sec, sec_drop = paired_deltas(valid, BOUNDED, _density)
    sec_ctl, sec_ctl_drop = paired_deltas(valid, (CONTROL,), _density)

    if not prim:
        out.append(
            "NO ANALYZABLE BOUNDED-TASK PAIRS — nothing to conclude; see artifacts."
        )
        return ("\n".join(out), 1)

    def block(name, deltas, dropped):
        note = f" ({dropped} pair(s) dropped: zero baseline)" if dropped else ""
        if not deltas:
            return f"{name}: no analyzable pairs{note}"
        med = statistics.median(deltas)
        lo, hi = bootstrap_ci(deltas)
        return (
            f"{name}: median {med:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]  "
            f"n={len(deltas)} pairs{note}"
        )

    out.append("")
    out.append(block("PRIMARY   length rel-delta, T1+T2", prim, prim_drop))
    out.append(block("          length rel-delta, T3 ctl", prim_ctl, prim_ctl_drop))
    out.append(block("SECONDARY density rel-delta, T1+T2", sec, sec_drop))
    out.append(block("          density rel-delta, T3 ctl", sec_ctl, sec_ctl_drop))

    disc = {"with": [], "without": []}
    for r in valid:
        if r.get("task") in BOUNDED:
            disc[r["variant"]].append(bool(r.get("disclosure")))
    out.append("")
    for variant in ("without", "with"):
        hits = disc[variant]
        if hits:
            out.append(
                f"MANIPULATION CHECK disclosure rate ({variant}): "
                f"{sum(hits)}/{len(hits)} = {sum(hits) / len(hits):.2f}"
            )

    # Decision rule, verbatim from #866.
    p_lo, p_hi = bootstrap_ci(prim)
    c_lo, c_hi = bootstrap_ci(prim_ctl) if prim_ctl else (None, None)
    prim_positive = p_lo is not None and p_lo > 0
    ctl_spans_zero = c_lo is None or (c_lo <= 0 <= c_hi)
    if prim_positive and ctl_spans_zero:
        verdict = "CLAUSE-SPECIFIC EFFECT: bounded-task length CI excludes 0 (positive); control CI does not."
    elif prim_positive and not ctl_spans_zero and c_lo is not None and c_lo > 0:
        verdict = "GENERIC-VERBOSITY EFFECT: length CI positive on bounded AND control tasks — not specific to coverage reporting."
    else:
        verdict = (
            "INCONCLUSIVE: the primary CI includes 0 at this n — the A/B does not "
            "corroborate the observational claim; consistent with (not proof of) confounding."
        )
    out.append("")
    out.append(f"VERDICT: {verdict}")

    med = statistics.median(prim)
    out.append("")
    out.append(
        f"COVERAGE_AB_RESULT primary_median={med:+.3f} ci95={p_lo:+.3f},{p_hi:+.3f} "
        f"n_pairs={len(prim)} t3_median={statistics.median(prim_ctl) if prim_ctl else 0:+.3f} "
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
        f"claude={args.claude_version} bootstrap_seed={BOOTSTRAP_SEED}"
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
