#!/usr/bin/env bash
# tests/test_portable_helpers_711.sh — #711 coreutils-portability helpers.
#
# #711 routes the last three GNU-coreutils bypasses through the GNU/BSD
# abstraction layer. This suite pins the Linux behavior of the helpers they land
# on so a regression is caught in CI; the macOS branch of each is reasoned in the
# PR, not run here (eng runs Linux). Hermetic: only lib-common.sh + coreutils
# already on PATH — no tmux, no network, no services.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
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
# the portable helper must yield the SAME epoch the prior `date -d` produced, so
# the Linux staleness integer is unchanged.
iso="2026-01-01T00:00:00Z"
old=$(date -d "$iso" +%s)
new=$(iso_to_epoch "$iso")
assert_eq "iso_to_epoch matches prior 'date -d' for a Z timestamp" "$old" "$new"
assert_true "iso_to_epoch yields a positive epoch" test "$new" -gt 0

echo "=== timeout availability: freshbox guard passes on Linux (site 3) ==="
# freshbox-boot-gate SKIPs when neither timeout(1) nor gtimeout resolves; on the
# Linux CI runner one MUST resolve, so the gate is not spuriously skipped. (The
# absent case is stock macOS -> clean SKIP, reasoned in the PR, not run here.)
assert_true "_TIMEOUT_BIN resolves on this host" test -n "$_TIMEOUT_BIN"
assert_eq "with_timeout runs a command to completion" "ok" "$(with_timeout 5 echo ok)"

echo
echo "TOTAL=$TOTAL PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
