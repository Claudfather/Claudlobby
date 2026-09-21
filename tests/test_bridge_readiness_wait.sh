#!/usr/bin/env bash
# tests/test_bridge_readiness_wait.sh — wait_bridge_ready_state's ceiling is
# wall-clock time, not a probe count (#1573).
#
# start-bot.sh used to poll readiness with `for _i in $(seq 1 "$_rc_iters")`
# where `_rc_iters = RC_READY_TIMEOUT_S * 2` — a count of PROBES, on the
# assumption every 0.5s-interval probe is free. Under real boot load a probe
# cost ~1.7s, so a configured "90s" ceiling ran for five minutes (measured
# 2026-09-19: POLL_START 18:02:23, TIMEOUT 18:07:25). This suite pins the
# extracted wait_bridge_ready_state to the wall clock instead: with a probe
# stubbed to cost 1s each, a 2s ceiling must still return in well under the
# ~6s the old iteration-count loop would have taken (4 probes x 1s + sleeps),
# let alone the ~5min a loaded host measured against a 90s config.
#
# Scenario (J) pins the other half of that ceiling: the clock-step fold must
# not be able to swallow it. Its threshold used to BE the ceiling, so a probe
# costing more than the whole ceiling folded every iteration and the loop ran
# on unbounded (measured against a 1s ceiling: still polling when it was
# killed at 130s; this scenario returns in 2s).
#
# Standalone bash (not pytest-collected on its own; discovered by
# tests/test_sh_suites.py). Runs under macOS /bin/bash (3.2) — no apostrophes
# in comments inside $( ) (gate: tests/test_bash_parse.py).
#
# Hermetic: every scenario stubs bridge_state (and, for the crash case,
# check_tmux_session) as a plain shell function defined AFTER sourcing
# lib-common.sh, so no real tmux session, network, or Telegram bridge is ever
# touched.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
PASS=0; FAIL=0; TOTAL=0
assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS + 1)); else echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1)); fi
}

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export CLAUDLOBBY_ROOT="$T"

# shellcheck source=../lib/lib-common.sh
. "$LIB_DIR/lib-common.sh"

# call_wait <args...> — invoke wait_bridge_ready_state and capture its stdout
# and return code into _WBRS_OUT / _WBRS_RC without tripping this script's own
# set -e: a bare `out=$(cmd)` where cmd returns non-zero aborts the script
# under errexit (only the condition of an if/while, or an && / || list, is
# exempt), and this function is expected to return 1 and 2 by design.
call_wait() {
    if _WBRS_OUT="$(wait_bridge_ready_state "$@" 2>/dev/null)"; then
        _WBRS_RC=0
    else
        _WBRS_RC=$?
    fi
}

echo "=== wait_bridge_ready_state — the wall-clock pin ==="

# (A) The defect itself: a probe that is never going to succeed and costs 1s
# each call. The OLD iteration-count loop ran 4 probes (timeout_s * 2) plus
# their 0.5s sleeps -- about 6s+ -- and that gap only widens as a probe gets
# slower, which is exactly what happened under real boot load. The wall-clock
# version must stop once 2 real seconds have passed, however many (or few)
# probes that bought.
bridge_state() { sleep 1; printf 'no_bridge'; }
BOT1="$T/bot1"; mkdir -p "$BOT1"
_t0=$(date +%s)
call_wait "$BOT1" 2 "" ""
_t1=$(date +%s)
assert_eq "always-no_bridge: returns 1 (not ready, not crashed)" "1" "$_WBRS_RC"
assert_eq "always-no_bridge: prints the last state (no_bridge)" "no_bridge" "$_WBRS_OUT"
assert_eq "always-no_bridge: wall time under 5s despite a 1s/probe cost" "true" \
    "$([ "$((_t1 - _t0))" -lt 5 ] && echo true || echo false)"

# (B) A transient probe: no_bridge twice, then up. The counter lives in a file
# because bridge_state runs in a subshell on every call (command
# substitution), so a plain shell counter variable would not persist across
# calls.
BOT2="$T/bot2"; mkdir -p "$BOT2"
CNT2="$T/cnt2"; echo 0 > "$CNT2"
bridge_state() {
    local n; n=$(cat "$CNT2"); n=$((n + 1)); echo "$n" > "$CNT2"
    if [ "$n" -le 2 ]; then printf 'no_bridge'; else printf 'up'; fi
}
call_wait "$BOT2" 10 "" ""
assert_eq "no_bridge twice then up: returns 0" "0" "$_WBRS_RC"
assert_eq "no_bridge twice then up: prints up" "up" "$_WBRS_OUT"

# (C) no_handle is terminal and immediate -- a non-channel bot has no poller
# to await, so this must not burn any of the ceiling.
BOT3="$T/bot3"; mkdir -p "$BOT3"
bridge_state() { printf 'no_handle'; }
_t0=$(date +%s)
call_wait "$BOT3" 10 "" ""
_t1=$(date +%s)
assert_eq "no_handle: returns 0" "0" "$_WBRS_RC"
assert_eq "no_handle: prints no_handle" "no_handle" "$_WBRS_OUT"
assert_eq "no_handle: resolved at once, not after the ceiling" "true" \
    "$([ "$((_t1 - _t0))" -lt 3 ] && echo true || echo false)"

# (D) no_token: the bot_expects_no_token read stays INSIDE the function (the
# caller keeps one call), so this drives it through a real bot.conf rather
# than stubbing bot_expects_no_token itself.
bridge_state() { printf 'no_token'; }

BOT4_CANARY="$T/bot4-canary"; mkdir -p "$BOT4_CANARY"
printf 'EXPECT_NO_TOKEN=1\n' > "$BOT4_CANARY/bot.conf"
call_wait "$BOT4_CANARY" 10 "" ""
assert_eq "no_token + declared EXPECT_NO_TOKEN=1: returns 0" "0" "$_WBRS_RC"
assert_eq "no_token + declared EXPECT_NO_TOKEN=1: prints no_token" "no_token" "$_WBRS_OUT"

BOT4_REAL="$T/bot4-real"; mkdir -p "$BOT4_REAL"
printf 'BOT_NAME=real\n' > "$BOT4_REAL/bot.conf"
call_wait "$BOT4_REAL" 2 "" ""
assert_eq "no_token, real bot (no EXPECT_NO_TOKEN): times out, returns 1" "1" "$_WBRS_RC"
assert_eq "no_token, real bot (no EXPECT_NO_TOKEN): prints no_token" "no_token" "$_WBRS_OUT"

# (E) A probe that recovers on the CLOCK rather than on a call count -- proves
# the loop keeps polling (rather than treating no_bridge as terminal) until
# bridge_state actually reports up.
BOT5="$T/bot5"; mkdir -p "$BOT5"
MARK5="$T/mark5"; date +%s > "$MARK5"
bridge_state() {
    local now start; now=$(date +%s); start=$(cat "$MARK5")
    if [ "$((now - start))" -ge 3 ]; then printf 'up'; else printf 'no_bridge'; fi
}
call_wait "$BOT5" 10 "" ""
assert_eq "no_bridge for 3s then up: returns 0" "0" "$_WBRS_RC"
assert_eq "no_bridge for 3s then up: prints up" "up" "$_WBRS_OUT"

# (F) The crash exit: check_tmux_session is one of the FOUR exits the
# extraction must preserve byte-for-byte in meaning -- a dead tmux session is
# fatal AT ONCE, never waiting out the ceiling. Stubbed since a hermetic suite
# has no real tmux session to kill; bridge_state must never even be reached.
BOT6="$T/bot6"; mkdir -p "$BOT6"
bridge_state() { printf 'no_bridge'; }
check_tmux_session() { return 1; }
_t0=$(date +%s)
call_wait "$BOT6" 10 "" "" "fake-session" "fake-socket"
_t1=$(date +%s)
assert_eq "tmux session gone: returns 2 (crashed)" "2" "$_WBRS_RC"
assert_eq "tmux session gone: prints crashed" "crashed" "$_WBRS_OUT"
assert_eq "tmux session gone: detected at once, not after the ceiling" "true" \
    "$([ "$((_t1 - _t0))" -lt 3 ] && echo true || echo false)"

# (G), (H), (I): the RTC-less primary host has no real-time clock, so its
# clock steps FORWARD by the whole downtime once NTP syncs after boot -- a
# bring-up in flight across that step must not read the step itself as
# elapsed time. `date` is stubbed as a shell function returning a canned
# sequence of readings, one per call; the counter lives in a file because a
# stubbed command run via command substitution forks a subshell each call, so
# a plain shell variable could not survive between calls (the same reason
# the bridge_state call-counters above use a file). Each stub is scoped to its
# own scenario with `unset -f date` right after the call, so the surviving
# scenarios and any later suite keep reading the real clock for their own
# wall-time assertions.

# (G) A forward step. The jump lands between two probes while bridge_state is
# still reporting no_bridge; the step must be folded out of "started" rather
# than counted as elapsed, so the 4s ceiling is never breached and the loop
# keeps polling until bridge_state turns up on the very next probe.
BOT7="$T/bot7"; mkdir -p "$BOT7"
CNT7="$T/cnt7"; echo 0 > "$CNT7"
bridge_state() {
    local n; n=$(cat "$CNT7"); n=$((n + 1)); echo "$n" > "$CNT7"
    if [ "$n" -le 2 ]; then printf 'no_bridge'; else printf 'up'; fi
}
DSEQ7="$T/dseq7"; echo 0 > "$DSEQ7"
date() {
    local i v
    i=$(cat "$DSEQ7")
    case "$i" in
        0) v=1000 ;;
        1) v=1000 ;;
        *) v=91000 ;;
    esac
    echo "$((i + 1))" > "$DSEQ7"
    printf '%s' "$v"
}
call_wait "$BOT7" 4 "" ""
unset -f date
assert_eq "forward clock step (+90000): does not time out, returns 0" "0" "$_WBRS_RC"
assert_eq "forward clock step (+90000): keeps polling to up" "up" "$_WBRS_OUT"

# (H) A backward step (an operator or NTP correcting the clock the other
# way) must not read as a negative elapsed time and race past the ceiling
# either -- the same fold-it-out rule applies regardless of sign.
BOT8="$T/bot8"; mkdir -p "$BOT8"
CNT8="$T/cnt8"; echo 0 > "$CNT8"
bridge_state() {
    local n; n=$(cat "$CNT8"); n=$((n + 1)); echo "$n" > "$CNT8"
    if [ "$n" -le 2 ]; then printf 'no_bridge'; else printf 'up'; fi
}
DSEQ8="$T/dseq8"; echo 0 > "$DSEQ8"
date() {
    local i v
    i=$(cat "$DSEQ8")
    case "$i" in
        0) v=91000 ;;
        1) v=91000 ;;
        *) v=86000 ;;
    esac
    echo "$((i + 1))" > "$DSEQ8"
    printf '%s' "$v"
}
call_wait "$BOT8" 4 "" ""
unset -f date
assert_eq "backward clock step (-5000): does not time out, returns 0" "0" "$_WBRS_RC"
assert_eq "backward clock step (-5000): keeps polling to up" "up" "$_WBRS_OUT"

# (I) A NORMAL, non-jumping advance of about 1s per probe must still time out
# exactly as before -- the clock-step tolerance must not swallow real elapsed
# time. bridge_state never resolves, so this only stops via the ceiling, on
# the third reading (the initial "started" plus two per-probe checks).
BOT9="$T/bot9"; mkdir -p "$BOT9"
bridge_state() { printf 'no_bridge'; }
DSEQ9="$T/dseq9"; echo 0 > "$DSEQ9"
date() {
    local i v
    i=$(cat "$DSEQ9")
    case "$i" in
        0) v=1000 ;;
        1) v=1001 ;;
        *) v=1002 ;;
    esac
    echo "$((i + 1))" > "$DSEQ9"
    printf '%s' "$v"
}
call_wait "$BOT9" 2 "" ""
unset -f date
assert_eq "normal 1s/probe advance: still times out on the third reading" "1" "$_WBRS_RC"
assert_eq "normal 1s/probe advance: prints the last state (no_bridge)" "no_bridge" "$_WBRS_OUT"

# (J) The costly probe: ONE probe that costs MORE than the whole ceiling. The
# clock-step fold's threshold used to BE the ceiling, so this shape folded
# every iteration into "started" and the loop never expired -- measured
# against a 1s ceiling: still polling when it was killed at 130s. It is not
# hypothetical:
# lib/validate-bot-change.sh drives the real start-bot.sh with
# RC_READY_TIMEOUT_S=1, and a probe there costs seconds under load. A ceiling
# that cannot expire hangs the harness (start-bot.sh never returns, so the
# `|| true` after it is never reached) and, for PR B, is a gate holder that
# never releases. The fold threshold is now max(timeout_s, 60), so a 2s probe
# against a 1s ceiling is ordinary elapsed time and the FIRST check expires.
#
# THIS ONE SCENARIO IS BOUNDED, and the bound is the point: the regression it
# pins is an UNBOUNDED loop, so a bare call_wait here would not fail on that
# regression -- it would hang this suite, then tests/test_sh_suites.py, then
# CI (measured: killed by hand at 30s), and it is why the committed-code
# mutant driver cannot carry this mutant at all. The call runs in a
# background subshell that writes "rc:out" to a file; this shell polls
# kill -0 for at most 15s, then reaps the subshell and its probe sleep and
# records a FAIL naming the regression. A test for a hang has to own its own
# clock -- every other scenario here is self-limiting and needs none.
BOT10="$T/bot10"; mkdir -p "$BOT10"
bridge_state() { sleep 2; printf 'no_bridge'; }

# Reap a pid and the children it may have left (the probe sleep sits under a
# command substitution, so it is a grandchild of the subshell). pgrep is on
# both platforms; without it the plain kill still ends the subshell and a 2s
# sleep exits on its own moments later.
_reap_tree() {
    local p="$1" c g
    if command -v pgrep >/dev/null 2>&1; then
        for c in $(pgrep -P "$p" 2>/dev/null); do
            for g in $(pgrep -P "$c" 2>/dev/null); do kill "$g" 2>/dev/null || true; done
            kill "$c" 2>/dev/null || true
        done
    fi
    kill "$p" 2>/dev/null || true
}

J_RES="$T/j-result"; rm -f "$J_RES"
_t0=$(date +%s)
(
    if _o="$(wait_bridge_ready_state "$BOT10" 1 "" "" 2>/dev/null)"; then _r=0; else _r=$?; fi
    # Written then renamed, so the file this shell polls is never half a
    # result: it does not exist, or it is the whole answer.
    printf '%s:%s' "$_r" "$_o" > "$J_RES.tmp" && mv "$J_RES.tmp" "$J_RES"
) &
_j_pid=$!
_j_waited=0
# Either signal ends the wait: the result landing, or the subshell dying. The
# VERDICT is the file, never the pid, because kill -0 on a just-finished child
# is RACY -- measured on bash 3.2.57: a child that has exited still answered
# kill -0 in 1 of 5 samples taken immediately (0 of 5 after 0.5s), the window
# before the shell reaps it. A file that exists is an answer that was written;
# a pid is only a hint about when to stop waiting.
while kill -0 "$_j_pid" 2>/dev/null && [ ! -s "$J_RES" ]; do
    if [ "$_j_waited" -ge 30 ]; then break; fi   # 30 x 0.5s = 15s
    sleep 0.5
    _j_waited=$((_j_waited + 1))
done
_t1=$(date +%s)
if [ -s "$J_RES" ]; then
    wait "$_j_pid" 2>/dev/null || true
    _j_out="$(cat "$J_RES")"
    assert_eq "probe costlier than the ceiling: returns 1 (times out)" "1" "${_j_out%%:*}"
    assert_eq "probe costlier than the ceiling: prints the last state (no_bridge)" "no_bridge" "${_j_out#*:}"
    assert_eq "probe costlier than the ceiling: expires in a few seconds, not never" "true" \
        "$([ "$((_t1 - _t0))" -lt 10 ] && echo true || echo false)"
else
    _reap_tree "$_j_pid"
    wait "$_j_pid" 2>/dev/null || true
    assert_eq "probe costlier than the ceiling: HUNG past 15 s (the fold threshold is the ceiling again)" \
        "completed" "HUNG"
fi

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
