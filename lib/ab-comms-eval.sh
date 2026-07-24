#!/bin/bash
# ab-comms-eval.sh — #729 stage-C A/B comms-eval harness (SCAFFOLDING).
#
# Measures whether the token-efficiency comms protocol (#716) actually saves
# tokens by running the SAME comms-heavy task battery against two otherwise
# identical bots: ab-with (protocols: [token-efficiency]) vs ab-without. It is a
# sibling of validate-bot-change.sh with an OWNED COPY of its scaffolding — that
# harness stubs `claude` and can only prove framework events; this property under
# test is MODEL behavior, so real runs boot a real interactive `claude` (the
# mechanic proven by interactive-claude-spike.sh, #729 stage B).
#
# SCOPE OF THIS FILE (F2/F4-independent scaffolding):
#   - the two-variant fixture, composed by real `claudlobby generate`
#   - the paired task x rep x variant run matrix
#   - the two gated token axes (protocol_sensitive + cost_weighted_total) via
#     lib/transcript-usage.py (#729 stage A)
#   - the pass-bar / verdict computation (cost-weighted CO-PRIMARY,
#     per-task-type, INCONCLUSIVE-never-PASS)
# The task CONTENT and the quality rubric are F2-ratified — they slot behind the
# battery_* and score_quality seams as STUBS until Chris ratifies them.
#
# MODES:
#   --dry-run       CI-safe. Zero model calls, no CLAUDE_CONFIG_DIR auth touch.
#                   Synthesizes deterministic transcripts and drives them through
#                   the REAL measurement + verdict path — the fork-independent
#                   wiring. This is what tests/test_ab_comms_eval.py exercises.
#   AB_EVAL_REAL=1  Real gate. REFUSED by this scaffolding: the battery content is
#                   an F2 stub and library/protocols/token-efficiency.md is
#                   unmerged (P1). A real gate needs both. The proven boot/dispatch
#                   /recover recipe lives in interactive-claude-spike.sh; wiring it
#                   into run_cell is F2 follow-up.
#
# The default verdict is INCONCLUSIVE BY CONSTRUCTION: with no F2-ratified
# threshold T and a stub quality scorer, the harness can never emit PASS. That is
# the intended safety posture — the skeleton cannot green a real gate pre-F2.
#
# KNOWN BLOCKER for real runs (surfaced by stage B): transcript-usage.py sums
# per-line, but interactive Claude Code writes one assistant message as N
# content-block lines EACH repeating message.usage, so real transcripts
# over-count. A --dedup-by-message-id mode belongs in the parser OWN PR (it moves
# the published stage-A read-out). Dry-run is unaffected — synth writes one line
# per message.
#
# Opt-in cost when real (post-F2): ~36-60 short real sessions per full run.
set -euo pipefail

LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$LIB/.." && pwd)"
# shellcheck source=lib-common.sh
. "$LIB/lib-common.sh"

MODEL_DRY="claude-opus-4-8-DRYRUN"

# --- defaults ---------------------------------------------------------------
DRY_RUN=0
TASKS_ARG=""
REPS=3
REPS_MAX=5
THRESHOLD=""          # empty sentinel -> verdict INCONCLUSIVE (no F2-ratified T)
COST_THRESHOLD="0.0"  # co-primary floor; F2 may raise above no-regression
KEEP=0
TASKS_DEFAULT="T1 T2 T3 T4 T5 T6"

usage() {
    cat <<'USAGE'
usage: ab-comms-eval.sh [--dry-run] [--tasks N|"T1 T3"] [--reps N] [--reps-max N]
                        [--threshold F] [--cost-threshold F] [--keep]
                        [--compute-verdict RESULTS.jsonl]
  --dry-run            CI-safe synthetic run (no model calls, no auth touch).
  --tasks              a count (first N of the battery) or a space/comma list.
  --reps               initial reps per cell (default 3).
  --reps-max           stopping-rule cap (default 5).
  --threshold          F2 T: required protocol_sensitive relative reduction.
  --cost-threshold     required cost_weighted_total reduction (default 0.0).
  --keep               keep the throwaway root for inspection.
The verdict/pass-bar computation lives in lib/ab-comms-verdict.py — run it
directly to (re)compute a verdict from a results.jsonl. Real gate is opt-in via
AB_EVAL_REAL=1 and is refused by this scaffolding.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --tasks) TASKS_ARG="$2"; shift ;;
        --reps) REPS="$2"; shift ;;
        --reps-max) REPS_MAX="$2"; shift ;;
        --threshold) THRESHOLD="$2"; shift ;;
        --cost-threshold) COST_THRESHOLD="$2"; shift ;;
        --keep) KEEP=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'ab-comms-eval: unknown arg %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

die() { printf 'ab-comms-eval: %s\n' "$*" >&2; exit 1; }

_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    else shasum -a 256 "$1" | awk '{print $1}'; fi
}

# --- verdict computation: thin wrapper over lib/ab-comms-verdict.py ----------
# The pass-bar / bootstrap / verdict logic is a standalone stdlib module (sibling
# to transcript-usage.py, following the dispatch-overdue.py precedent) so it is
# directly unit-testable and F2 can extend the threshold + scorer there. This
# wrapper only threads the harness pins into it.
compute_verdict() {  # $1 results.jsonl  $2 out.json  $3 reps_now
    local ph=""
    [ "${PROTO_PLACEHOLDER:-0}" = 1 ] && ph="--proto-placeholder"
    python3 "$LIB/ab-comms-verdict.py" "$1" \
        --out "$2" \
        --threshold "$THRESHOLD" \
        --cost-threshold "$COST_THRESHOLD" \
        --reps-now "$3" \
        --reps-max "$REPS_MAX" \
        --claude-version "${CLAUDE_VER:-unknown}" \
        --proto-hash "${PROTO_HASH:-unknown}" \
        --weights-file "${WEIGHTS_FILE:-}" \
        $ph
}

# --- mode gate --------------------------------------------------------------
if [ "$DRY_RUN" != 1 ]; then
    if [ "${AB_EVAL_REAL:-0}" = 1 ]; then
        die "real mode (AB_EVAL_REAL=1) is REFUSED by this scaffolding: the task battery is an F2 stub and library/protocols/token-efficiency.md is unmerged (P1). The proven boot/dispatch/recover recipe lives in interactive-claude-spike.sh; wiring run_cell to it is F2 follow-up. Use --dry-run for the CI-safe wiring check."
    fi
    printf 'ab-comms-eval: opt-in. Use --dry-run (CI-safe wiring check) or AB_EVAL_REAL=1 (real gate, refused pending F2 battery + P1 protocol).\n'
    exit 0
fi

# --- scaffolding: OWNED COPY of validate-bot-change.sh (provenance-commented) --
# The four pieces below are copied inline from validate-bot-change.sh; they are
# inline there too (not in lib-common.sh) and consolidation is an optional
# follow-up, not this PR.
#
# [1] tmux socket-isolation shim (validate-bot-change.sh :36-51). unset FLEET_NAME
#     so bots resolve to the tmux-<name> fallback; shadow `tmux` so every session
#     op lands on that session private -L server. (Dry-run never boots tmux, but
#     the shim ships so the real path is wired.)
unset FLEET_NAME
vsock() { printf 'tmux-%s' "$1"; }
tmux() {
    local i sock=""
    local -a a=("$@")
    for ((i = 0; i < ${#a[@]}; i++)); do
        case "${a[i]}" in
            -t | -s) [ $((i + 1)) -lt ${#a[@]} ] && sock="$(vsock "${a[i + 1]}")"; break ;;
        esac
    done
    if [ -n "$sock" ]; then command tmux -L "$sock" "$@"; else command tmux "$@"; fi
}

# [2] throwaway ROOT + per-run private TMUX namespace (validate-bot-change.sh
#     :60-74). Literal /tmp so socket sun_path stays under 108 bytes.
ROOT="$(mktemp -d /tmp/ab-comms-eval.XXXXXX)"
TMUX_TMPDIR="$(mktemp -d /tmp/ab-comms-eval-sock.XXXXXX)"
export TMUX_TMPDIR

# [3] cleanup trap (validate-bot-change.sh :80-91).
cleanup() {
    for _s in ab-with ab-without; do
        command tmux -L "$(vsock "$_s")" kill-server 2>/dev/null || true
    done
    [ "$KEEP" = 1 ] || rm -rf "$ROOT" "$TMUX_TMPDIR"
    return 0
}
trap cleanup EXIT

# [4] pass/fail counter (validate-bot-change.sh :126-134).
pass=0; fail=0
check() {
    if [ "$2" = yes ]; then pass=$((pass + 1)); printf '  PASS  %s\n' "$1"
    else fail=$((fail + 1)); printf '  FAIL  %s\n' "$1"; fi
}

# --- F2 SEAMS (stubs until Chris ratifies) ----------------------------------
# battery_* return the per-task dispatch content + the elided-detail ground truth
# the sufficiency checker greps. score_quality is the rubric. All three are
# F2-dependent and deliberately inert here; the mechanical skeleton around them is
# what this PR delivers.
battery_dispatch_text() {  # $1 task
    printf 'STUB dispatch for %s -- F2-pending (real deterministic task content awaits ratification)' "$1"
}
battery_must_persist() {  # $1 task -> the must_persist facts (empty until F2)
    printf ''
}
score_quality() {  # $1 task -> a JSON quality object. Stub passes until F2.
    printf '{"gate":"pass","scorer":"stub","note":"F2-pending"}'
}
mechanical_check() {  # $1 task  $2 must_persist  $3 transcript -> "true"|"false"
    # MECHANICAL seam (always-on in real mode): asserts the [BOTREPORT] ledger-row
    # fields, the mandated ack+completion posts, and content-sufficiency of the
    # must_persist facts against the persisted source. Dry-run has no real session
    # artifacts, so it passes; threading it here (fed the must_persist facts) means
    # F2 / real mode slot the real predicate without restructuring run_cell.
    printf 'true'
}

# --- variant fixture: composed by REAL claudlobby generate -------------------
setup_variants() {
    # Own library tree: symlink each real library entry, EXCEPT protocols, which
    # is a real dir so the placeholder token-efficiency protocol can be injected
    # when P1 is unmerged (real gate runs refuse the placeholder).
    mkdir -p "$ROOT/library/protocols"
    local e name
    for e in "$SRC"/library/*; do
        name="$(basename "$e")"
        [ "$name" = protocols ] && continue
        ln -s "$e" "$ROOT/library/$name"
    done
    for e in "$SRC"/library/protocols/*; do
        ln -s "$e" "$ROOT/library/protocols/$(basename "$e")"
    done
    ln -s "$SRC/templates" "$ROOT/templates"
    ln -s "$SRC/voices" "$ROOT/voices" 2>/dev/null || true
    ln -s "$SRC/lib" "$ROOT/lib"

    local proto="$ROOT/library/protocols/token-efficiency.md"
    if [ -e "$SRC/library/protocols/token-efficiency.md" ]; then
        PROTO_PLACEHOLDER=0
    else
        rm -f "$proto"
        cat > "$proto" <<'MD'
---
title: Token Efficiency (PLACEHOLDER)
description: PLACEHOLDER protocol for the A/B comms-eval scaffolding. Real gate runs refuse it.
---

# Token Efficiency (PLACEHOLDER)

PLACEHOLDER protocol body. The real protocol lands via P1 (#716). Real gate runs
(AB_EVAL_REAL=1) refuse this placeholder and require the merged file.
MD
        PROTO_PLACEHOLDER=1
    fi
    PROTO_HASH="$(_sha256 "$proto")"

    mkdir -p "$ROOT/config-with" "$ROOT/config-without"
    # Two IDENTICAL code-review workers; the ONLY difference is the protocol on WITH.
    cat > "$ROOT/fleet.yaml" <<YAML
fleet:
  name: abeval
  service_prefix: abev
  accounts:
    default: ~/.claude
    with: $ROOT/config-with
    without: $ROOT/config-without
  plugins:
    include_defaults: false
  bots:
    ab-without:
      name: ab-without
      account: without
      expertise:
        - code-review
      dangerously_skip_permissions: true
      channels: []
      telegram:
        handle: ab_without_bot
    ab-with:
      name: ab-with
      account: with
      expertise:
        - code-review
      protocols:
        - token-efficiency
      dangerously_skip_permissions: true
      channels: []
      telegram:
        handle: ab_with_bot
YAML
    if ! CLAUDLOBBY_ROOT="$ROOT" PYTHONPATH="$SRC" python3 -m claudlobby generate >"$ROOT/generate.out" 2>&1; then
        cat "$ROOT/generate.out" >&2
        die "claudlobby generate failed for the A/B fixture"
    fi
}

# --- token measurement: the two gated axes via transcript-usage.py -----------
# NOTE: on REAL interactive transcripts this over-counts (per-line usage
# repetition); see the header blocker. Dry-run synth writes one line per message,
# so it is exact here.
measure_transcript() {  # $1 path (file or dir) -> "in out cc cr ps cwt turns msgs model"
    local path="$1" j msgs
    j="$(python3 "$LIB/transcript-usage.py" --json "$path" 2>/dev/null)" || return 1
    [ -n "$j" ] || return 1
    if [ ! -s "$ROOT/weights.json" ]; then
        printf '%s' "$j" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin).get("weights",{})))' \
            > "$ROOT/weights.json" 2>/dev/null || true
    fi
    if [ -d "$path" ]; then
        msgs="$(find "$path" -name '*.jsonl' -exec cat {} + 2>/dev/null | grep -c . || true)"
    else
        msgs="$(grep -c . "$path" 2>/dev/null || true)"
    fi
    [ -n "$msgs" ] || msgs=0
    printf '%s' "$j" | AB_MSGS="$msgs" python3 -c '
import json, os, sys
d = json.load(sys.stdin)["aggregate"]["main"]
m = "|".join(d.get("models") or []) or "-"
print(d["input_tokens"], d["output_tokens"], d["cache_creation_input_tokens"],
      d["cache_read_input_tokens"], d["protocol_sensitive"], d["cost_weighted_total"],
      d["turns"], os.environ.get("AB_MSGS", "0"), m)
'
}

# --- dry-run cell body: synthesize a clean transcript ------------------------
# Deterministic in (task-index, rep, variant). WITH trims fresh input+output and
# adds a little standing cache weight (the protocol text) -- realistic enough to
# exercise both axes and the co-primary gate.
synth_transcript() {  # $1 taskidx  $2 rep  $3 variant  $4 dispatch-text -> echoes the file path
    local ti="$1" rep="$2" variant="$3" dispatch="$4" base out in cc cr f
    # Route the battery dispatch text through the synth user turn so the F2 battery
    # seam is threaded in dry-run too (JSON-sanitize: drop backslashes then quotes).
    dispatch="${dispatch//\\/}"; dispatch="${dispatch//\"/}"
    mkdir -p "$ROOT/transcripts"
    f="$ROOT/transcripts/t${ti}-${variant}-r${rep}.jsonl"
    base=$((800 + ti * 400))
    if [ "$variant" = without ]; then
        out=$((base + rep * 40)); in=$((base * 3)); cc=$((base / 2)); cr=$((base * 20 + rep * 100))
    else
        out=$(((base * 70) / 100 + rep * 10)); in=$(((base * 3 * 88) / 100))
        cc=$((base / 2 + 60)); cr=$((base * 20 + rep * 100 + 400))
    fi
    {
        printf '{"type":"user","message":{"role":"user","content":"%s"}}\n' "$dispatch"
        printf '{"type":"assistant","isSidechain":false,"sessionId":"dry-t%s-%s-r%s","message":{"model":"%s","usage":{"input_tokens":%d,"output_tokens":%d,"cache_creation_input_tokens":%d,"cache_read_input_tokens":%d}}}\n' \
            "$ti" "$variant" "$rep" "$MODEL_DRY" "$in" "$out" "$cc" "$cr"
    } > "$f"
    printf '%s' "$f"
}

# --- run one paired cell -----------------------------------------------------
# In dry-run the cell synthesizes; in real mode (F2 follow-up) it would boot a
# real session via the interactive-claude-spike.sh recipe (seed auth+trust ->
# start-bot.sh -> dispatch.sh -> await the report-back.jsonl ledger row -> recover
# the transcript). Real mode is refused upstream, so only the dry branch runs.
run_cell() {  # $1 task  $2 taskidx  $3 rep  $4 variant
    local task="$1" ti="$2" rep="$3" variant="$4"
    local t0 t1 wall path mline q mech dispatch must
    dispatch="$(battery_dispatch_text "$task")"
    must="$(battery_must_persist "$task")"
    t0="$(date +%s)"
    path="$(synth_transcript "$ti" "$rep" "$variant" "$dispatch")"
    t1="$(date +%s)"; wall=$((t1 - t0))
    if ! mline="$(measure_transcript "$path")"; then
        die "measurement failed for $task/$variant/rep$rep"
    fi
    local in out cc cr ps cwt turns msgs model
    read -r in out cc cr ps cwt turns msgs model <<<"$mline"
    q="$(score_quality "$task")"
    mech="$(mechanical_check "$task" "$must" "$path")"
    printf '{"task":"%s","variant":"%s","rep":%d,"input_tokens":%s,"output_tokens":%s,"cache_creation_input_tokens":%s,"cache_read_input_tokens":%s,"protocol_sensitive":%s,"cost_weighted_total":%s,"turns":%s,"messages":%s,"wall_s":%d,"model":"%s","quality":%s,"mech_ok":%s}\n' \
        "$task" "$variant" "$rep" "$in" "$out" "$cc" "$cr" "$ps" "$cwt" "$turns" "$msgs" "$wall" "$model" "$q" "$mech" \
        >> "$RESULTS"
}

# --- resolve the task battery -----------------------------------------------
resolve_tasks() {
    if [ -z "$TASKS_ARG" ]; then printf '%s' "$TASKS_DEFAULT"; return; fi
    if printf '%s' "$TASKS_ARG" | grep -qE '^[0-9]+$'; then
        local n="$TASKS_ARG" i=0 out=""
        for t in $TASKS_DEFAULT; do
            i=$((i + 1)); [ "$i" -le "$n" ] && out="$out $t"
        done
        printf '%s' "${out# }"
    else
        printf '%s' "$TASKS_ARG" | tr ',' ' '
    fi
}

# --- main --------------------------------------------------------------------
CLAUDE_VER="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
[ -n "$CLAUDE_VER" ] || CLAUDE_VER="dry-run"

printf '=== ab-comms-eval (dry-run): compose A/B fixture ===\n'
setup_variants
WEIGHTS_FILE="$ROOT/weights.json"

RESULTS="$ROOT/results.jsonl"; : > "$RESULTS"
VERDICT="$ROOT/verdict.json"
TASKS="$(resolve_tasks)"
printf 'tasks: %s   reps: %s (max %s)   threshold: %s\n' "$TASKS" "$REPS" "$REPS_MAX" "${THRESHOLD:-<none: INCONCLUSIVE>}"

# Run the paired matrix; extend one rep at a time to reps-max while any task
# straddles the bar (the stopping rule). With the default (no threshold) every
# task is INCONCLUSIVE immediately, so this settles in a single round.
reps_done=0; R="$REPS"
while :; do
    for ((rep = reps_done + 1; rep <= R; rep++)); do
        ti=0
        for task in $TASKS; do
            ti=$((ti + 1))
            for variant in without with; do
                run_cell "$task" "$ti" "$rep" "$variant"
            done
        done
    done
    reps_done="$R"
    verdict_out="$(compute_verdict "$RESULTS" "$VERDICT" "$reps_done")"
    any_straddle="$(printf '%s\n' "$verdict_out" | sed -n 's/^ANY_STRADDLE=//p' | tail -1)"
    if [ "${any_straddle:-0}" = 1 ] && [ "$R" -lt "$REPS_MAX" ]; then
        R=$((R + 1)); continue
    fi
    break
done

printf '\n=== verdict (reps=%s) ===\n' "$reps_done"
printf '%s\n' "$verdict_out" | grep -v '^ANY_STRADDLE='

# --- scaffolding wiring asserts (what --dry-run proves) ----------------------
printf '\n=== scaffolding checks ===\n'
[ -f "$ROOT/runtime/bots/ab-with/bot.conf" ] && r=yes || r=no
check "ab-with composed by generate" "$r"
[ -f "$ROOT/runtime/bots/ab-without/bot.conf" ] && r=yes || r=no
check "ab-without composed by generate" "$r"
if [ "${PROTO_PLACEHOLDER:-0}" = 1 ]; then
    grep -q 'PLACEHOLDER' "$ROOT/runtime/bots/ab-with/CLAUDE.md" 2>/dev/null && r=yes || r=no
    check "token-efficiency protocol lands in ab-with" "$r"
    grep -q 'PLACEHOLDER' "$ROOT/runtime/bots/ab-without/CLAUDE.md" 2>/dev/null && r=no || r=yes
    check "ab-without excludes the protocol (only difference)" "$r"
fi
grep -q '"variant":"with"' "$RESULTS" && r=yes || r=no
check "results.jsonl has WITH rows" "$r"
grep -q '"variant":"without"' "$RESULTS" && r=yes || r=no
check "results.jsonl has WITHOUT rows" "$r"
python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
assert d["per_task"], "no per_task"
t=d["per_task"][0]
assert "protocol_sensitive" in t and "cost_weighted_total" in t, "missing an axis"
assert "pins" in d and "weights" in d["pins"], "missing pins/weights"
assert not (d["overall"]=="PASS" and d["pins"]["threshold"] is None), "PASS without a ratified T"' \
    "$VERDICT" 2>/dev/null && r=yes || r=no
check "verdict.json valid: both axes, pins, no bare-PASS without T" "$r"
# zero model calls: no auth landed in either per-bot CLAUDE_CONFIG_DIR
find "$ROOT"/config-with "$ROOT"/config-without -name '.credentials.json' 2>/dev/null | grep -q . && r=no || r=yes
check "no CLAUDE_CONFIG_DIR auth touched (zero model calls)" "$r"

printf '\nROOT=%s\nRESULTS=%s\nVERDICT=%s\n' "$ROOT" "$RESULTS" "$VERDICT"
printf 'checks: %s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || die "scaffolding wiring checks failed"
printf 'ALL SCAFFOLDING CHECKS PASSED (verdict INCONCLUSIVE by construction until F2 ratifies T + the quality scorer).\n'
