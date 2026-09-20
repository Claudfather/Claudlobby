#!/bin/bash
# test_boot_work_state.sh — the #935 boot_work_state identity matrix.
#
# Authored in P1 (#934) so P2 opens against a stated contract rather than
# against nothing. Standalone bash, hermetic (files in a tmpdir, one function
# call, no tmux, no network, no services); collected by tests/test_sh_suites.py
# via its test_*.sh glob, so it is gated the moment it lands.
#
# WHAT THIS REPLACES, AND WHY. #934 step 7 specifies a clock-poison suite --
# btime inversion pairs and a danger-window `.spawn` < btime case. The epic
# resize of 2026-07-31 deleted the btime leg, the `_BWS_BTIME` seam and the
# whole clock half of the matrix (resize section 1), and dropped the clock
# replay scenario by name (section 3), because under the locked F1(b) identity
# mechanism no verdict leg reads a clock at all. Those tests would exercise
# code that is specified not to exist. What the resize puts in P1 instead is
# "the shrunken unit matrix (~8 identity cases incl. the skewed-mtime
# clock-freedom case and the pre-mechanism floor)" -- this file. #934's body
# was never updated to say so; see #1579.
#
# THE CONTRACT UNDER TEST (epic Architecture block; resize section 1), an
# ORDERED ladder -- the first matching leg decides:
#
#   boot_work_state <bot_dir> [grace_s]
#     .spawn missing, or its content empty  -> indeterminate
#     spawn age < grace OR uptime < grace   -> pre_grace
#     marker content == spawn content       -> worked
#     else (absent, empty, or foreign id)   -> never_worked
#
#   stdout: exactly one of those four words.  exit: 0 ALWAYS -- callers branch
#   on the word, and a predicate that can set -e-kill its caller repeats the
#   lib error-trap class. Seams: _BWS_NOW, _BWS_UPTIME. There is deliberately
#   NO _BWS_BTIME; adding one re-opens what the resize closed.
#
# SELF-ARMING. While boot_work_state does not exist this suite reports every
# case as PENDING and exits 0, because a hard-red suite in a shared CI lane
# blocks work unrelated to this epic. It needs NO edit in P2: defining the
# predicate arms all nine cases automatically, so the pending state cannot
# outlive the thing it is waiting for. The cases were proven to discriminate
# in P1 against a reference implementation and a deliberately wrong one --
# see the PR body for both runs.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/lib-common.sh
. "$REPO_ROOT/lib/lib-common.sh"
set +e  # lib-common arms set -e at source time; this suite scores, never aborts

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/bws-matrix.XXXXXX")"
trap 'rm -rf "$ROOT"' EXIT

pass=0; fail=0; pending=0
BID="1789900000.4242.31337"   # a boot-id shape; its wall component is a LABEL
OTHER="1789800000.1111.22222" # a DIFFERENT incarnation (the corpse signature)
GRACE=300

mkbot() { local d="$ROOT/$1"; mkdir -p "$d/data"; printf '%s' "$d"; }

# bws_case <case-name> <expected> <bot_dir> [now_offset_s] [uptime_s]
# now_offset_s ages the fixture WITHOUT touching any mtime -- that is what the
# _BWS_NOW seam is for, and it keeps the suite free of the non-portable
# backdating this repo has got wrong in both directions (GNU touch -d, BSD
# date -v). Default uptime is comfortably past grace so the uptime guard only
# fires in the case that asks for it.
bws_case() {
    local name="$1" want="$2" dir="$3" off="${4:-100000}" up="${5:-100000}"
    local got rc
    got=$(_BWS_NOW=$(( $(date +%s) + off )) _BWS_UPTIME="$up" \
          boot_work_state "$dir" "$GRACE" 2>/dev/null)
    rc=$?
    if [ "$got" = "$want" ] && [ "$rc" -eq 0 ]; then
        pass=$((pass + 1)); printf '  ok: %-58s -> %s\n' "$name" "$got"
    else
        fail=$((fail + 1))
        printf '  FAIL: %-56s -> got %s (rc %s), want %s (rc 0)\n' \
            "$name" "${got:-<empty>}" "$rc" "$want"
    fi
}

# --- fixtures ---------------------------------------------------------------
# 1. pre-mechanism floor: no .spawn at all.
C1=$(mkbot c1_no_spawn)

# 2. pre-mechanism floor: .spawn exists but carries no boot-id.
C2=$(mkbot c2_empty_spawn); : > "$C2/data/.spawn"

# 3. grace: a fresh incarnation with no marker is NOT yet a strand.
C3=$(mkbot c3_fresh); printf '%s' "$BID" > "$C3/data/.spawn"

# 4. the uptime guard -- the ONLY clock logic that survived the resize. A
#    forward clock step at boot inflates spawn AGE, so a bot could burn its
#    whole grace in its first second; raw uptime is step-immune.
C4=$(mkbot c4_young_host); printf '%s' "$BID" > "$C4/data/.spawn"

# 5. worked: the marker mirrors this incarnation's id.
C5=$(mkbot c5_worked)
printf '%s' "$BID" > "$C5/data/.spawn"; printf '%s' "$BID" > "$C5/data/.last-tool-call"

# 6. CLOCK-FREEDOM: identical ids, but the marker mtime is OLDER than .spawn --
#    the unclean-shutdown inversion that fake-hwclock produces on this host.
#    Under mtime comparison this reads as a strand; under identity it is
#    worked, and that inversion is the entire point of F1(b). Ordering is
#    built with sleep rather than a backdate so the suite stays portable.
C6=$(mkbot c6_skewed_worked)
printf '%s' "$BID" > "$C6/data/.last-tool-call"
sleep 1
printf '%s' "$BID" > "$C6/data/.spawn"

# 7. never_worked: marker absent, past grace. The S2 population.
C7=$(mkbot c7_no_marker); printf '%s' "$BID" > "$C7/data/.spawn"

# 8. never_worked: a marker left by a PRE-MECHANISM incarnation carries no id.
C8=$(mkbot c8_empty_marker)
printf '%s' "$BID" > "$C8/data/.spawn"; : > "$C8/data/.last-tool-call"

# 9. never_worked: a FOREIGN id -- the six corpses of 2026-07-30 and the twelve
#    of 2026-09-20. start-bot never clears the marker, so it survives the
#    reboot carrying the previous incarnation's id.
C9=$(mkbot c9_foreign_id)
printf '%s' "$BID" > "$C9/data/.spawn"; printf '%s' "$OTHER" > "$C9/data/.last-tool-call"

echo "=== #935 boot_work_state identity matrix (authored in P1/#934) ==="

if ! command -v boot_work_state >/dev/null 2>&1; then
    for c in \
        "1 .spawn absent                        -> indeterminate" \
        "2 .spawn present but empty             -> indeterminate" \
        "3 fresh spawn, no marker               -> pre_grace" \
        "4 old spawn but young host uptime      -> pre_grace" \
        "5 marker id == spawn id                -> worked" \
        "6 ids match, marker mtime OLDER        -> worked  (clock-freedom)" \
        "7 marker absent, past grace            -> never_worked" \
        "8 marker present but empty             -> never_worked" \
        "9 marker carries a FOREIGN id          -> never_worked"; do
        pending=$((pending + 1)); printf '  PENDING: %s\n' "$c"
    done
    echo ""
    echo "  boot_work_state is NOT DEFINED in lib/lib-common.sh."
    echo "  That is expected on main today: the predicate lands in #935 (P2)."
    echo "  These $pending cases are the contract it must satisfy. This suite"
    echo "  arms ITSELF -- defining the predicate runs all $pending for real,"
    echo "  with no edit to this file. Until then it gates nothing, and says so."
    echo ""
    echo "=== $pass passed, $fail failed, $pending pending ==="
    exit 0
fi

bws_case "1 .spawn absent                  -> indeterminate" indeterminate "$C1"
bws_case "2 .spawn present but empty       -> indeterminate" indeterminate "$C2"
bws_case "3 fresh spawn, no marker         -> pre_grace"     pre_grace     "$C3" 0
bws_case "4 old spawn, young host uptime   -> pre_grace"     pre_grace     "$C4" 100000 10
bws_case "5 marker id == spawn id          -> worked"        worked        "$C5"
bws_case "6 ids match, marker mtime OLDER  -> worked"        worked        "$C6"
bws_case "7 marker absent, past grace      -> never_worked"  never_worked  "$C7"
bws_case "8 marker present but empty       -> never_worked"  never_worked  "$C8"
bws_case "9 marker carries a FOREIGN id    -> never_worked"  never_worked  "$C9"

echo ""
echo "=== $pass passed, $fail failed, $pending pending ==="
[ "$fail" -eq 0 ]
