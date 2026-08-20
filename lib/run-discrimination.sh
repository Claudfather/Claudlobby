#!/usr/bin/env bash
# run-discrimination.sh — the #1236 discrimination run, gated on REAL boots.
#
# The gate is wired here rather than documented because an instruction to run it
# is not a gate. Three instructions failed on this issue alone: a conf that had to
# be passed by every caller, a deadline default nobody overrode, and #1032, whose
# whole thesis is that a usage gap closed by intending to remember is not closed.
#
# WHY THE GATE BOOTS FOR REAL. Mode C passed every fixture test in #1293 and was
# then found by two real boots. A fixture cannot surprise you about the shape of
# real output -- it is written by the same person, at the same sitting, under the
# same understanding of the layout as the analyzer itself. So the preflight spends
# two real boots and asserts that clean ones contribute NOTHING through the
# production path, on the code that is about to run, checked at that moment.
#
# Two boots against a sixty-one boot matrix. It is proven to fire rather than
# designed to: it is the exact check that caught mode C.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
set +e

BLOCKS="${BLOCKS:-31}"
LOAD="${LOAD:-20}"
DEADLINE="${DEADLINE:-120}"
OUT="${OUT:-$(safe_mktemp_dir discrimination)}"

say() { printf '\n=== %s ===\n' "$1"; }

say "PREFLIGHT: 2 real boots for the gate"
pre_log="$OUT/preflight.log"
if ! bash "$LIB_DIR/boot-strand-sampler.sh" -n 1 --trace-arms "on off" \
        --deadline "$DEADLINE" --keep > "$pre_log" 2>&1; then
    printf 'preflight sampler FAILED; see %s\n' "$pre_log" >&2
    exit 5
fi
pre_art="$(sed -n 's/^kept artifacts at \(.*\)$/\1/p' "$pre_log" | tail -1)/artifacts"
if [ ! -f "$pre_art/rows.jsonl" ]; then
    printf 'preflight produced no rows.jsonl at %s -- cannot gate\n' "$pre_art" >&2
    exit 5
fi
printf 'preflight artifacts: %s\n' "$pre_art"

say "GATE: real clean boots must contribute ZERO"
# The gate refuses when handed no real clean boots, so a preflight that happened
# to strand every boot cannot pass it by having nothing to check.
if ! python3 "$LIB_DIR/exit-token-mixture.py" --gate \
        --rows "$pre_art/rows.jsonl" "$pre_art"/trace-boot-*; then
    printf '\nMATRIX NOT RUN. The gate blocks it deliberately: on ~61 boots roughly\n' >&2
    printf '51 are clean, so this defect would not bias the result, it would\n' >&2
    printf 'DETERMINE it -- and every other rung would read green.\n' >&2
    exit 4
fi

say "MATRIX: $BLOCKS blocks x 2 trace arms, --load $LOAD"
mat_log="$OUT/matrix.log"
bash "$LIB_DIR/boot-strand-sampler.sh" -n "$BLOCKS" --trace-arms "on off" \
    --load "$LOAD" --deadline "$DEADLINE" --keep > "$mat_log" 2>&1
mat_rc=$?
mat_art="$(sed -n 's/^kept artifacts at \(.*\)$/\1/p' "$mat_log" | tail -1)/artifacts"
printf 'sampler rc=%s, artifacts: %s\n' "$mat_rc" "$mat_art"

say "MIXTURE"
if [ -f "$mat_art/rows.jsonl" ]; then
    python3 "$LIB_DIR/exit-token-mixture.py" --rows "$mat_art/rows.jsonl" \
        "$mat_art"/trace-boot-* | tee "$OUT/mixture.txt"
else
    printf 'no rows.jsonl in %s -- cannot compute the mixture\n' "$mat_art" >&2
    exit 5
fi

printf '\nrun dir: %s\n' "$OUT"
