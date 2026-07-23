#!/usr/bin/env bash
# tests/test_rolling_restart.sh — rolling-restart.sh + wait_bridge_ready gate.
# Standalone bash (not pytest-collected). Runs under macOS /bin/bash (3.2).
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

echo "=== wait_bridge_ready — the fresh-fenced BRIDGE_READY gate ==="
# shellcheck source=../lib/lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT="$T/bot"; mkdir -p "$BOT/logs"
LOG="$BOT/logs/startup.log"

# (1) A STALE BRIDGE_READY (before the fence) must NOT pass — the exact
#     "looks healthy but this boot never came up" trap the gate exists to stop.
printf '%s\n' "2026-01-01 POLL_START — old boot" "2026-01-01 BRIDGE_READY — Telegram poller up" > "$LOG"
fence="$(wc -c < "$LOG" | tr -d ' ')"
wait_bridge_ready "$BOT" 0 "$fence" && r=pass || r=timeout
assert_eq "(1) stale BRIDGE_READY below the fence is NOT accepted" "timeout" "$r"

# (2) A FRESH BRIDGE_READY (appended after the fence) passes.
printf '%s\n' "2026-07-23 POLL_START — this boot" "2026-07-23 BRIDGE_READY — Telegram poller up" >> "$LOG"
wait_bridge_ready "$BOT" 0 "$fence" && r=ready || r=timeout
assert_eq "(2) fresh BRIDGE_READY above the fence passes" "ready" "$r"

# (3) Fence with no fresh line yet → timeout (drives the serial wait).
fence2="$(wc -c < "$LOG" | tr -d ' ')"
printf '%s\n' "2026-07-23 POLL_START — restarted, bridge still coming up" >> "$LOG"
wait_bridge_ready "$BOT" 0 "$fence2" && r=ready || r=timeout
assert_eq "(3) fresh POLL_START but no BRIDGE_READY yet → timeout" "timeout" "$r"

# (4) Rotation fallback: log truncated below the fence → scan from the last
#     POLL_START. A fresh BRIDGE_READY there still passes.
: > "$LOG"
printf '%s\n' "POLL_START — post-rotation boot" "BRIDGE_READY — Telegram poller up" >> "$LOG"
wait_bridge_ready "$BOT" 0 999999 && r=ready || r=timeout
assert_eq "(4) rotation fallback accepts a fresh BRIDGE_READY after POLL_START" "ready" "$r"

# (5) Rotation fallback with POLL_START but NO BRIDGE_READY → timeout.
: > "$LOG"
printf '%s\n' "POLL_START — post-rotation boot, bridge down" >> "$LOG"
wait_bridge_ready "$BOT" 0 999999 && r=ready || r=timeout
assert_eq "(5) rotation fallback with no BRIDGE_READY → timeout" "timeout" "$r"

echo ""
echo "=== rolling-restart.sh — fleet enumeration + CLI guards ==="

# rr_list_fleets finds fleets at BOTH depths (flat + nested). Source the script
# (source-guard keeps main from running) and call the enumerator directly.
mkdir -p "$T/local/flatfleet" "$T/local/sysA/nestedfleet"
printf 'fleet:\n  name: flatfleet\n' > "$T/local/flatfleet/fleet.yaml"
printf 'fleet:\n  name: nestedfleet\n' > "$T/local/sysA/nestedfleet/fleet.yaml"
# shellcheck source=../lib/rolling-restart.sh
. "$LIB_DIR/rolling-restart.sh"
_fleets="$(rr_list_fleets | sort | tr '\n' ',')"
assert_eq "rr_list_fleets finds flat + nested fleets" "flatfleet,nestedfleet," "$_fleets"

# CLI guards (subprocess so an exit doesn't kill the test shell).
run_rc() { CLAUDLOBBY_ROOT="$T" bash "$LIB_DIR/rolling-restart.sh" "$@" >/dev/null 2>&1; echo $?; }
assert_eq "no fleet and no --all → usage error (2)"   "2" "$(run_rc)"
assert_eq "unknown option → error (2)"                "2" "$(run_rc --bogus)"
assert_eq "non-integer --ceiling → error (2)"         "2" "$(run_rc flatfleet --ceiling abc)"

echo ""
echo "=== weekly-worker-restart.sh rides the shared gate ==="
assert_eq "weekly restart calls wait_bridge_ready" "true" \
    "$(grep -q 'wait_bridge_ready' "$LIB_DIR/weekly-worker-restart.sh" && echo true || echo false)"

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
