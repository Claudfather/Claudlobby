#!/usr/bin/env bash
# tests/test_boot_admission.sh — contract tests for lib/boot-admission.sh, the
# boot admission gate that REPLACES the #304 host-wide boot lock (#1573, PR B
# task 1).
#
# Standalone bash (not pytest-collected on its own); discovered by
# tests/test_sh_suites.py, which runs it hermetically (constructed env: PATH +
# LANG only, plus a throwaway HOME/TMPDIR -- see tests/conftest.py
# constructed_env). Runs under macOS /bin/bash (3.2) too.
#
# Hermetic by construction rather than by stubbing: the gate touches only
# $CLAUDLOBBY_ROOT/state/boot/<epoch>/ and <bot_dir>/data/, so this suite
# points CLAUDLOBBY_ROOT at a temp tree and pins the boot epoch through the
# CLAUDLOBBY_BOOT_EPOCH seam resolve_boot_epoch already ships.
# PLANE_EMIT_DISABLED=1 is the ruled harness exemption for the plane shim; the
# cells that assert on an event or a metric stub the emitting door inside the
# launcher fixture, so nothing here ever forks plane-emit.sh.
#
# `date` is faked on a prepended PATH (the tests/test_supervisor_adapter.sh
# fake-binary pattern) rather than as a shell function, because the cells that
# need a canned clock drive a CHILD process -- the launcher fixture -- and a
# function would not reach it. The fake passes straight through to /bin/date
# unless FAKE_DATE_SEQ or FAKE_DATE_NO_NS is set, so it is inert for every
# other cell, and every timing assertion in the suite itself calls /bin/date
# by absolute path so a canned clock can never reach the measurement.
#
# NO BACKDATING. `touch -t` and `date -v` / `date -d` are the pair this repo
# has got wrong in both directions (tests/test_boot_work_state.sh:61 records
# it), so every age boundary here is crossed by SHRINKING the window knob and
# sleeping real seconds rather than by rewriting an mtime. That costs a few
# seconds of suite time and buys a cell that means the same thing on both
# platforms.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LIB_DIR="$REPO_ROOT/lib"
export LIB_DIR

PASS=0; FAIL=0; TOTAL=0
assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS + 1)); else echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1)); fi
}
assert_contains() {
    TOTAL=$((TOTAL + 1)); local d="$1" needle="$2" haystack="$3"
    case "$haystack" in
        *"$needle"*) echo "  PASS: $d"; PASS=$((PASS + 1)) ;;
        *) echo "  FAIL: $d (expected to find '$needle' in: $haystack)"; FAIL=$((FAIL + 1)) ;;
    esac
}
assert_true() {
    TOTAL=$((TOTAL + 1)); local d="$1" v="$2"
    if [ "$v" = "true" ]; then echo "  PASS: $d"; PASS=$((PASS + 1)); else echo "  FAIL: $d (expected true, got '$v')"; FAIL=$((FAIL + 1)); fi
}
exists() { [ -e "$1" ] && echo true || echo false; }
count_lines() { printf '%s' "$(ls -1 "$1" 2>/dev/null | wc -l | tr -d " " || true)"; }

T="$(mktemp -d)"
PIDFILE="$T/pids"; : > "$PIDFILE"
cleanup() {
    local p
    while read -r p; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done < "$PIDFILE"
    rm -rf "$T" 2>/dev/null || true
}
trap 'cleanup' EXIT

mkdir -p "$T/bin"

# --- the fake `date` --------------------------------------------------------
# Inert unless FAKE_DATE_SEQ (a file of canned `+%s` readings, one per line,
# the last line repeating forever) or FAKE_DATE_NO_NS (macOS-shaped: `+%s%N`
# answers with a literal trailing N) is set.
cat > "$T/bin/date" <<'FAKEDATE'
#!/bin/bash
if [ -n "${FAKE_DATE_NO_NS:-}" ] && [ "${1:-}" = "+%s%N" ]; then
    printf '%sN\n' "$(/bin/date +%s)"
    exit 0
fi
if [ -z "${FAKE_DATE_SEQ:-}" ]; then exec /bin/date "$@"; fi
case "${1:-}" in
    +%s|+%s%N)
        _i="$(cat "$FAKE_DATE_SEQ.i" 2>/dev/null || printf 0)"
        _n=$((_i + 1))
        printf '%s' "$_n" > "$FAKE_DATE_SEQ.i"
        _v="$(sed -n "${_n}p" "$FAKE_DATE_SEQ")"
        [ -n "$_v" ] || _v="$(tail -1 "$FAKE_DATE_SEQ")"
        if [ "${1:-}" = "+%s%N" ]; then printf '%s000000000\n' "$_v"; else printf '%s\n' "$_v"; fi
        ;;
    *) exec /bin/date "$@" ;;
esac
FAKEDATE
chmod +x "$T/bin/date"
export PATH="$T/bin:$PATH"

export CLAUDLOBBY_ROOT="$T/root"
export CLAUDLOBBY_BOOT_EPOCH=1700000000
export PLANE_EMIT_DISABLED=1
export BOOT_ADMISSION_POLL_S=0.2
BOTS="$CLAUDLOBBY_ROOT/runtime/bots"
STATE="$CLAUDLOBBY_ROOT/state/boot/$CLAUDLOBBY_BOOT_EPOCH"
mkdir -p "$BOTS"

# shellcheck source=../lib/lib-common.sh
. "$LIB_DIR/lib-common.sh"

# A rename or a partial refactor that leaves sourcing itself clean but a door
# undefined must fail loudly here rather than silently run zero of the
# assertions below (the tests/test_supervisor_adapter.sh guard, same reason).
type boot_admission_acquire boot_admission_release _boot_admission_reap >/dev/null 2>&1 || {
    echo "FAIL: boot-admission doors missing after sourcing lib-common.sh"; exit 1; }

mkbot() {  # mkbot <name> <service> <priority> -- prints the bot dir
    local n="$1" svc="$2" p="$3" d="$BOTS/$1"
    mkdir -p "$d/data" "$d/logs"
    printf 'BOT_SERVICE=%s\nBOT_NAME=%s\nBOOT_PRIORITY=%s\n' "$svc" "$n" "$p" > "$d/bot.conf"
    printf '%s' "$d"
}

reset_state() { rm -rf "$CLAUDLOBBY_ROOT/state/boot"; mkdir -p "$STATE/tickets" "$STATE/slots"; }

# Both of these are called inside $( ), and a background child INHERITS the
# command substitution pipe as its stdout -- so without the redirect the
# substitution blocks until the child exits, which for live_pid means it hands
# back a pid that is already DEAD and costs 60s to do it. Measured: it turned
# every held-slot fixture into a reclaimable one.
live_pid() {  # spawn a long-lived process and print its pid
    sleep 60 >/dev/null 2>&1 &
    local p=$!
    printf '%s\n' "$p" >> "$PIDFILE"
    printf '%s' "$p"
}

dead_pid() {  # print the pid of a process that has already exited
    sleep 0 >/dev/null 2>&1 &
    local p=$!
    wait "$p" 2>/dev/null || true
    printf '%s' "$p"
}

hold_slot() {  # hold_slot <n> <unit> -- occupy a slot with a LIVE non-launcher holder
    local n="$1" u="$2"
    mkdir -p "$STATE/slots/$n"
    printf '%s\n' "$(live_pid)" > "$STATE/slots/$n/pid"
    printf '%s' "$u" > "$STATE/slots/$n/unit"
    printf '%s' "$(/bin/date +%s)" > "$STATE/slots/$n/granted_at"
}

wait_for() {  # wait_for <seconds> <command...>
    local limit="$1"; shift
    local i=0 max
    max=$((limit * 10))
    while [ "$i" -lt "$max" ]; do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 0.1
        i=$((i + 1))
    done
    return 1
}
# No `ls | grep -q`: grep -q exits on its first match and SIGPIPEs ls, and
# under pipefail (lib-common sets it) the pipeline then reports failure on the
# very case that matched -- so every poll would burn its whole budget and the
# cells below would pass or fail for reasons unrelated to the gate.
have_ticket() {
    local f
    for f in "$STATE/tickets"/*; do
        [ -f "$f" ] || continue
        case "${f##*/}" in *-"$1") return 0 ;; esac
    done
    return 1
}
file_present() { [ -e "$1" ]; }

# --- the launcher fixture ---------------------------------------------------
# A stand-in for Task 2s call site: it sources lib-common, owns LOG, captures
# the verdict from STDOUT (R17), then composes the EXIT trap the call site will
# compose -- release FIRST, then _lc_cleanup, which is the whole of R11.
cat > "$T/launch.sh" <<'LAUNCH'
#!/bin/bash
set -euo pipefail
. "$LIB_DIR/lib-common.sh"
BOT_DIR="$1"; HOLD_S="${2:-0}"; OUT="$3"
# start-bot.sh calls this immediately before the gate, and BOOT_PRIORITY --
# the manager-first half of the whole mechanism -- arrives ONLY this way. A
# fixture that skips it reads every bot as a worker and the priority cells
# then pass or fail on arrival order.
load_bot_conf "$BOT_DIR"
LOG="$BOT_DIR/logs/startup.log"
setup_log_dir "$LOG"
if [ -n "${EVENT_LOG:-}" ]; then
    emit_fleet_event() { printf '%s\t%s\n' "${1:-}" "${3:-}" >> "$EVENT_LOG"; return 0; }
fi
if [ -n "${METRIC_LOG:-}" ]; then
    plane_emit_events() { cat >> "$METRIC_LOG"; return 0; }
fi
printf '%s\n' "$_LC_TMPDIR" > "$OUT.tmpdir"
_v="$(boot_admission_acquire "$BOT_DIR")"
printf '%s %s %s\n' "$(/bin/date +%s)" "$(basename "$BOT_DIR")" "$_v" >> "$GRANT_LOG"
printf '%s\n' "$_v" > "$OUT"
if [ -n "${NO_LC_CLEANUP:-}" ]; then
    trap 'boot_admission_release "$BOT_DIR"' EXIT
else
    trap 'boot_admission_release "$BOT_DIR"; _lc_cleanup' EXIT
fi
sleep "$HOLD_S"
LAUNCH
chmod +x "$T/launch.sh"

GRANT_LOG="$T/grants.log"; export GRANT_LOG
: > "$GRANT_LOG"

# launch <bot_dir> <hold_s> <out> [VAR=VAL ...] -- sets LAUNCH_PID.
# Started in THIS shell, never inside a command substitution: a background job
# forked by a subshell is not a child of the main shell and `wait` on it fails.
LAUNCH_PID=""
launch() {
    local d="$1" h="$2" o="$3"; shift 3
    env "$@" bash "$T/launch.sh" "$d" "$h" "$o" &
    LAUNCH_PID=$!
    printf '%s\n' "$LAUNCH_PID" >> "$PIDFILE"
}

echo "=== (g3) a %N-less date still yields a 19-digit sortable stamp (R2) ==="

reset_state
S_REAL="$(_boot_admission_stamp)"
assert_eq "real date: stamp is 19 digits" "19" "${#S_REAL}"
case "$S_REAL" in ''|*[!0-9]*) assert_eq "real date: stamp is all digits" "yes" "no" ;; *) assert_eq "real date: stamp is all digits" "yes" "yes" ;; esac
export FAKE_DATE_NO_NS=1
S_FAKE="$(_boot_admission_stamp)"
unset FAKE_DATE_NO_NS
assert_eq "%N-less date: stamp is still 19 digits" "19" "${#S_FAKE}"
case "$S_FAKE" in ''|*[!0-9]*) assert_eq "%N-less date: stamp is all digits (the N was rejected)" "yes" "no" ;; *) assert_eq "%N-less date: stamp is all digits (the N was rejected)" "yes" "yes" ;; esac
# R2s load-bearing half: the fallback sorts WITH real ns stamps rather than
# before every one of them. Same second, ns=0, so it sorts first -- and a real
# ns stamp taken in that same second sorts after it, not a whole epoch away.
assert_eq "fallback sorts WITH ns stamps (same second, ns 0 first)" "$S_FAKE" \
    "$(printf '%s\n%s\n' "$S_REAL" "$S_FAKE" | LC_ALL=C sort | head -1 || true)"
assert_eq "fallback and a real stamp share the epoch-second prefix" \
    "${S_REAL:0:10}" "${S_FAKE:0:10}"

echo "=== the derived slot formula (F14: the bash half of the two-language pin) ==="

# clamp((ncpu or 1) // 4, 1, 4) -- the same clamp claudlobby.boot.derive_slots
# applies. tests/test_boot_policy.py runs BOTH implementations over this same
# cpu list and asserts they agree; this cell pins the bash side on its own so a
# drift is named here too, in the suite that owns the gate.
for _pair in "1:1" "3:1" "4:1" "8:2" "12:3" "16:4" "64:4" "0:1" ":1" "abc:1"; do
    _cpu="${_pair%%:*}"; _want="${_pair##*:}"
    assert_eq "derive_slots($_cpu) = $_want" "$_want" "$(_boot_admission_derive_slots "$_cpu")"
done

echo "=== the effective cap is dispersed per ticket (R9) ==="

# cap + arrival_rank x (ready_timeout_s / slots). R9s recorded numbers: at
# cap 1200, ready_timeout 200, 1 slot, ranks 0/1/20 give 1200/1400/5200.
assert_eq "rank 0: the flat cap" "1200" "$(_boot_admission_effective_cap 1200 0 200 1)"
assert_eq "rank 1: one dispersion step" "1400" "$(_boot_admission_effective_cap 1200 1 200 1)"
assert_eq "rank 20: twenty dispersion steps" "5200" "$(_boot_admission_effective_cap 1200 20 200 1)"
assert_eq "4 slots: dispersion is ready_timeout/slots" "1250" "$(_boot_admission_effective_cap 1200 1 200 4)"
# A cap of 0 means NEVER WAIT and must stay 0 at every rank -- dispersing it
# would turn the one setting that promises not to wait into the longest wait.
assert_eq "cap 0 stays 0 at rank 0" "0" "$(_boot_admission_effective_cap 0 0 200 1)"
assert_eq "cap 0 stays 0 at rank 20" "0" "$(_boot_admission_effective_cap 0 20 200 1)"

echo "=== (d2a) the clock-step fold, PR As arithmetic verbatim (R8) ==="

# _step_s = max(cap, 60); a tick whose delta is negative or exceeds _step_s
# shifts the origin instead of counting.
assert_eq "normal 1s tick counts" "1000" "$(_boot_admission_fold 1001 1000 1000 60)"
assert_eq "+3600s step shifts the origin" "4600" "$(_boot_admission_fold 4600 1000 1000 60)"
assert_eq "-4000s step shifts the origin" "-3000" "$(_boot_admission_fold 600 4600 1000 60)"
assert_eq "a tick exactly at the threshold still counts" "1000" "$(_boot_admission_fold 1060 1000 1000 60)"

echo "=== (g) an unwritable state dir yields unavailable without stalling ==="

reset_state
BOT_G="$(mkbot g-bot svc-g 1)"
UNWRITABLE="$T/unwritable"
mkdir -p "$UNWRITABLE"
chmod 500 "$UNWRITABLE"
_t0="$(/bin/date +%s)"
OUT_G="$(CLAUDLOBBY_ROOT="$UNWRITABLE" BOOT_ADMISSION_WAIT_MAX_S=600 EVENT_LOG="$T/ev-g.log" \
    bash -c '. "$LIB_DIR/lib-common.sh"
             emit_fleet_event() { printf "%s\t%s\n" "${1:-}" "${3:-}" >> "$EVENT_LOG"; return 0; }
             LOG="$1/logs/startup.log"; setup_log_dir "$LOG"
             boot_admission_acquire "$1"' _ "$BOT_G")"
_t1="$(/bin/date +%s)"
chmod 700 "$UNWRITABLE"
assert_eq "unwritable state dir: verdict is unavailable" "unavailable" "$OUT_G"
assert_true "unwritable state dir: returns without stalling (<5s)" "$([ "$((_t1 - _t0))" -lt 5 ] && echo true || echo false)"
assert_contains "unwritable state dir: boot_admission_unavailable emitted" "boot_admission_unavailable" "$(cat "$T/ev-g.log" 2>/dev/null || true)"
assert_contains "unwritable state dir: the reason names the state dir" "state dir unwritable" "$(cat "$T/ev-g.log" 2>/dev/null || true)"

# R16: nine harness bot.confs and every pre-generate fleet carry BOT_SERVICE="",
# so an unresolvable unit name is a real population, not a hypothetical. It must
# proceed ungated rather than share a ticket path with another bot.
reset_state
BOT_NOSVC="$BOTS/nosvc"
mkdir -p "$BOT_NOSVC/data" "$BOT_NOSVC/logs"
printf 'BOT_SERVICE=\nBOT_NAME=\n' > "$BOT_NOSVC/bot.conf"
OUT_NS="$(EVENT_LOG="$T/ev-ns.log" bash -c '. "$LIB_DIR/lib-common.sh"
             emit_fleet_event() { printf "%s\t%s\n" "${1:-}" "${3:-}" >> "$EVENT_LOG"; return 0; }
             LOG="$1/logs/startup.log"; setup_log_dir "$LOG"
             boot_admission_acquire "$1"' _ "$BOT_NOSVC")"
assert_eq "empty unit name: verdict is unavailable" "unavailable" "$OUT_NS"
assert_contains "empty unit name: the reason says so" "unit name empty" "$(cat "$T/ev-ns.log" 2>/dev/null || true)"
assert_eq "empty unit name: no ticket was minted" "0" "$(count_lines "$STATE/tickets")"

echo "=== an un-regenerated bot.conf (NO BOOT_* key at all) still starts ==="

# The third state beside empty, non-numeric and zero -- and the one "no
# operator step" actually depends on. Every BOOT_* read is ${KEY:-default}, so
# a fleet that has not regenerated degrades to a one-slot gate with todays
# timings rather than aborting under set -u.
reset_state
BOT_BARE="$BOTS/bare"
mkdir -p "$BOT_BARE/data" "$BOT_BARE/logs"
printf 'BOT_SERVICE=svc-bare\nBOT_NAME=bare\n' > "$BOT_BARE/bot.conf"
OUT_BARE="$(bash -c 'unset BOOT_ADMISSION_SLOTS BOOT_ADMISSION_WAIT_MAX_S BOOT_PRIORITY BOOT_HOLD_CEILING_S RC_READY_TIMEOUT_S
             . "$LIB_DIR/lib-common.sh"
             LOG="$1/logs/startup.log"; setup_log_dir "$LOG"
             boot_admission_acquire "$1"' _ "$BOT_BARE")"
assert_eq "no BOOT_* key anywhere: still granted" "granted:0" "$OUT_BARE"
assert_contains "no BOOT_* key anywhere: the grant is logged" "ADMISSION_GRANTED slot=0" "$(cat "$BOT_BARE/logs/startup.log")"

echo "=== (g4) the zero and one cases ==="

# slots >= bots_on_host: granted on the FIRST loop, no wait line.
reset_state
BOT_Z="$(mkbot z-bot svc-z 1)"
OUT_Z="$(BOOT_ADMISSION_SLOTS=4 BOOT_ADMISSION_WAIT_MAX_S=600 bash -c '. "$LIB_DIR/lib-common.sh"
             LOG="$1/logs/startup.log"; setup_log_dir "$LOG"
             boot_admission_acquire "$1"' _ "$BOT_Z")"
assert_eq "slots >= bots: granted on the first loop" "granted:0" "$OUT_Z"
# The FIRST-LOOP proof is the absent wait line, not the printed second count:
# the grant line records real wall time, and the handful of forks between
# `started` and the grant can cross a second boundary on a loaded host. The
# recorded wait is asserted to be at most one second rather than exactly zero,
# because pinning it to 0 would make the cell fail on load rather than on a
# regression.
_WZ="$(sed -n 's/.*ADMISSION_GRANTED slot=0 after \([0-9][0-9]*\)s.*/\1/p' "$BOT_Z/logs/startup.log" | head -1 || true)"
assert_true "slots >= bots: the recorded wait is <=1s" "$([ -n "$_WZ" ] && [ "$_WZ" -le 1 ] && echo true || echo false)"
assert_eq "slots >= bots: no ADMISSION_WAIT line (granted on the first loop)" "0" "$(grep -c 'ADMISSION_WAIT' "$BOT_Z/logs/startup.log" || true)"

# admission_wait_max_s: 0 means NEVER WAIT -- proceed immediately, and emit
# boot_admission_timeout exactly once.
reset_state
BOT_Z0="$(mkbot z0-bot svc-z0 1)"
hold_slot 0 svc-other
_t0="$(/bin/date +%s)"
OUT_Z0="$(BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=0 EVENT_LOG="$T/ev-z0.log" bash -c '. "$LIB_DIR/lib-common.sh"
             emit_fleet_event() { printf "%s\t%s\n" "${1:-}" "${3:-}" >> "$EVENT_LOG"; return 0; }
             LOG="$1/logs/startup.log"; setup_log_dir "$LOG"
             boot_admission_acquire "$1"' _ "$BOT_Z0")"
_t1="$(/bin/date +%s)"
assert_eq "cap 0: verdict is timeout" "timeout" "$OUT_Z0"
assert_true "cap 0: proceeds immediately (<3s)" "$([ "$((_t1 - _t0))" -lt 3 ] && echo true || echo false)"
assert_eq "cap 0: boot_admission_timeout emitted exactly once" "1" "$(grep -c 'boot_admission_timeout' "$T/ev-z0.log" || true)"
assert_contains "cap 0: the event carries the waited_s shape" '"waited_s":0' "$(cat "$T/ev-z0.log")"
assert_contains "cap 0: ADMISSION_TIMEOUT logged" "ADMISSION_TIMEOUT" "$(cat "$BOT_Z0/logs/startup.log")"

echo "=== (g2) a prior-epoch tree is stale UNCONDITIONALLY, with no pid read (R14) ==="

reset_state
OLD="$CLAUDLOBBY_ROOT/state/boot/1699999999"
mkdir -p "$OLD/slots/0" "$OLD/tickets"
# Every signal that would make this slot HELD inside the current epoch: a live
# pid and an mtime one second old. It must go anyway -- a reboot makes a pid
# meaningless, and reading one here is the confident wrong answer.
printf '%s\n' "$(live_pid)" > "$OLD/slots/0/pid"
printf '%s' "svc-old" > "$OLD/slots/0/unit"
printf 'pid=%s\n' "$(live_pid)" > "$OLD/tickets/0-1699999999000000000-svc-old"
# A plugin stamp and its lock sit in the SAME state/boot directory and are
# written under with_lock by plugin_ensure -- deleting them from an unlocked
# 2s poll re-arms the amplifier PR A removed.
: > "$CLAUDLOBBY_ROOT/state/boot/plugins-updated.1700000000.claudna_Claudfather"
mkdir -p "$CLAUDLOBBY_ROOT/state/boot/plugins.lock"
_boot_admission_reap "$BOTS"
assert_eq "prior-epoch tree removed wholesale" "false" "$(exists "$OLD")"
assert_eq "current-epoch tree untouched" "true" "$(exists "$STATE/slots")"
assert_eq "plugin stamp NOT touched (it belongs to plugin_ensure)" "true" "$(exists "$CLAUDLOBBY_ROOT/state/boot/plugins-updated.1700000000.claudna_Claudfather")"
assert_eq "plugin lock NOT touched" "true" "$(exists "$CLAUDLOBBY_ROOT/state/boot/plugins.lock")"

echo "=== (c) a slot whose recorded pid is DEAD is reclaimed by the next waiter (R6a) ==="

reset_state
BOT_C="$(mkbot c-bot svc-c 1)"
mkdir -p "$STATE/slots/0"
printf '%s\n' "$(dead_pid)" > "$STATE/slots/0/pid"
printf '%s' "svc-gone" > "$STATE/slots/0/unit"
OUT_C="$(BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=20 bash -c '. "$LIB_DIR/lib-common.sh"
             LOG="$1/logs/startup.log"; setup_log_dir "$LOG"
             boot_admission_acquire "$1"' _ "$BOT_C")"
assert_eq "dead holder: the next waiter is granted the reclaimed slot" "granted:0" "$OUT_C"
assert_eq "dead holder: the slot now names the new holder" "svc-c" "$(cat "$STATE/slots/0/unit" 2>/dev/null || true)"

echo "=== (c2) a pid-less slot is HELD inside the claim grace, reapable past it (R4) ==="

reset_state
mkdir -p "$STATE/slots/0"   # the mkdir landed, the pid is not written yet
export BOOT_ADMISSION_CLAIM_GRACE_S=30
_boot_admission_reap "$BOTS"
assert_eq "pid-less slot inside the claim grace: HELD" "true" "$(exists "$STATE/slots/0")"
sleep 2
export BOOT_ADMISSION_CLAIM_GRACE_S=1
_boot_admission_reap "$BOTS"
unset BOOT_ADMISSION_CLAIM_GRACE_S
assert_eq "pid-less slot past the claim grace: reaped" "false" "$(exists "$STATE/slots/0")"

echo "=== (c3) two concurrent reclaimers, exactly ONE winner (R5) ==="

reset_state
mkdir -p "$STATE/slots/0"
printf '%s\n' "$(dead_pid)" > "$STATE/slots/0/pid"
: > "$T/reclaim.out"
for _i in 1 2 3 4 5 6; do
    ( if _boot_admission_reclaim "$STATE/slots/0"; then printf 'win\n' >> "$T/reclaim.out"; else printf 'lose\n' >> "$T/reclaim.out"; fi ) &
done
wait
assert_eq "six concurrent reclaimers: exactly one winner" "1" "$(grep -c '^win$' "$T/reclaim.out" || true)"
assert_eq "six concurrent reclaimers: five losers" "5" "$(grep -c '^lose$' "$T/reclaim.out" || true)"
assert_eq "the slot is gone" "false" "$(exists "$STATE/slots/0")"
assert_eq "no .reap scratch left behind" "0" "$(ls -a "$STATE/slots" 2>/dev/null | grep -c '^\.reap' || true)"

echo "=== (c4) a REUSED pid: kill -0 alone is not enough (R6b) ==="

reset_state
REUSED="$(live_pid)"
mkdir -p "$STATE/slots/0"
printf '%s\n' "$REUSED" > "$STATE/slots/0/pid"
printf '%s' "svc-reused" > "$STATE/slots/0/unit"
# Inside the hold ceiling the LIVE pid holds the slot -- the positive control,
# without which "reaped" below would not distinguish the liveness PAIR from a
# reaper that simply deletes everything it finds.
export BOOT_HOLD_CEILING_S=30
_boot_admission_reap "$BOTS"
assert_eq "live pid inside the hold ceiling: slot HELD" "true" "$(exists "$STATE/slots/0")"
sleep 2
export BOOT_HOLD_CEILING_S=1
_boot_admission_reap "$BOTS"
unset BOOT_HOLD_CEILING_S
assert_eq "live pid PAST the hold ceiling: slot reclaimed anyway" "false" "$(exists "$STATE/slots/0")"
assert_true "the reused pid is still alive (kill -0 alone would have held it)" "$(kill -0 "$REUSED" 2>/dev/null && echo true || echo false)"

echo "=== (f2) the reaper removes a marker whose recorded launcher pid is dead (R7) ==="

reset_state
BOT_M1="$(mkbot m1 svc-m1 1)"
BOT_M2="$(mkbot m2 svc-m2 1)"
printf 'pid=%s\nepoch=%s\n' "$(dead_pid)" "$CLAUDLOBBY_BOOT_EPOCH" > "$BOT_M1/data/.boot-queued"
printf 'pid=%s\nepoch=%s\n' "$(live_pid)" "$CLAUDLOBBY_BOOT_EPOCH" > "$BOT_M2/data/.boot-queued"
_boot_admission_reap "$BOTS"
assert_eq "dead launcher: its marker is removed" "false" "$(exists "$BOT_M1/data/.boot-queued")"
assert_eq "live launcher: its marker survives" "true" "$(exists "$BOT_M2/data/.boot-queued")"
# The epoch beside the pid is what tells a boot-queue from a restart-queue --
# the .spawn lesson, recorded verbatim in lib/boot-capture.sh:19-25.
printf 'pid=%s\nepoch=%s\n' "$(live_pid)" "1699999999" > "$BOT_M2/data/.boot-queued"
_boot_admission_reap "$BOTS"
assert_eq "prior-epoch marker: removed although its pid is live" "false" "$(exists "$BOT_M2/data/.boot-queued")"
# No bots dir means CANNOT LOOK, which is not the same answer as NOTHING TO
# REAP -- the reaper must delete nothing rather than guess (source_state.py's
# rule, #1146s direction).
printf 'pid=%s\nepoch=%s\n' "$(dead_pid)" "$CLAUDLOBBY_BOOT_EPOCH" > "$BOT_M1/data/.boot-queued"
_boot_admission_reap
assert_eq "no bots dir: the reaper touches no marker at all" "true" "$(exists "$BOT_M1/data/.boot-queued")"
_boot_admission_reap "$T/no-such-bots-dir"
assert_eq "unreadable bots dir: still touches no marker" "true" "$(exists "$BOT_M1/data/.boot-queued")"

echo "=== (f) the marker lives from acquire to RELEASE (R7b) ==="

reset_state
BOT_F="$(mkbot f-bot svc-f 1)"
OUT_F="$(BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=20 bash -c '. "$LIB_DIR/lib-common.sh"
             LOG="$1/logs/startup.log"; setup_log_dir "$LOG"
             boot_admission_acquire "$1"' _ "$BOT_F")"
assert_eq "granted" "granted:0" "$OUT_F"
assert_eq "the marker exists while the bot is granted-but-not-ready" "true" "$(exists "$BOT_F/data/.boot-queued")"
assert_contains "the marker carries the launcher pid" "pid=" "$(cat "$BOT_F/data/.boot-queued")"
assert_contains "the marker carries the boot epoch beside the pid" "epoch=$CLAUDLOBBY_BOOT_EPOCH" "$(cat "$BOT_F/data/.boot-queued")"
assert_eq "the ticket is still in the queue while the slot is held" "1" "$(count_lines "$STATE/tickets")"
bash -c '. "$LIB_DIR/lib-common.sh"; LOG="$1/logs/startup.log"; boot_admission_release "$1"' _ "$BOT_F"
assert_eq "release removes the marker" "false" "$(exists "$BOT_F/data/.boot-queued")"

# The normal path is RECORDED, or the slot formula, the cap and the hold
# ceiling stay unfalsifiable from the very reboot meant to validate them.
# Armed deliberately for this cell only (the suite runs with the plane shim
# disabled) and pointed at a stub, so no plane-emit.sh is ever forked.
reset_state
BOT_MET="$(mkbot met-bot svc-met 1)"
METRIC_LOG="$T/metrics.log"; : > "$METRIC_LOG"
PLANE_EMIT_DISABLED= FLEET_NAME=test-fleet BOT_NAME=met-bot METRIC_LOG="$METRIC_LOG" \
BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=20 bash -c '. "$LIB_DIR/lib-common.sh"
             plane_emit_events() { cat >> "$METRIC_LOG"; return 0; }
             LOG="$1/logs/startup.log"; setup_log_dir "$LOG"
             boot_admission_acquire "$1" >/dev/null
             boot_admission_release "$1"' _ "$BOT_MET"
wait_for 5 grep -q 'boot.admission_hold_s' "$METRIC_LOG" || true
assert_contains "the grant is recorded as boot.admission_wait_s" "boot.admission_wait_s" "$(cat "$METRIC_LOG")"
assert_contains "the hold is recorded as boot.admission_hold_s" "boot.admission_hold_s" "$(cat "$METRIC_LOG")"
assert_contains "the sample subject is the INSTANCE alias" '"subject":"bot:test-fleet/met-bot"' "$(cat "$METRIC_LOG")"
assert_contains "the sample is a metric_sample emitted by start-bot" '"emitter":"start-bot"' "$(cat "$METRIC_LOG")"

# The recording must be NON-BLOCKING, and the shape that bites is specific:
# the caller captures the verdict in a COMMAND SUBSTITUTION, so a background
# child that inherits the substitution pipe keeps it open and the caller blocks
# until that child exits -- every boot on the host, wedged behind a plane emit.
# Driven through a deliberately SLOW stub, and timed by the real clock.
reset_state
BOT_NB="$(mkbot nb-bot svc-nb 1)"
_t0="$(/bin/date +%s)"
OUT_NB="$(PLANE_EMIT_DISABLED= FLEET_NAME=test-fleet BOT_NAME=nb-bot \
  BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=20 bash -c '. "$LIB_DIR/lib-common.sh"
             plane_emit_events() { sleep 20; return 0; }
             LOG="$1/logs/startup.log"; setup_log_dir "$LOG"
             boot_admission_acquire "$1"' _ "$BOT_NB")"
_t1="$(/bin/date +%s)"
assert_eq "a slow plane emit still returns the verdict" "granted:0" "$OUT_NB"
assert_true "the command substitution is NOT held open by the background emit (<5s)" \
    "$([ "$((_t1 - _t0))" -lt 5 ] && echo true || echo false)"

echo "=== (e) release removes slot + ticket + marker; the trap does it on exit (R11) ==="

reset_state
BOT_E="$(mkbot e-bot svc-e 1)"
OUT_E="$T/e.verdict"
launch "$BOT_E" 3 "$OUT_E" BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=20
P_E="$LAUNCH_PID"
wait_for 10 file_present "$OUT_E" || true
assert_eq "launcher granted" "granted:0" "$(cat "$OUT_E" 2>/dev/null || true)"
assert_eq "mid-hold: slot present" "true" "$(exists "$STATE/slots/0")"
assert_eq "mid-hold: ticket present" "1" "$(count_lines "$STATE/tickets")"
assert_eq "mid-hold: marker present" "true" "$(exists "$BOT_E/data/.boot-queued")"
LCT_E="$(cat "$OUT_E.tmpdir")"
assert_eq "mid-hold: _LC_TMPDIR present" "true" "$(exists "$LCT_E")"
wait "$P_E" 2>/dev/null || true
assert_eq "after exit: slot removed" "false" "$(exists "$STATE/slots/0")"
assert_eq "after exit: ticket removed" "0" "$(count_lines "$STATE/tickets")"
assert_eq "after exit: marker removed" "false" "$(exists "$BOT_E/data/.boot-queued")"
assert_eq "after exit: _LC_TMPDIR gone (the composed trap named _lc_cleanup)" "false" "$(exists "$LCT_E")"
assert_contains "ADMISSION_RELEASED logged (the token, never a bare RELEASED)" "ADMISSION_RELEASED" "$(cat "$BOT_E/logs/startup.log")"
set +e
bash -c '. "$LIB_DIR/lib-common.sh"; LOG="$1/logs/startup.log"; boot_admission_release "$1"' _ "$BOT_E"
_RC_IDEM=$?
set -e
assert_eq "release is idempotent (second call rc 0, nothing left to remove)" "0" "$_RC_IDEM"

# The R11 POSITIVE CONTROL, run rather than claimed: the same launcher with
# _lc_cleanup DROPPED from the composed trap leaks its mktemp -d. Without this
# the assertion above could pass against a trap that did nothing at all.
reset_state
BOT_E2="$(mkbot e2-bot svc-e2 1)"
OUT_E2="$T/e2.verdict"
launch "$BOT_E2" 1 "$OUT_E2" NO_LC_CLEANUP=1 BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=20
wait "$LAUNCH_PID" 2>/dev/null || true
LCT_E2="$(cat "$OUT_E2.tmpdir")"
assert_eq "mutant control: dropping _lc_cleanup LEAKS _LC_TMPDIR" "true" "$(exists "$LCT_E2")"
assert_eq "mutant control: the release half still ran" "false" "$(exists "$BOT_E2/data/.boot-queued")"
rm -rf "$LCT_E2"

echo "=== (a) priority, deterministically: a manager ahead makes a worker wait ==="

reset_state
BOT_A="$(mkbot a-worker svc-a-worker 1)"
# A priority-0 manager ticket, minted BEFORE the worker launcher starts, with a
# LIVE pid and a fresh mtime so the reaper keeps it. No race to lose: the
# precondition is on disk before anything else runs.
printf 'pid=%s\n' "$(live_pid)" > "$STATE/tickets/0-1700000000000000000-svc-a-manager"
OUT_A="$T/a.verdict"
launch "$BOT_A" 0 "$OUT_A" BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=20
P_A="$LAUNCH_PID"
sleep 2
assert_eq "worker with a manager ahead of it: still waiting, not granted" "false" "$(exists "$OUT_A")"
assert_eq "worker with a manager ahead of it: took no slot" "false" "$(exists "$STATE/slots/0")"
assert_contains "the wait is logged with the queue depth and the priority" "ADMISSION_WAIT queue=2 slots=0/1 priority=1" "$(cat "$BOT_A/logs/startup.log")"
rm -f "$STATE/tickets/0-1700000000000000000-svc-a-manager"
wait_for 10 file_present "$OUT_A" || true
assert_eq "once the manager ticket clears, the worker is granted" "granted:0" "$(cat "$OUT_A" 2>/dev/null || true)"
wait "$P_A" 2>/dev/null || true

echo "=== (a2) the granted ORDER, read from the grant log lines ==="

reset_state
: > "$GRANT_LOG"
BOT_W="$(mkbot a2-worker svc-a2-worker 1)"
BOT_MGR="$(mkbot a2-lead svc-a2-lead 0)"
hold_slot 0 svc-blocking
# The worker arrives FIRST (earlier arrival stamp); the manager arrives second
# and must still be granted first. Both are parked in the wait loop before the
# slot is freed, so the grant order is decided by the ticket sort and nothing
# else. Read from the GRANT LOG lines, never from a sampled file.
OUT_W="$T/a2w.verdict"; OUT_MGR="$T/a2m.verdict"
launch "$BOT_W" 0 "$OUT_W" BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=25
P_W="$LAUNCH_PID"
wait_for 10 have_ticket svc-a2-worker || true
launch "$BOT_MGR" 0 "$OUT_MGR" BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=25
P_MGR="$LAUNCH_PID"
wait_for 10 have_ticket svc-a2-lead || true
assert_eq "both are queued, neither granted" "false" "$(exists "$OUT_W")"
rm -rf "$STATE/slots/0"
wait "$P_MGR" 2>/dev/null || true
wait "$P_W" 2>/dev/null || true
assert_eq "manager granted" "granted:0" "$(cat "$OUT_MGR" 2>/dev/null || true)"
assert_eq "worker granted" "granted:0" "$(cat "$OUT_W" 2>/dev/null || true)"
assert_eq "the FIRST grant line names the manager, though it arrived second" "a2-lead" "$(awk '{print $2}' "$GRANT_LOG" | head -1 || true)"
assert_eq "the SECOND grant line names the worker" "a2-worker" "$(awk '{print $2}' "$GRANT_LOG" | sed -n 2p || true)"

echo "=== (b) 2 slots, three waiters: exactly two hold at once ==="

reset_state
: > "$GRANT_LOG"
B1="$(mkbot b1 svc-b1 1)"; B2="$(mkbot b2 svc-b2 1)"; B3="$(mkbot b3 svc-b3 1)"
O1="$T/b1.v"; O2="$T/b2.v"; O3="$T/b3.v"
launch "$B1" 4 "$O1" BOOT_ADMISSION_SLOTS=2 BOOT_ADMISSION_WAIT_MAX_S=25
PB1="$LAUNCH_PID"; wait_for 10 have_ticket svc-b1 || true
launch "$B2" 4 "$O2" BOOT_ADMISSION_SLOTS=2 BOOT_ADMISSION_WAIT_MAX_S=25
PB2="$LAUNCH_PID"; wait_for 10 have_ticket svc-b2 || true
launch "$B3" 1 "$O3" BOOT_ADMISSION_SLOTS=2 BOOT_ADMISSION_WAIT_MAX_S=25
PB3="$LAUNCH_PID"; wait_for 10 have_ticket svc-b3 || true
wait_for 10 file_present "$O2" || true
sleep 1
assert_eq "exactly two slots are held at once" "2" "$(ls -d "$STATE"/slots/[0-9]* 2>/dev/null | wc -l | tr -d ' ' || true)"
assert_eq "the third waiter is not granted yet" "false" "$(exists "$O3")"
wait "$PB1" 2>/dev/null || true
wait "$PB2" 2>/dev/null || true
wait_for 10 file_present "$O3" || true
wait "$PB3" 2>/dev/null || true
_V3="$(cat "$O3" 2>/dev/null || true)"
assert_eq "the third is granted after a release" "granted" "${_V3%%:*}"
assert_eq "all three eventually granted" "3" "$(grep -c 'granted:' "$GRANT_LOG" || true)"

echo "=== (d) a waiter past its cap times out, removing its OWN ticket first (R10) ==="

reset_state
: > "$GRANT_LOG"
BOT_D="$(mkbot d-head svc-d-head 0)"
BOT_D2="$(mkbot d-next svc-d-next 1)"
hold_slot 0 svc-blocking
OUT_D="$T/d.verdict"
launch "$BOT_D" 0 "$OUT_D" BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=1 RC_READY_TIMEOUT_S=1 EVENT_LOG="$T/ev-d.log"
wait "$LAUNCH_PID" 2>/dev/null || true
assert_eq "past the cap: verdict is timeout" "timeout" "$(cat "$OUT_D" 2>/dev/null || true)"
assert_eq "the timing-out waiter removed its OWN ticket" "false" "$(have_ticket svc-d-head && echo true || echo false)"
assert_contains "boot_admission_timeout emitted" "boot_admission_timeout" "$(cat "$T/ev-d.log" 2>/dev/null || true)"
assert_contains "the event carries the rank" '"rank":' "$(cat "$T/ev-d.log" 2>/dev/null || true)"
assert_contains "ADMISSION_TIMEOUT logged" "ADMISSION_TIMEOUT" "$(cat "$BOT_D/logs/startup.log")"
# A LOWER-priority waiter is head of queue immediately afterwards. Had the
# head's p0 ticket survived its own timeout, this p1 waiter would have queued
# behind it for its whole cap.
rm -rf "$STATE/slots/0"
OUT_D2="$T/d2.verdict"
launch "$BOT_D2" 0 "$OUT_D2" BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=10
wait "$LAUNCH_PID" 2>/dev/null || true
assert_eq "the next waiter is head of queue immediately afterwards" "granted:0" "$(cat "$OUT_D2" 2>/dev/null || true)"

echo "=== (d2) a clock step mid-wait: neither give up early nor jump the queue (R8) ==="

reset_state
BOT_CS="$(mkbot cs-bot svc-cs 1)"
printf 'pid=%s\n' "$(live_pid)" > "$STATE/tickets/0-1700000000000000000-svc-cs-blocker"
SEQ="$T/dseq"
# Readings, one per `date +%s` call: a settled base, then +3600, then -4000
# (400s BEFORE the base), then frozen. A cap of 8s would be blown twice over by
# a naive elapsed subtraction; PR As fold shifts the origin instead.
printf '1700000100\n1700000100\n1700003700\n1699999700\n1699999700\n' > "$SEQ"
printf '0' > "$SEQ.i"
OUT_CS="$T/cs.verdict"
launch "$BOT_CS" 0 "$OUT_CS" FAKE_DATE_SEQ="$SEQ" BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=8 RC_READY_TIMEOUT_S=1
P_CS="$LAUNCH_PID"
sleep 3
assert_eq "clock stepped +3600 then -4000: did NOT give up early" "false" "$(exists "$OUT_CS")"
assert_eq "clock stepped: did NOT jump the queue either" "false" "$(exists "$STATE/slots/0")"
rm -f "$STATE/tickets/0-1700000000000000000-svc-cs-blocker"
wait_for 10 file_present "$OUT_CS" || true
wait "$P_CS" 2>/dev/null || true
assert_eq "clock stepped: granted once the queue cleared" "granted:0" "$(cat "$OUT_CS" 2>/dev/null || true)"

echo "=== (d3) releases past the cap are SPREAD, not simultaneous (R9) ==="

reset_state
: > "$GRANT_LOG"
hold_slot 0 svc-blocking
# cap 1, ready_timeout 3, 1 slot -> dispersion 3; ranks 0/1/2 give effective
# caps 1/4/7. Three waiters -- 3 x (cap / hold), the plans shape -- all blocked:
# their timeouts must land at DIFFERENT instants, because a flat cap expires
# every waiter in the same second and reconstitutes the storm the gate replaces.
D1="$(mkbot d3a svc-d3a 1)"; D2="$(mkbot d3b svc-d3b 1)"; D3="$(mkbot d3c svc-d3c 1)"
V1="$T/d3a.v"; V2="$T/d3b.v"; V3="$T/d3c.v"
launch "$D1" 0 "$V1" BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=1 RC_READY_TIMEOUT_S=3
PD1="$LAUNCH_PID"; wait_for 10 have_ticket svc-d3a || true
launch "$D2" 0 "$V2" BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=1 RC_READY_TIMEOUT_S=3
PD2="$LAUNCH_PID"; wait_for 10 have_ticket svc-d3b || true
launch "$D3" 0 "$V3" BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=1 RC_READY_TIMEOUT_S=3
PD3="$LAUNCH_PID"; wait_for 10 have_ticket svc-d3c || true
wait "$PD1" 2>/dev/null || true
wait "$PD2" 2>/dev/null || true
wait "$PD3" 2>/dev/null || true
assert_eq "all three timed out" "3" "$(grep -c 'timeout' "$GRANT_LOG" || true)"
_TS="$(awk '{print $1}' "$GRANT_LOG" | sort -n || true)"
assert_eq "the three timeouts land at three DISTINCT instants" "3" \
    "$(printf '%s\n' "$_TS" | sort -u | wc -l | tr -d ' ' || true)"
_SPREAD=$(( $(printf '%s\n' "$_TS" | tail -1) - $(printf '%s\n' "$_TS" | head -1) ))
assert_true "the spread is at least one dispersion step (>=3s), not simultaneous" \
    "$([ "$_SPREAD" -ge 3 ] && echo true || echo false)"

echo "=== (F17) the temporary rollback carrier ==="

reset_state
BOT_X="$(mkbot x-bot svc-x 1)"
OUT_X="$(BOOT_ADMISSION_DISABLED=1 bash -c '. "$LIB_DIR/lib-common.sh"
             LOG="$1/logs/startup.log"; setup_log_dir "$LOG"
             boot_admission_acquire "$1"' _ "$BOT_X")"
assert_eq "BOOT_ADMISSION_DISABLED=1: verdict is disabled" "disabled" "$OUT_X"
assert_contains "BOOT_ADMISSION_DISABLED=1: one log line, naming ungated" "ADMISSION_DISABLED" "$(cat "$BOT_X/logs/startup.log")"
assert_eq "BOOT_ADMISSION_DISABLED=1: exactly one gate log line" "1" "$(wc -l < "$BOT_X/logs/startup.log" | tr -d ' ')"
assert_eq "BOOT_ADMISSION_DISABLED=1: no ticket" "0" "$(count_lines "$STATE/tickets")"
assert_eq "BOOT_ADMISSION_DISABLED=1: no slot" "0" "$(ls -d "$STATE"/slots/[0-9]* 2>/dev/null | wc -l | tr -d ' ' || true)"
assert_eq "BOOT_ADMISSION_DISABLED=1: no marker" "false" "$(exists "$BOT_X/data/.boot-queued")"

echo "=== only the verdict reaches stdout (R17) ==="

reset_state
BOT_V="$(mkbot v-bot svc-v 1)"
OUT_V="$(BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=10 bash -c '. "$LIB_DIR/lib-common.sh"
             LOG="$1/logs/startup.log"; setup_log_dir "$LOG"
             boot_admission_acquire "$1"' _ "$BOT_V" 2>/dev/null)"
assert_eq "stdout carries the verdict and nothing else" "granted:0" "$OUT_V"
assert_true "the gate log lines went to \$LOG, not stdout" "$([ -s "$BOT_V/logs/startup.log" ] && echo true || echo false)"

echo "=== (R7b) a QUEUED bot is mid-boot to service_is_starting, under real contention ==="

# The one thing the Python suite cannot reach. tests/test_service_is_starting.py
# pins the predicate against a HAND-WRITTEN marker and against one real acquire,
# both in a single process with nothing contending. What is only observable here
# is the state the whole PR exists for: a bot PARKED IN THE WAIT LOOP behind
# another bot holding the only slot, asked by the very predicate keepalive and
# fleet-pulse ask, in a process that is not the one waiting.
#
# It is also the only place the marker's WHOLE LIFETIME is visible: queued ->
# granted -> released, with the predicate consulted at each step. Written this
# way rather than as three cells because the transitions are what matter -- a
# marker that never appears and one that never goes away both satisfy any
# single-instant assertion.
#
# _OS is pinned to Darwin for the asks, deliberately: rung 2 is unreachable
# there, so a rc 0 can ONLY have come from the marker. On Linux the stubless
# systemctl call for a unit that does not exist would answer inactive/dead and
# reach the same verdict, but by elimination rather than by proof.
reset_state
: > "$GRANT_LOG"
BOT_H="$(mkbot q-holder svc-q-holder 1)"
BOT_Q="$(mkbot q-queued svc-q-queued 1)"
ask_starting() {  # ask_starting <bot_dir> -- prints yes|no, as a consumer would
    ( _OS="Darwin"; service_is_starting "$(bot_conf_get "$1" BOT_SERVICE "")" "$1" \
        && echo yes || echo no )
}
assert_eq "before either boots, neither is mid-start" "no" "$(ask_starting "$BOT_Q")"
OUT_H="$T/qh.verdict"; OUT_Q="$T/qq.verdict"
# The holder keeps its slot for 6s, which is this cell's stand-in for the
# readiness poll start-bot.sh releases after.
launch "$BOT_H" 6 "$OUT_H" BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=40
P_H="$LAUNCH_PID"
wait_for 10 file_present "$OUT_H" || true
assert_eq "the holder is granted" "granted:0" "$(cat "$OUT_H" 2>/dev/null || true)"
assert_eq "a GRANTED bot is still mid-boot (one marker, acquire to release)" "yes" "$(ask_starting "$BOT_H")"
launch "$BOT_Q" 0 "$OUT_Q" BOOT_ADMISSION_SLOTS=1 BOOT_ADMISSION_WAIT_MAX_S=40
P_Q="$LAUNCH_PID"
wait_for 10 have_ticket svc-q-queued || true
assert_eq "the second bot is queued, not granted" "false" "$(exists "$OUT_Q")"
assert_eq "a QUEUED bot reads as mid-boot -- the whole point of the marker" "yes" "$(ask_starting "$BOT_Q")"
# And the negative that makes it mean something: a bot that never launched is
# sitting in the same tree, at the same instant, and reads as NOT starting.
BOT_N="$(mkbot q-never svc-q-never 1)"
assert_eq "a bot that never launched reads as NOT starting (same tree, same instant)" "no" "$(ask_starting "$BOT_N")"
wait "$P_Q" 2>/dev/null || true
wait "$P_H" 2>/dev/null || true
assert_eq "the queued bot is granted once the holder releases" "granted:0" "$(cat "$OUT_Q" 2>/dev/null || true)"
assert_eq "after release the holder is no longer mid-boot" "no" "$(ask_starting "$BOT_H")"
assert_eq "after release the second bot is no longer mid-boot either" "no" "$(ask_starting "$BOT_Q")"
assert_eq "no marker survives either bot" "false" \
    "$([ -e "$BOT_H/data/.boot-queued" ] || [ -e "$BOT_Q/data/.boot-queued" ] && echo true || echo false)"

echo "=== resolve_boot_epoch: the key both doors use must not move mid-boot ==="

# Found by wiring the real call site (task 2) and measured, not reasoned.
# lib/start-bot.sh REBUILDS PATH partway through a boot, without /usr/sbin --
# where macOS keeps `sysctl`. So resolve_boot_epoch answered a real epoch at
# ACQUIRE (before the rebuild) and NOTHING at RELEASE (after it): the gate took
# a slot under state/boot/<epoch> and released under state/boot/admission-noepoch,
# so every release removed nothing while still logging ADMISSION_RELEASED, and a
# slot was only ever freed later by the reaper noticing the launcher had exited.
# Measured before the fix: the queued bot was granted 6s after the holder
# released. After it: the same instant.
#
# Platform-neutral BY CONSTRUCTION rather than by a macOS guard: the assertion
# is that the two PATHs AGREE, which is true on Linux for a different reason and
# false on macOS for the reason above. A guard would have skipped the only host
# the defect lives on.
EPOCH_PATH_FULL="$(unset CLAUDLOBBY_BOOT_EPOCH; . "$LIB_DIR/lib-common.sh"; resolve_boot_epoch 2>/dev/null || true)"
EPOCH_PATH_SB="$(unset CLAUDLOBBY_BOOT_EPOCH; PATH=/usr/local/bin:/usr/bin:/bin; . "$LIB_DIR/lib-common.sh"; resolve_boot_epoch 2>/dev/null || true)"
assert_true "an epoch resolves at all on this host" "$([ -n "$EPOCH_PATH_FULL" ] && echo true || echo false)"
assert_eq "the SAME epoch resolves under start-bot own rebuilt PATH" "$EPOCH_PATH_FULL" "$EPOCH_PATH_SB"

# And it is an EPOCH, not some other field of the same line. The macOS source
# reads `{ sec = 1789152060, usec = 890487 }`, and an unanchored `.*sec` is
# greedy: it backtracked into `usec` and this function returned the
# MICROSECONDS as the boot epoch on every macOS host. A stale usec is a small
# number that collides across boots, which is exactly what R14 -- a prior-epoch
# tree is stale UNCONDITIONALLY -- must never have to guess about.
EPOCH_NOW="$(/bin/date +%s)"
assert_true "the epoch is in the past and inside the last decade (not a usec)" \
    "$([ -n "$EPOCH_PATH_FULL" ] && [ "$EPOCH_PATH_FULL" -le "$EPOCH_NOW" ] \
        && [ "$EPOCH_PATH_FULL" -gt "$(( EPOCH_NOW - 315360000 ))" ] && echo true || echo false)"

# The parse itself, against the real macOS line shape, on EITHER platform: an
# `uptime` that refuses -s (what macOS does) and a `sysctl` that answers the
# documented shape. Without both stubs a Linux host would answer from
# `uptime -s` and never reach the branch that carried the defect.
mkdir -p "$T/epochstub"
cat > "$T/epochstub/uptime" <<'FAKEUPTIME'
#!/bin/sh
echo "uptime: illegal option -- s" >&2
exit 1
FAKEUPTIME
cat > "$T/epochstub/sysctl" <<'FAKESYSCTL'
#!/bin/sh
printf '%s\n' "{ sec = 1789152060, usec = 890487 } Fri Sep 11 14:41:00 2026"
FAKESYSCTL
chmod +x "$T/epochstub/uptime" "$T/epochstub/sysctl"
EPOCH_PARSED="$(unset CLAUDLOBBY_BOOT_EPOCH; PATH="$T/epochstub:$PATH"; . "$LIB_DIR/lib-common.sh"; resolve_boot_epoch 2>/dev/null || true)"
assert_eq "kern.boottime parses to the SEC, never the usec" "1789152060" "$EPOCH_PARSED"

echo "=== the call site in start-bot.sh is the one the fixture stands in for ==="

# The launcher fixture above is a STAND-IN for lib/start-bot.sh, and a stand-in
# that has drifted from the thing it stands for certifies nothing. These are
# textual, deliberately: the behavioural proof of the real call site is
# lib/validate-bot-change.sh, which boots it. What a text check CAN own is that
# the three lines exist at all and are shaped the way R11 and R17 require.
SB="$LIB_DIR/start-bot.sh"
SB_ACQUIRE='_adm="$(boot_admission_acquire "$BOT_DIR")"'
SB_TRAP='trap '"'"'boot_admission_release "$BOT_DIR"; _lc_cleanup'"'"' EXIT'
SB_RELEASE='boot_admission_release "$BOT_DIR"'

assert_eq "start-bot.sh acquires, capturing the verdict (R17)" "1" \
    "$(grep -cF "$SB_ACQUIRE" "$SB" || true)"
assert_eq "start-bot.sh composes _lc_cleanup into its EXIT trap (R11)" "1" \
    "$(grep -cF "$SB_TRAP" "$SB" || true)"
# Exactly ONE top-level EXIT trap, and the assertion above already says which.
# Any second one would overwrite it -- and a bare `trap ... EXIT` is the R11
# mutant: it drops lib-commons source-time cleanup and leaks one mktemp -d per
# bot start, on every bot, on every host, forever.
assert_eq "start-bot.sh sets exactly one EXIT trap" "1" \
    "$(grep -cE "^[[:space:]]*trap .* EXIT" "$SB" || true)"
assert_eq "start-bot.sh releases explicitly, not only from the trap" "1" \
    "$(grep -cxF "$SB_RELEASE" "$SB" || true)"
# ORDER, not just presence: a release placed after the two pane sends would hold
# a slot across the 10-19s TUI-draw wait for no benefit to any waiting bot.
SB_REL_LN="$(grep -nxF "$SB_RELEASE" "$SB" | head -1 | cut -d: -f1 || true)"
SB_SEND_LN="$(grep -n 'pane_send_verified "\$TMUX_SOCKET"' "$SB" | head -1 | cut -d: -f1 || true)"
assert_true "release precedes the first pane_send_verified" \
    "$([ -n "$SB_REL_LN" ] && [ -n "$SB_SEND_LN" ] && [ "$SB_REL_LN" -lt "$SB_SEND_LN" ] && echo true || echo false)"

echo "=== both emitted event types are registered (#903) ==="

# An event type nothing names is one no reader can filter FOR, and the rows
# that go missing are exactly the ones the filter cannot return -- so the gap
# is invisible from inside the reader. Checked in BOTH directions against the
# shipped registry, because a registry entry for a type the gate never emits is
# the same drift wearing the other hat.
KV="$REPO_ROOT/claudlobby/known_values.py"
for _ev in boot_admission_timeout boot_admission_unavailable; do
    assert_true "$_ev is emitted by the gate" \
        "$(grep -q "$_ev" "$LIB_DIR/boot-admission.sh" && echo true || echo false)"
    assert_true "$_ev is registered in known_values.py" \
        "$(grep -q "\"$_ev\"" "$KV" && echo true || echo false)"
done
assert_eq "the registry names exactly the two the gate emits" "2" \
    "$(grep -c '"boot_admission_' "$KV" || true)"
# #903s own deliverable is the COMPLETE event-type SSOT, and claudlobby/brief.py
# keys its standing disclosure on that symbol existing. A two-entry set under
# that name would retire the disclosure while the defect stands.
assert_eq "the registry does NOT claim to be the #903 SSOT" "0" \
    "$(grep -c '^FLEET_EVENT_TYPES' "$KV" || true)"

echo "=== start-bot.sh carries NO second serialization primitive ==="

# The #304 lock is deleted in the same commit that lands the gate; the gate is
# that lock done properly, not a protocol beside it. "The boot lock returns" is
# on the mutant list, and this is what catches it.
assert_eq "no BOOT_LOCK anywhere under lib/" "0" "$(grep -rl 'BOOT_LOCK' "$LIB_DIR" 2>/dev/null | wc -l | tr -d ' ' || true)"
assert_eq "no .claudlobby-fleet-boot.lock anywhere under lib/" "0" "$(grep -rl 'claudlobby-fleet-boot' "$LIB_DIR" 2>/dev/null | wc -l | tr -d ' ' || true)"
assert_eq "start-bot.sh calls no flock" "0" "$(grep -c 'flock' "$LIB_DIR/start-bot.sh" || true)"
assert_eq "start-bot.sh mkdirs no lock of its own" "0" "$(grep -cE 'mkdir[^;&|]*lock' "$LIB_DIR/start-bot.sh" || true)"
assert_eq "no harness site still neuters a boot lock" "0" "$(grep -c 'BOOT_LOCK_HOLD_S' "$LIB_DIR/validate-bot-change.sh" || true)"
# The gates own door is the ONE serializer, and it is the one the call site
# reaches through lib-common.
assert_eq "lib-common sources the gate (one source line, beside supervisor.sh)" "1" "$(grep -c '\. "\$_LIB_COMMON_DIR/boot-admission.sh"' "$LIB_DIR/lib-common.sh" || true)"

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$TOTAL" -gt 0 ] || { echo "FAIL: zero assertions ran"; exit 1; }
[ "$FAIL" -eq 0 ] || exit 1
