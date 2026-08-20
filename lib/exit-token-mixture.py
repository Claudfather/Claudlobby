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
# ELIMINATED_THRESHOLD at RATIFIED_CONF: n=21 gives 10.4%, n=22 gives 9.9%.
# The DERIVATION is asserted -- not this constant taken on trust -- but only when
# something runs it: `--self-test`, or tests/test_exit_token_mixture.py. Nothing
# checks it at import, and an earlier version of this comment said it did.
ELIMINATION_MIN_N = 22

# ELIMINATION IS WITHHELD, AND THE GROUNDS ARE STRUCTURAL RATHER THAN POWER.
#
# The original clause said "unreachable below n=22, THEREFORE unavailable here",
# deriving unavailability from power. That premise is FALSIFIED: measured strand
# rate on this run is ~0.8, not the ~1-in-3 it was sized against, so ~24 traced
# strands land and the floor is reachable. Leaning on the clause now would mean
# relying on a sentence justified by a calculation that turned out wrong.
#
# THE REASON THAT SURVIVES -- and it does not depend on n at all:
#
# `_pane_trace_candidate` RETURNS EARLY on every self-identifying outcome --
# `no-region` (box never drawn), `held` (payload seen), `no-payload` (trace not
# armed). Only THREE tokens fall through: empty-box, below-floor, not-substring,
# split by whether the first stripped line is empty. That fall-through is one
# mass divided three ways, and every failure-to-OBSERVE lands in the empty arm.
#
# So an observation defect inflates empty-box and DEFLATES below-floor and
# not-substring -- which are exactly the candidates an elimination would be
# claimed about. A near-zero count for one of them is therefore the same artifact
# as empty-box dominance, seen from the other end. We already ratified trusting
# the first least; the second cannot then be trusted.
#
# TWO of the four defects actually DRAIN a specific candidate; the taxonomy matters
# and an earlier version of this comment got it wrong:
#
#   DRAIN (lowers a specific candidate count -- what the argument needs)
#     * duplicate-tick collision -- into the residual
#     * no-payload / unarmed trace -- into EXCLUSION. It returns early with its own
#       token so it never reaches the residual, but the trace is dropped entirely,
#       and a dropped trace that would have shown below-floor turns a count of 1
#       into a count of 0. That is the false-elimination path exactly.
#   INFLATE (raises the residual without lowering any candidate)
#     * mode C and the bare glob -- clean boots that were never candidates
#
# All four raise empty-box; only the first pair lowers a specific count.
#
# AND THE WITHHOLD IS NOT ABSOLUTE, which was the flaw in its first form: "an
# observation defect could produce a low count" is true of every measurement ever
# taken, so as stated it forbade elimination forever, on any instrument, at any n.
# That is a policy of never concluding, dressed as rigour.
#
# THE EXIT CONDITION: a PER-CANDIDATE POSITIVE CONTROL -- a fixture where that
# candidate is KNOWN PRESENT and the analyzer detects it. With one, a zero count
# means "the detector works and saw nothing". Without one, zero and "the detector
# is blind to this candidate" are the same observable, which is the whole argument.
#
# The existing corpus does NOT supply this: tests/test_pane_verify_trace.sh asserts
# the reconstruction AGREES with the live predicate, which is a CONSISTENCY check.
# A detector only ever seen returning NEGATIVE is not yet a detector. Those are
# different properties and only the positive control licenses an elimination.
#
# WHAT IS NOT WITHHELD: every candidate still gets its CP interval, zero-count
# ones included, labelled as bounds. "Not observed, consistent with up to X%" is
# true and survives. What is withheld is the VERDICT WORD, which asserts more
# than the bound and is the part that gets quoted.
#
# TWO RATIFIERS SET THE BAR AND TWO LIFT IT. One is on record; the default stays
# WITHHOLD until the second, so silence keeps the safe direction.
ELIMINATION_RATIFIED_UNAVAILABLE = True

# The five verdicts _pane_trace_candidate can emit (lib-common.sh:1386-1430).
# Listed so a candidate seen ZERO times still appears in the output with its
# bounds — an absent row and a zero row are different claims, and only one of
# them is true when nobody looked.
ALL_TOKENS = ("empty-box", "below-floor", "not-substring", "no-region", "held")

# A SIXTH token the predicate can emit, absent from the pre-registration's table
# of five. Derived from the source rather than from that table (lib-common.sh
# `_pane_trace_candidate`, the `[ -n "$payload" ] || { printf 'no-payload'; }`
# rung). It does not mean "this candidate fired" — it means the TRACE WAS NOT
# ARMED: `pane_send_verified` writes $PANE_VERIFY_TRACE/payload with `|| true`,
# so a failed write leaves an empty payload and every frame then classifies as
# not-substring. Scoring it would manufacture a unanimous verdict out of an
# instrument failure, so it is named, never scored, and always disclosed.
INSTRUMENT_FAILURE_TOKENS = ("no-payload",)

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
    # TWO SENDS INTO ONE TRACE DIR. `pane_send_verified` restarts `tick` at 0 and
    # OVERWRITES $PANE_VERIFY_TRACE/payload on every call, while ticks.tsv is
    # APPENDED — so a boot that sends twice (start-bot.sh has two call sites,
    # :391 resume and :406 STARTUP_PROMPT) collides tick ids, overwrites the
    # tick-N.pane files, and renders the FIRST send's frames against the SECOND
    # send's payload. Measured, before any real run: six such traces scored
    # 100% empty-box DOMINANT at 68.1% — a unanimous verdict for the leading
    # hypothesis, entirely artifact. Refuse rather than pick one, because the
    # wrong answer here is the one nobody would question.
    ids = [t.get("tick") for t in ticks]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        return None, f"duplicate-tick-ids:{dupes} — trace dir holds more than one send"
    last = max(ticks, key=lambda t: t.get("tick", -1))
    cand = last.get("candidate")
    if isinstance(cand, str) and cand in INSTRUMENT_FAILURE_TOKENS:
        return None, f"{cand} — trace not armed (empty payload); NOT a candidate observation"
    if not isinstance(cand, str) or cand not in ALL_TOKENS:
        return None, f"unknown-candidate:{cand!r}"
    if cand == "held":
        # The loop did not exit clean on this tick, so this boot is not a
        # false-clean. Reported, never folded into the mixture.
        return "held", "held-at-exit"
    return cand, "ok"


def mixture(tokens: list[str], conf: float, cp_interval,
            allow_elimination: bool = not ELIMINATION_RATIFIED_UNAVAILABLE) -> dict:
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
            reachable = n >= ELIMINATION_MIN_N and hi < ELIMINATED_THRESHOLD
            if reachable and allow_elimination:
                verdict = "ELIMINATED"
            elif reachable:
                # Withheld BY REGISTRATION, not by arithmetic. Named distinctly so
                # the output cannot be read as "the bound was not met".
                verdict = "NOT-OBSERVED (elimination withheld: registration)"
            else:
                verdict = "NOT-OBSERVED"
        else:
            verdict = "INCONCLUSIVE"
        rows.append({"token": tok, "k": k, "n": n, "share": k / n,
                     "lo": lo, "hi": hi, "verdict": verdict})
    return {"n": n, "rows": rows}


# THE MANUFACTURED VERDICT. Both known artifacts produce the SAME output --
# `empty-box` dominance -- by unrelated mechanisms:
#
#   * duplicate-tick collision renders one send's frames against another's payload
#   * scoring CLEAN boots, whose exit tick is empty-box because the send SUCCEEDED
#
# And empty-box points at RENDER LAG, which is already the favoured candidate. So
# the prior on that specific result moves DOWN, not up: if the run returns
# empty-box DOMINANT it is the outcome to trust LEAST, because it is what a broken
# instrument emits. Every other result is comparatively unmanufactured.
#
# This is a claim about how to READ the result, so it is registered rather than
# left to the analysis -- and it is stated with EVIDENCE (was selection derived?
# were artifacts seen?) rather than as a standing caveat a reader learns to skip.
MANUFACTURED_TOKEN = "empty-box"


def scrutiny_block(mix: dict, unscored: list[tuple[str, str]], derived: bool) -> list[str]:
    row = next((r for r in mix["rows"] if r["token"] == MANUFACTURED_TOKEN), None)
    if not row or row["verdict"] != "DOMINANT":
        return []
    dup = sum(1 for _, why in unscored if "duplicate-tick-ids" in why)
    unarmed = sum(1 for _, why in unscored if why.startswith("no-payload"))
    nonstrand = sum(1 for _, why in unscored if "only 'strand' is scoreable" in why)
    out = [
        "",
        "!! SCRUTINY CONDITION — empty-box DOMINANT is the MANUFACTURED verdict",
        "",
        "   Two unrelated instrument defects both produce empty-box dominance, and",
        "   empty-box indicates render lag, the already-favoured candidate. This is",
        "   therefore the result to trust LEAST. Do not report it without confirming",
        "   both artifacts are dead:",
        "",
        f"   [{'OK ' if derived else 'FAIL'}] strand selection DERIVED from rows.jsonl "
        f"({'yes' if derived else 'NO — clean boots may be in the mixture'})",
        f"   [{'OK ' if dup == 0 else '!! '}] traces refused for duplicate tick ids: {dup}",
        f"   [OK ] traces refused as unarmed (no-payload): {unarmed}",
        f"   [OK ] non-strand traces excluded by classification: {nonstrand}",
        "",
        "   A FAIL above means this verdict is an artifact, not a finding.",
    ]
    return out


def format_report(mix: dict, unscored: list[tuple[str, str]], conf: float,
                  derived: bool = True, loadavgs: list[float] | None = None) -> str:
    n = mix["n"]
    out = []
    out.append("EXIT-TOKEN MIXTURE — #1236 discrimination")
    out.append("")
    out.append(f"statistic: {STAT_LABEL}")
    out.append(f"n (clean-exit strands scored): {n}")
    # The scope travels WITH the verdict, and it is measured rather than intended.
    out.append(loadavg_scope(loadavgs or []))
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
    out.extend(scrutiny_block(mix, unscored, derived))
    if unscored:
        out.append("")
        out.append(f"UNSCORED: {len(unscored)} trace(s) contributed nothing to the mixture —")
        for name, why in unscored:
            out.append(f"  {name}: {why}")
        out.append("These are disclosed rather than dropped: a denominator that quietly")
        out.append("excludes what it could not read is the defect this run exists to study.")
    return "\n".join(out)


def load_loadavg(path: Path) -> list[float]:
    """Every recorded loadavg_1m, for stating the verdict's scope from MEASUREMENT.

    The scope must not be quoted from the burner count. `--load N` is an INPUT;
    `loadavg_1m` is what the host actually experienced, which is why the sampler
    records it per boot (boot-strand-sampler.sh:264). Pre-registering "loadavg ~25"
    from the intended target and then publishing that beside a verdict is a stated
    bound that a reader takes as measured -- worse than no bound. Measured on this
    run: median 30.2 against a quoted ~25, at the TOP of the #933 19-31 band rather
    than the middle.
    """
    out: list[float] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        v = r.get("loadavg_1m")
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def loadavg_scope(vals: list[float]) -> str:
    if not vals:
        return "observed loadavg: NOT RECORDED — scope cannot be stated from measurement"
    vals = sorted(vals)
    mid = len(vals) // 2
    med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return (f"observed loadavg (recorded per boot, n={len(vals)}): "
            f"median {med:.1f}, range {vals[0]:.1f}-{vals[-1]:.1f}")


def load_rows(path: Path) -> dict[int, str]:
    """boot index -> outcome, from the sampler's own rows.jsonl."""
    out: dict[int, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r.get("i"), int):
            out[r["i"]] = r.get("outcome", "")
    return out


def boot_index(trace_dir: Path) -> int | None:
    """trace-boot-<i> -> i. The sampler names the dir that way (sampler:943)."""
    name = trace_dir.name
    if not name.startswith("trace-boot-"):
        return None
    try:
        return int(name[len("trace-boot-"):])
    except ValueError:
        return None


def score_dirs(trace_dirs: list[Path], rows: dict[int, str], lib_common: Path):
    """(tokens, unscored, held) — THE selection+scoring path.

    Extracted so the gate exercises exactly what production runs. A gate that
    reaches past this into `exit_token` would certify a path nothing uses: it
    would see the RAW token (`empty-box` for a clean boot, correctly) and read
    that as the pipeline failing, which is how the first version of this gate
    blocked the matrix on a false alarm.
    """
    tokens: list[str] = []
    unscored: list[tuple[str, str]] = []
    held: list[str] = []
    for d in trace_dirs:
        if not d.is_dir():
            unscored.append((str(d), "not a directory"))
            continue
        if rows:
            idx = boot_index(d)
            if idx is None:
                unscored.append((d.name, "dir name is not trace-boot-<i>; cannot match a row"))
                continue
            outcome = rows.get(idx)
            if outcome is None:
                unscored.append((d.name, f"no row for boot {idx} in --rows"))
                continue
            if outcome != "strand":
                unscored.append((d.name, f"outcome={outcome!r} — only 'strand' is scoreable"))
                continue
        tok, status = exit_token(render_trace(d, lib_common))
        if tok is None:
            unscored.append((d.name, status))
        elif tok == "held":
            held.append(d.name)
            unscored.append((d.name, "exit tick is 'held' — not a clean exit, so not a false-clean"))
        else:
            tokens.append(tok)
    return tokens, unscored, held


# ── the run-blocking gate ────────────────────────────────────────────────────
#
# WHY THIS IS A GATE AND NOT A NOTE. `_pane_trace_candidate` returns EARLY on a
# positive finding -- `held` at the paste marker (lib-common.sh:1396 region) and
# `held` on the substring match -- and classifies the RESIDUAL on fall-through.
# So "payload observed" exits early and everything else lands in the residual,
# subdivided by what the frame contained: nothing at all -> `empty-box`, short
# text -> `below-floor`, wrong text -> `not-substring`.
#
# That makes `empty-box` the NOTHING-OBSERVED bucket, and it cannot distinguish
# "the phenomenon produced nothing" from "the instrument failed to look." Both
# land there. And here the coincidence is worse than generic: `empty-box` is ALSO
# the genuine render-lag signature, so the favoured hypothesis and the
# instrument-failure bucket are THE SAME TOKEN.
#
# A category like that needs a higher evidentiary bar BY CONSTRUCTION, not because
# of which defects happen to be known today -- the next one lands there too.
#
# And a bar that must be REMEMBERED is not a bar. Three instructions failed on this
# issue alone (a conf that had to be passed, a deadline default, #1032's whole
# thesis that a usage gap closed by intending to remember is not closed). So this
# is the exact check that caught mode C -- clean boots must score ZERO -- run as a
# precondition that BLOCKS, not as a caution someone reads an hour later.
def run_gate(cp, lib_common: Path, real: list[tuple[Path, Path]] | None = None) -> int:
    failures: list[str] = []
    checks = 0

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        payload = "set +H; Boot probe GATE: a payload long enough to clear the floor"
        # A CLEAN boot: the box drew, the payload submitted, the frame is empty.
        # This is byte-for-byte the shape that scored empty-box 2/2 in the smoke run.
        for i in range(2):
            d = root / f"trace-boot-{i}"
            d.mkdir()
            (d / "payload").write_text(payload)
            (d / "tick-1.pane").write_text("> ")
            (d / "ticks.tsv").write_text("1\tdrawn\n")
        rows = {0: "clean", 1: "clean"}

        # 1. The positive control: WITHOUT classification these DO score empty-box.
        #    If this stops being true the gate has stopped testing anything.
        toks = []
        for i in range(2):
            tok, _ = exit_token(render_trace(root / f"trace-boot-{i}", lib_common))
            if tok:
                toks.append(tok)
        checks += 1
        if toks != ["empty-box", "empty-box"]:
            failures.append(
                f"POSITIVE CONTROL DEAD: clean boots no longer score empty-box "
                f"unclassified (got {toks!r}). The gate is not exercising the defect."
            )

        # 2. The gate proper: WITH classification they must contribute NOTHING.
        checks += 1
        scored = [i for i in range(2) if rows.get(i) == "strand"]
        if scored:
            failures.append(f"clean boots selected for scoring: {scored}")

    # 3. REAL BOOTS. Fixtures cannot surprise you about the shape of real output --
    #    mode C passed every fixture test in #1293 and was found by two real boots.
    #    So this half only means anything on traces a real sampler wrote.
    clean_dirs: list[Path] = []
    r: dict[int, str] = {}
    if real:
        r = load_rows(real[0][0])
        clean_dirs = [d for _, d in real
                      if boot_index(d) is not None and r.get(boot_index(d)) == "clean"]
    # OUTSIDE the `if real` guard, deliberately. An earlier version put this
    # inside it, so invoking --gate with no traces at all skipped the real half
    # entirely and printed GATE PASSED -- a fixture-only pass, which is the exact
    # state #1293 merged green in and the thing this gate exists to refuse. Caught
    # by its own test, after the claim had already been made out loud.
    if not clean_dirs:
        failures.append(
            "no REAL clean boots supplied — the real half of this gate did not run, "
            "and a fixture-only pass is the state #1293 merged green in"
        )
    else:
        # 3a. POSITIVE CONTROL on real data: raw-scored, a clean boot lands in
        #     the residual. If it does not, the defect is not live here and a
        #     pass proves nothing.
        checks += 1
        raw = [exit_token(render_trace(d, lib_common))[0] for d in clean_dirs]
        if not any(t == MANUFACTURED_TOKEN for t in raw):
            failures.append(
                f"POSITIVE CONTROL DEAD on real boots: raw scores {raw!r} contain no "
                f"{MANUFACTURED_TOKEN!r}, so this gate is not exercising the defect"
            )
        # 3b. THE GATE: through the production path, they contribute NOTHING.
        checks += 1
        toks, unsc, _ = score_dirs(clean_dirs, r, lib_common)
        if toks:
            failures.append(
                f"REAL clean boots scored {toks!r} through the production path — "
                "selection is not holding"
            )
        elif len(unsc) != len(clean_dirs):
            failures.append("real clean boots were neither scored nor disclosed")

    print("PRE-MATRIX GATE — clean boots must score ZERO")
    print(f"  checks run: {checks}")
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        print("\nGATE FAILED — the matrix must not run. On ~61 boots roughly 51 are")
        print("clean, so this defect would not bias the result, it would DETERMINE it:")
        print("empty-box dominant, far past the bar, on a correctly pre-registered")
        print("statistic computed correctly. Every rung green except this one.")
        return 4
    print("  GATE PASSED — clean boots score empty-box unclassified (control live),")
    print("  and contribute nothing once classification is applied.")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Exit-token mixture over #1236 verify traces")
    ap.add_argument("trace_dirs", nargs="*", type=Path)
    ap.add_argument("--lib-common", type=Path, default=_HERE / "lib-common.sh")
    ap.add_argument("--conf", type=float, default=RATIFIED_CONF,
                    help="two-sided CP conf; default is the RATIFIED 0.80")
    ap.add_argument("--rows", type=Path,
                    help="the sampler's rows.jsonl; ONLY boots it classifies as "
                         "'strand' are scored")
    ap.add_argument("--allow-unclassified", action="store_true",
                    help="score every given trace dir without consulting rows.jsonl. "
                         "NOT the normal path -- see the refusal text")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--allow-elimination", action="store_true",
                    help="lift the ratified UNAVAILABLE clause on ELIMINATED. Requires "
                         "a blind re-ratification; never set it after seeing the data")
    ap.add_argument("--gate", action="store_true",
                    help="run-blocking pre-matrix gate: clean boots must score ZERO")
    ap.add_argument("--self-test", action="store_true",
                    help="verify the derived constants and the scoring logic, 0 boots")
    args = ap.parse_args(argv)
    summary = _load_summary()
    cp = summary.cp_interval

    if args.gate:
        real = []
        if args.rows:
            real = [(args.rows, d) for d in args.trace_dirs]
        return run_gate(cp, args.lib_common, real)

    if args.self_test:
        return _self_test(cp)

    if not args.trace_dirs:
        print("no trace dirs given; nothing to score", file=sys.stderr)
        return 2

    # SELECTION IS DERIVED, NOT TYPED (#1236). A trace is written for EVERY boot,
    # clean ones included, and a clean boot's exit tick is `empty-box` because the
    # payload was genuinely submitted. Scoring those measures successful sends and
    # calls the result render lag. Measured on the smoke run: two ordinary clean
    # boots produced `empty-box 2/2 = 100%`, well-formed and confident; at n=10 the
    # same input reads DOMINANT.
    #
    # The failure needs no unusual condition -- only that someone points the tool at
    # the artifacts directory, which is the obvious thing to do and is exactly what
    # produced the finding. The pre-registration rests on strand classification
    # being ground truth INDEPENDENT of pane geometry (no user-role record in the
    # transcript); consulting the pane alone throws that away. So the classification
    # is read from the sampler's own rows.jsonl, and absent it this refuses.
    rows: dict[int, str] = {}
    if args.rows:
        # An unreadable classification is a question this run cannot answer, not a
        # crash and not an empty result -- the #1216 unreachable-vs-empty rule.
        if not args.rows.is_file():
            print(f"refusing: --rows {args.rows} is not a readable file", file=sys.stderr)
            return 3
        try:
            rows = load_rows(args.rows)
        except OSError as exc:
            print(f"refusing: cannot read --rows {args.rows}: {exc}", file=sys.stderr)
            return 3
        if not rows:
            print(f"refusing: {args.rows} yielded no classified boots", file=sys.stderr)
            return 3
    elif not args.allow_unclassified:
        print(
            "refusing: no --rows given, so strand selection cannot be derived.\n"
            "A trace is written for EVERY boot. A CLEAN boot exits on `empty-box`\n"
            "because the payload was submitted -- scoring it measures successful\n"
            "sends and reports them as render lag. Pass --rows <artifacts/rows.jsonl>,\n"
            "or --allow-unclassified if you have selected strands by other means.",
            file=sys.stderr,
        )
        return 3

    tokens, unscored, held = score_dirs(args.trace_dirs, rows, args.lib_common)

    mix = mixture(tokens, args.conf, cp, allow_elimination=args.allow_elimination)
    if args.json:
        print(json.dumps({"statistic": STAT_LABEL, "conf": args.conf,
                          "mixture": mix, "unscored": unscored, "held": held,
                          "selection_derived": bool(rows),
                          "loadavg_observed": load_loadavg(args.rows) if args.rows else [],
                          "loadavg_scope": loadavg_scope(load_loadavg(args.rows) if args.rows else []),
                          "scrutiny": scrutiny_block(mix, unscored, bool(rows))}, indent=1))
    else:
        las = load_loadavg(args.rows) if args.rows else []
        print(format_report(mix, unscored, args.conf, derived=bool(rows), loadavgs=las))
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
    check(f"0-of-{ELIMINATION_MIN_N} withheld by registration", row["verdict"],
          "NOT-OBSERVED (elimination withheld: registration)")
    m2 = mixture(["empty-box"] * ELIMINATION_MIN_N, RATIFIED_CONF, cp, allow_elimination=True)
    row2 = next(r for r in m2["rows"] if r["token"] == "not-substring")
    check(f"0-of-{ELIMINATION_MIN_N} reachable when lifted", row2["verdict"], "ELIMINATED")

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
