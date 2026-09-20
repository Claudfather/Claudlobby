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

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
