#!/usr/bin/env bash
# tests/test_portable_helpers_711.sh — #711 coreutils-portability helpers.
#
# #711 routes the last three GNU-coreutils bypasses through the GNU/BSD
# abstraction layer. This suite runs on both supported CI platforms and pins
# the helpers' native behavior. Hermetic: only lib-common.sh and installed
# command-line prerequisites — no tmux, no network, no services.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../claudlobby/_runtime_scripts"
PASS=0; FAIL=0; TOTAL=0

assert_eq() {
    TOTAL=$((TOTAL + 1))
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  PASS: $desc"; PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc (expected '$expected', got '$actual')"; FAIL=$((FAIL + 1))
    fi
}
assert_true() {
    TOTAL=$((TOTAL + 1))
    local desc="$1"; shift
    if "$@"; then echo "  PASS: $desc"; PASS=$((PASS + 1))
    else echo "  FAIL: $desc"; FAIL=$((FAIL + 1)); fi
}

# shellcheck source=/dev/null
. "$LIB_DIR/lib-common.sh"

echo "=== proc_rss_kb: portable self+children RSS sum (site 1: ps --ppid) ==="
# Measure this shell's RSS with no persistent child, then with one backgrounded
# direct child. proc_rss_kb must count self plus direct children; the GNU-only
# `ps --ppid` it replaces returned 0 on BSD/macOS.
base=$(proc_rss_kb "$$")
sleep 300 &
child=$!
withchild=$(proc_rss_kb "$$")
kill "$child" 2>/dev/null || true
wait "$child" 2>/dev/null || true

# A positive integer proves the portable `ps -A -o pid=,ppid=,rss=` parsed.
assert_true "proc_rss_kb returns a positive integer" test "$base" -gt 0
# Adding a direct child raises the sum -> children ARE summed (the macOS failure
# was 0). If proc_rss_kb counted self only, withchild == base and this goes RED.
assert_true "proc_rss_kb includes a direct child in the sum" test "$withchild" -gt "$base"

echo "=== iso_to_epoch: GitHub createdAt (RFC3339 Z) parse (site 2: date -d) ==="
# The sweep's timestamps are GitHub `createdAt` -> ...Z. For a Z-pinned instant
# the portable helper must yield the UTC epoch represented by that timestamp.
iso="2026-01-01T00:00:00Z"
new=$(iso_to_epoch "$iso")
assert_eq "iso_to_epoch maps a Z timestamp to its UTC epoch" "1767225600" "$new"
assert_true "iso_to_epoch yields a positive epoch" test "$new" -gt 0

echo "=== timeout availability: freshbox guard passes on the equipped host (site 3) ==="
# CI equips timeout(1) on Linux and gtimeout(1) on macOS. Both must run the
# command rather than spuriously treating the supported runner as unequipped.
assert_true "_TIMEOUT_BIN resolves on this host" test -n "$_TIMEOUT_BIN"
assert_eq "with_timeout runs a command to completion" "ok" "$(with_timeout 5 echo ok)"

echo
echo "TOTAL=$TOTAL PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
