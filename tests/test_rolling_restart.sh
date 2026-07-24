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

echo "=== bridge_fence_write + wait_bridge_ready — the marker-fenced gate ==="
# shellcheck source=../lib/lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT="$T/bot"; mkdir -p "$BOT/logs"
LOG="$BOT/logs/startup.log"

# Prior-boot content that already contains a (stale) BRIDGE_READY — the exact
# "looks healthy but THIS boot never came up" trap the gate exists to stop.
printf '%s\n' "2026-01-01 POLL_START — old boot" "2026-01-01 BRIDGE_READY — Telegram poller up" > "$LOG"

# (1) bridge_fence_write appends a unique marker and echoes the token.
tok="$(bridge_fence_write "$BOT")"
assert_eq "(1) fence marker written to the log" "true" "$(grep -q "$tok" "$LOG" && echo true || echo false)"

# (2) The stale BRIDGE_READY (BEFORE the marker) must NOT pass.
wait_bridge_ready "$BOT" 0 "$tok" && r=ready || r=timeout
assert_eq "(2) stale BRIDGE_READY before the marker is NOT accepted" "timeout" "$r"

# (3) A fresh BRIDGE_READY AFTER the marker passes.
printf '%s\n' "2026-07-23 POLL_START — this boot" "2026-07-23 BRIDGE_READY — Telegram poller up" >> "$LOG"
wait_bridge_ready "$BOT" 0 "$tok" && r=ready || r=timeout
assert_eq "(3) fresh BRIDGE_READY after the marker passes" "ready" "$r"

# (4) A NEW marker with no ready-yet after it → timeout (drives the serial wait).
tok2="$(bridge_fence_write "$BOT")"
printf '%s\n' "2026-07-23 POLL_START — restarted, bridge still coming up" >> "$LOG"
wait_bridge_ready "$BOT" 0 "$tok2" && r=ready || r=timeout
assert_eq "(4) new marker, BRIDGE_READY not yet written → timeout" "timeout" "$r"

# (5) Rotation that KEEPS the marker (log-rotate's line-tail): still passes.
: > "$LOG"
tok3="$(bridge_fence_write "$BOT")"
printf '%s\n' "2026-07-23 POLL_START — booting" "2026-07-23 BRIDGE_READY — Telegram poller up" >> "$LOG"
tail -n 3 "$LOG" > "$LOG.rot" && mv "$LOG.rot" "$LOG"   # rotation keeps the recent tail
wait_bridge_ready "$BOT" 0 "$tok3" && r=ready || r=timeout
assert_eq "(5) rotation keeping the marker still accepts a fresh BRIDGE_READY" "ready" "$r"

# (6) Rotation that DROPS the marker → fail CLOSED (timeout), NEVER false-ready.
#     This is the #696 finding: the old byte-offset fallback accepted a stale
#     pre-restart BRIDGE_READY here; the marker approach rejects it.
: > "$LOG"
printf '%s\n' "OLD POLL_START — prior boot" "OLD BRIDGE_READY — Telegram poller up" > "$LOG"  # marker rotated away
wait_bridge_ready "$BOT" 0 "RR_FENCE_that_was_rotated_away" && r=ready || r=timeout
assert_eq "(6) marker rotated away → fail CLOSED (stale BRIDGE_READY rejected)" "timeout" "$r"

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
