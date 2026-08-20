#!/usr/bin/env python3
"""Exit-token mixture over #1236 verify traces — the discrimination run's PRIMARY output.

The pre-registration names a statistic (the distribution of `pane_shows_payload`
exit-tick candidates across strands, with exact Clopper-Pearson intervals) and,
after review, names the invocation that computes it (conf=0.80). Neither is worth
anything without a third thing: an instrument that computes it AT ALL. There was
none — the trace writer and its own test were the only readers of a trace in the
repo — so this module is that instrument.

WHAT IT DOES NOT DO, deliberately:

  * It does not re-derive the candidate. `pane_trace_render` (lib-common.sh)
    already reconstructs each tick offline by calling the REAL `_pane_trace_candidate`
    and `_pane_strip_chrome`, precisely so a rendered explanation cannot drift from
    the predicate it explains. Re-implementing that logic here in Python would
    create a second copy that agrees today and diverges silently later — the exact
    failure this issue is about. So this module SHELLS OUT to the shipped renderer
    and consumes its JSONL.
  * It does not compute Clopper-Pearson. That lives in `boot-strand-summary.py`
    and is imported.
  * It does not decide anything the pre-registration did not ratify. No
    elimination claim is reachable here at any n below ELIMINATION_MIN_N, and the
    code refuses rather than trusting the caller not to ask.

THE EXIT-TICK RULE, verified rather than assumed. The exit tick is the LAST row of
`ticks.tsv`. Both tracer call sites are inside the verify loop — lib-common.sh:1810
on the held path (which then `continue`s) and :1814 on the not-held path (which
falls through to the `case "$box"` that returns 0). Nothing traces after the loop;
`_pane_recover_unconfirmed_send` does not call the tracer. So for a CLEAN EXIT —
which is exactly the false-clean population under study — the last traced tick is
the tick on which the loop decided. A trace whose last tick is `held` did not exit
clean; it is reported separately and never counted into the mixture, because it is
a different event.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_summary():
    """Import cp_interval from the sibling module rather than reimplementing it."""
    path = _HERE / "boot-strand-summary.py"
    spec = importlib.util.spec_from_file_location("_bss", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The ratified statistic. 0.80 TWO-SIDED is how Clopper-Pearson is invoked; the
# thing being asserted is its LOWER limit, which is a 90% ONE-SIDED bound. Those
# are the same number and different words, and the pre-registration uses the
# words while the code uses the number — so every artifact carries both.
RATIFIED_CONF = 0.80
STAT_LABEL = (
    "90% one-sided lower bound (Clopper-Pearson, invoked conf=0.80 two-sided)"
)
DOMINANT_THRESHOLD = 0.50
ELIMINATED_THRESHOLD = 0.10
# Smallest n at which a zero-count candidate's CP upper bound falls below
# ELIMINATED_THRESHOLD at RATIFIED_CONF. Derived, asserted at import by
# --self-test, never hardcoded as folklore: n=21 gives 10.4%, n=22 gives 9.9%.
ELIMINATION_MIN_N = 22

# The five verdicts _pane_trace_candidate can emit (lib-common.sh:1386-1430).
# Listed so a candidate seen ZERO times still appears in the output with its
# bounds — an absent row and a zero row are different claims, and only one of
# them is true when nobody looked.
ALL_TOKENS = ("empty-box", "below-floor", "not-substring", "no-region", "held")

# What each token indicates, per the pre-registration's table.
TOKEN_MEANING = {
    "empty-box": "render lag",
    "below-floor": "the _PANE_MIN_VISIBLE_MATCH floor",
    "not-substring": "chrome the stripper misses",
    "no-region": "box never drawn (a DIFFERENT defect, not this one)",
    "held": "not a false-clean; the loop should not have exited",
}


def render_trace(trace_dir: Path, lib_common: Path) -> list[dict]:
    """Run the SHIPPED pane_trace_render over one trace dir, return its ticks.

    Shelling out is the point, not a limitation — see the module docstring.
    `set +e` after sourcing because lib-common.sh arms `set -e` at source time
    and a non-zero probe inside the renderer would otherwise kill the shell
    before it printed anything.
    """
    script = f'set +e; . "{lib_common}" >/dev/null 2>&1; set +e; pane_trace_render "{trace_dir}"'
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=120
    )
    ticks = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ticks.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return ticks


def exit_token(ticks: list[dict]) -> tuple[str | None, str]:
    """(token, status) for one trace. See THE EXIT-TICK RULE in the module docstring.

    Returns (None, reason) rather than guessing whenever the trace cannot answer:
    an unscored trace is disclosed and drops out of the denominator, which is a
    different and more honest thing than being counted as some default token.
    """
    if not ticks:
        return None, "no-ticks"
    last = max(ticks, key=lambda t: t.get("tick", -1))
    cand = last.get("candidate")
    if not isinstance(cand, str) or cand not in ALL_TOKENS:
        return None, f"unknown-candidate:{cand!r}"
    if cand == "held":
        # The loop did not exit clean on this tick, so this boot is not a
        # false-clean. Reported, never folded into the mixture.
        return "held", "held-at-exit"
    return cand, "ok"


def mixture(tokens: list[str], conf: float, cp_interval) -> dict:
    """Per-token share with CP interval. Never pooled, per the ratified bar."""
    n = len(tokens)
    rows = []
    for tok in ALL_TOKENS:
        k = tokens.count(tok)
        if n == 0:
            rows.append({"token": tok, "k": 0, "n": 0, "share": None,
                         "lo": None, "hi": None, "verdict": "NO-DATA"})
            continue
        lo, hi = cp_interval(k, n, conf)
        if lo > DOMINANT_THRESHOLD:
            verdict = "DOMINANT"
        elif k == 0:
            # NOT OBSERVED and ELIMINATED are different statements and both must
            # be sayable. Elimination is unreachable below ELIMINATION_MIN_N and
            # is refused here rather than being available to a hopeful reader.
            if n >= ELIMINATION_MIN_N and hi < ELIMINATED_THRESHOLD:
                verdict = "ELIMINATED"
            else:
                verdict = "NOT-OBSERVED"
        else:
            verdict = "INCONCLUSIVE"
        rows.append({"token": tok, "k": k, "n": n, "share": k / n,
                     "lo": lo, "hi": hi, "verdict": verdict})
    return {"n": n, "rows": rows}


def format_report(mix: dict, unscored: list[tuple[str, str]], conf: float) -> str:
    n = mix["n"]
    out = []
    out.append("EXIT-TOKEN MIXTURE — #1236 discrimination")
    out.append("")
    out.append(f"statistic: {STAT_LABEL}")
    out.append(f"n (clean-exit strands scored): {n}")
    out.append("")
    if n == 0:
        out.append("NO SCORED STRANDS. The mixture is undefined, not empty —")
        out.append("nothing here may be read as evidence about any candidate.")
    else:
        out.append(f"{'token':<15} {'k/n':>7} {'share':>7} {'CP lower':>9} {'CP upper':>9}  verdict")
        for r in mix["rows"]:
            if r["token"] == "held":
                continue
            out.append(
                f"{r['token']:<15} {str(r['k']) + '/' + str(r['n']):>7} "
                f"{r['share']:>6.1%} {r['lo']:>9.1%} {r['hi']:>9.1%}  {r['verdict']}"
                f"   ({TOKEN_MEANING[r['token']]})"
            )
    out.append("")
    if n and n < ELIMINATION_MIN_N:
        out.append(
            f"ELIMINATED is UNAVAILABLE at n={n}: a zero-count candidate's upper bound "
            f"does not fall below {ELIMINATED_THRESHOLD:.0%} until n={ELIMINATION_MIN_N}. "
            "A token not observed is reported as NOT-OBSERVED with its upper bound, "
            "which is a bound and not an elimination."
        )
    if unscored:
        out.append("")
        out.append(f"UNSCORED: {len(unscored)} trace(s) contributed nothing to the mixture —")
        for name, why in unscored:
            out.append(f"  {name}: {why}")
        out.append("These are disclosed rather than dropped: a denominator that quietly")
        out.append("excludes what it could not read is the defect this run exists to study.")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Exit-token mixture over #1236 verify traces")
    ap.add_argument("trace_dirs", nargs="*", type=Path)
    ap.add_argument("--lib-common", type=Path, default=_HERE / "lib-common.sh")
    ap.add_argument("--conf", type=float, default=RATIFIED_CONF,
                    help="two-sided CP conf; default is the RATIFIED 0.80")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="verify the derived constants and the scoring logic, 0 boots")
    args = ap.parse_args(argv)
    summary = _load_summary()
    cp = summary.cp_interval

    if args.self_test:
        return _self_test(cp)

    if not args.trace_dirs:
        print("no trace dirs given; nothing to score", file=sys.stderr)
        return 2

    tokens: list[str] = []
    unscored: list[tuple[str, str]] = []
    held: list[str] = []
    for d in args.trace_dirs:
        if not d.is_dir():
            unscored.append((str(d), "not a directory"))
            continue
        tok, status = exit_token(render_trace(d, args.lib_common))
        if tok is None:
            unscored.append((d.name, status))
        elif tok == "held":
            held.append(d.name)
            unscored.append((d.name, "exit tick is 'held' — not a clean exit, so not a false-clean"))
        else:
            tokens.append(tok)

    mix = mixture(tokens, args.conf, cp)
    if args.json:
        print(json.dumps({"statistic": STAT_LABEL, "conf": args.conf,
                          "mixture": mix, "unscored": unscored, "held": held}, indent=1))
    else:
        print(format_report(mix, unscored, args.conf))
    return 0


def _self_test(cp) -> int:
    """Offline checks. Zero boots, zero model calls, no tmux."""
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r}, want {want!r}")

    # ELIMINATION_MIN_N is DERIVED, so prove the derivation rather than the constant.
    lo21, hi21 = cp(0, 21, RATIFIED_CONF)
    lo22, hi22 = cp(0, 22, RATIFIED_CONF)
    check("n=21 upper still >= 10%", hi21 >= ELIMINATED_THRESHOLD, True)
    check("n=22 upper < 10%", hi22 < ELIMINATED_THRESHOLD, True)
    check("ELIMINATION_MIN_N", ELIMINATION_MIN_N, 22)

    # The ratified decision table, recomputed from the shipped function.
    for k, want in ((10, "DOMINANT"), (9, "DOMINANT"), (8, "DOMINANT"),
                    (7, "INCONCLUSIVE"), (6, "INCONCLUSIVE")):
        m = mixture(["empty-box"] * k + ["below-floor"] * (10 - k), RATIFIED_CONF, cp)
        row = next(r for r in m["rows"] if r["token"] == "empty-box")
        check(f"{k}-of-10 verdict", row["verdict"], want)

    # A zero-count token at n=10 is NOT-OBSERVED, never ELIMINATED.
    m = mixture(["empty-box"] * 10, RATIFIED_CONF, cp)
    row = next(r for r in m["rows"] if r["token"] == "not-substring")
    check("0-of-10 verdict", row["verdict"], "NOT-OBSERVED")
    check("0-of-10 upper ~20.6%", round(row["hi"] * 100, 1), 20.6)

    # And it only becomes ELIMINATED once n clears the derived floor.
    m = mixture(["empty-box"] * ELIMINATION_MIN_N, RATIFIED_CONF, cp)
    row = next(r for r in m["rows"] if r["token"] == "not-substring")
    check(f"0-of-{ELIMINATION_MIN_N} verdict", row["verdict"], "ELIMINATED")

    # Empty input is UNDEFINED, not a clean sweep for anything.
    m = mixture([], RATIFIED_CONF, cp)
    check("n=0 verdicts", {r["verdict"] for r in m["rows"]}, {"NO-DATA"})

    # exit_token: last tick wins, unreadable is None, held is separated.
    check("exit=last tick", exit_token([{"tick": 0, "candidate": "held"},
                                        {"tick": 1, "candidate": "empty-box"}])[0], "empty-box")
    check("no ticks", exit_token([])[0], None)
    check("held at exit", exit_token([{"tick": 3, "candidate": "held"}])[0], "held")
    check("unknown token", exit_token([{"tick": 1, "candidate": "banana"}])[0], None)

    # The statistic label must carry BOTH forms so an artifact proves its own
    # conformance to a pre-registration that says "90% one-sided".
    check("label names one-sided", "90% one-sided" in STAT_LABEL, True)
    check("label names invocation", "conf=0.80" in STAT_LABEL, True)

    for f in fails:
        print(f"FAIL {f}")
    print(f"self-test: {'PASS' if not fails else str(len(fails)) + ' FAILURES'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
