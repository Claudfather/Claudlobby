#!/usr/bin/env python3
"""#728 P1 channel-brevity A/B — pre-registered analysis over the harness rows.

Standalone stdlib module (ab-coverage-verdict.py sibling; the endpoints,
pairing, interval level and decision rule are FROZEN by the #729
channel-brevity pre-registration comment; this module implements them and
nothing else):

- PRIMARY: median of per-(task,rep) paired relative length deltas
  (with-without)/without, pooled over the CHANNEL arm (S1, S2); seeded
  bootstrap 95% CI. The ship-supporting direction is NEGATIVE (shorter).
- RULE-ZERO GATE (co-primary): required-fact misses per variant on the channel
  arm; WITH misses must not exceed WITHOUT misses. A reply that got shorter by
  dropping a required fact is lossy compression — the component failing its
  own rule zero.
- CONTROL: identical length treatment of the never-compress arm (S3, verbatim
  error lines — fixed-size by contract). A WITH reduction whose CI excludes 0
  below means the component compresses what it promises never to compress.
- Fact fidelity on the control arm is reported for the same reason.

Interval machinery: resample-medians algorithm, seed and resample count are
shared with ab-comms-verdict.py by import; the LEVEL is computed here at 95%
per the registration text (the #866 erratum precedent: the label and the
machinery must agree, so CI_PCT is explicit and pinned in the output).

INCONCLUSIVE is first-class: a primary CI spanning zero reports "does not
demonstrate an effect at this n" — never "no effect". A primary CI excluding
zero ABOVE (longer output) is outside the registered branch set and is named
exactly that. Invalid rows and zero-baseline pairs are excluded and disclosed.

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

CI_PCT = 95  # the #729 registration text governs; see module docstring


def bootstrap_ci(xs: list[float]) -> tuple[float | None, float | None]:
    """Seeded bootstrap CI on the median at CI_PCT — same algorithm, seed and
    resample count as ab_comms_verdict.bootstrap_ci, level per the registration."""
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
                        f"ab-channel-brevity-verdict: skipping undecodable row: {line[:80]}",
                        file=sys.stderr,
                    )
    except OSError as exc:
        print(f"ab-channel-brevity-verdict: cannot read {path}: {exc}", file=sys.stderr)
    return rows


def _length(row: dict) -> float:
    return row.get("len_chars", 0)


def paired_deltas(rows: list[dict], arm: str) -> tuple[list[float], int]:
    """Per-(task,rep) relative length deltas (with-without)/without for valid
    pairs in <arm>. Zero-baseline pairs are dropped but COUNTED — the readout
    discloses the shrunk n instead of silently narrowing it."""
    cells: dict[tuple[str, int, str], dict] = {}
    for r in rows:
        if r.get("valid") and r.get("arm") == arm:
            cells[(r["task"], r["rep"], r["variant"])] = r
    deltas: list[float] = []
    dropped = 0
    for (task, rep, variant), wo in sorted(cells.items()):
        if variant != "without":
            continue
        w = cells.get((task, rep, "with"))
        if w is None:
            continue
        base = _length(wo)
        if base == 0:
            dropped += 1
            continue
        deltas.append((_length(w) - base) / base)
    return deltas, dropped


def _endpoint(rows: list[dict], arm: str) -> dict:
    deltas, dropped = paired_deltas(rows, arm)
    lo, hi = bootstrap_ci(deltas)
    return {
        "deltas": deltas,
        "dropped": dropped,
        "n": len(deltas),
        "median": statistics.median(deltas) if deltas else None,
        "lo": lo,
        "hi": hi,
    }


def _fact_misses(rows: list[dict], arm: str) -> dict[str, tuple[int, int]]:
    """variant -> (misses, cells) among valid rows in <arm>."""
    out: dict[str, tuple[int, int]] = {}
    for variant in ("without", "with"):
        cells = [
            r
            for r in rows
            if r.get("valid") and r.get("arm") == arm and r.get("variant") == variant
        ]
        misses = sum(1 for r in cells if not r.get("facts_ok"))
        out[variant] = (misses, len(cells))
    return out


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
    out.append(
        "── channel-brevity A/B verdict (#728 P1 gate, registered on #729) " + "─" * 6
    )
    out.append(f"rows: {len(rows)} total, {len(valid)} valid, {len(invalid)} invalid")
    if invalid:
        detail = ", ".join(
            f"{r.get('task')}/{r.get('variant')}/rep{r.get('rep')}" for r in invalid
        )
        out.append(f"invalid rows excluded and disclosed: {detail}")

    ep_ch = _endpoint(valid, "channel")
    ep_ctl = _endpoint(valid, "control")
    facts_ch = _fact_misses(valid, "channel")
    facts_ctl = _fact_misses(valid, "control")

    if not ep_ch["deltas"]:
        out.append(
            "NO ANALYZABLE CHANNEL-ARM PAIRS — nothing to conclude; see artifacts."
        )
        return ("\n".join(out), 1)

    out.append("")
    out.append(_fmt("PRIMARY length rel-delta, channel", ep_ch))
    out.append(_fmt("CONTROL length rel-delta, never-compress", ep_ctl))
    out.append("")
    for arm_name, fm in (("channel", facts_ch), ("control", facts_ctl)):
        for variant in ("without", "with"):
            m, n = fm[variant]
            if n:
                out.append(
                    f"RULE-ZERO facts ({arm_name}, {variant}): {m} miss(es) in {n} cells"
                )

    # Decision rule, verbatim from the #729 registration. Branch order matters:
    # a quality regression disqualifies BEFORE any length branch can support.
    p_lo, p_hi = ep_ch["lo"], ep_ch["hi"]
    c_lo, c_hi = ep_ctl["lo"], ep_ctl["hi"]
    with_m, _ = facts_ch["with"]
    wo_m, _ = facts_ch["without"]
    quality_holds = with_m <= wo_m
    prim_reduces = p_hi is not None and p_hi < 0
    prim_spans = p_lo is not None and p_lo <= 0 <= p_hi
    ctl_reduces = c_hi is not None and c_hi < 0

    if not quality_holds:
        verdict = (
            f"RULE-ZERO REGRESSION: WITH drops required facts more often than WITHOUT "
            f"({with_m} vs {wo_m} misses on the channel arm) — lossy compression; "
            "does not support shipping regardless of length."
        )
    elif prim_reduces and ctl_reduces:
        verdict = (
            "REDUCES BUT COMPRESSES THE NEVER-COMPRESS ARM: channel CI excludes 0 below "
            "AND the verbatim-error control CI excludes 0 below — the component is lossy "
            "in practice (rule zero violated at the control); does not support shipping as written."
        )
    elif prim_reduces:
        verdict = (
            "SUPPORTED: channel-arm length CI excludes 0 below with the rule-zero gate "
            "holding and the never-compress control not reduced — the component shortens "
            "what it should and spares what it must."
        )
    elif p_lo is not None and p_lo > 0:
        verdict = (
            "DIRECTIONAL-POSITIVE (outside the registered branch set): the channel-arm "
            "length CI excludes 0 ABOVE — the component is associated with LONGER "
            "replies here. Flagged for interpretation, not mapped onto a registered branch."
        )
    else:
        verdict = (
            "INCONCLUSIVE: the channel-arm CI includes 0 at this n — the A/B does not "
            "demonstrate a length effect; a component that costs context and does not "
            "move the endpoint does not clear the ship bar."
        )
    out.append("")
    out.append(f"VERDICT: {verdict}")

    ctl_med = ep_ctl["median"]
    out.append("")
    out.append(
        f"CHANNEL_BREVITY_AB_RESULT primary_median={ep_ch['median']:+.3f} "
        f"ci{CI_PCT}={p_lo:+.3f},{p_hi:+.3f} n_pairs={ep_ch['n']} "
        f"ctl_median={f'{ctl_med:+.3f}' if ctl_med is not None else 'none'} "
        f"facts_ch_without={wo_m} facts_ch_with={with_m} "
        f"facts_ctl_without={facts_ctl['without'][0]} facts_ctl_with={facts_ctl['with'][0]} "
        f"seed={BOOTSTRAP_SEED}"
    )
    return ("\n".join(out), 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--hash-with", default="?")
    ap.add_argument("--claude-version", default="?")
    args = ap.parse_args(argv)
    text, rc = analyze(load_rows(args.results))
    print(text)
    print(
        f"pins: component={args.hash_with[:12]} claude={args.claude_version} "
        f"bootstrap_seed={BOOTSTRAP_SEED} ci_pct={CI_PCT}"
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
