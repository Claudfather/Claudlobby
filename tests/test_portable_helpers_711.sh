#!/usr/bin/env bash
# tests/test_portable_helpers_711.sh — #711 coreutils-portability helpers.
#
# #711 routes the last three GNU-coreutils bypasses through the GNU/BSD
# abstraction layer. Run the native GNU/BSD branches on either test host, with
# explicit controls for the optional timeout capability. Hermetic: only
# lib-common.sh + host utilities — no tmux, no network, no services.
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
# Independently known Unix epoch, not a GNU-only expected-value calculation.
old=1767225600
new=$(iso_to_epoch "$iso")
assert_eq "iso_to_epoch matches prior 'date -d' for a Z timestamp" "$old" "$new"
assert_true "iso_to_epoch yields a positive epoch" test "$new" -gt 0

echo "=== timeout capability: freshbox guard and generic fallback (site 3) ==="
# freshbox-boot-gate SKIPs when neither timeout(1) nor gtimeout resolves; on the
# Linux CI runner one MUST resolve, so the gate is not spuriously skipped.
# Stock macOS may lack it: with_timeout then runs UNGUARDED. These controls
# exercise forwarding/fallback only, never claim the fallback has a deadline.
if [ "$_OS" = Linux ]; then
    assert_true "_TIMEOUT_BIN resolves on Linux" test -n "$_TIMEOUT_BIN"
elif [ -n "$_TIMEOUT_BIN" ]; then
    assert_true "resolved timeout is executable" test -x "$_TIMEOUT_BIN"
else
    echo "  INFO: no timeout executable; generic with_timeout runs unguarded"
fi
assert_eq "with_timeout runs a command to completion" "ok" "$(with_timeout 5 echo ok)"

# A function stub isolates the available-binary branch without needing GNU
# coreutils installed. Its output proves the duration and argument boundaries.
_fixture_timeout() { local secs="$1"; shift; printf '%s|' "$secs"; "$@"; }
saved_timeout="$_TIMEOUT_BIN"
_TIMEOUT_BIN=_fixture_timeout
assert_eq "available timeout receives duration and intact arguments" "5|ok with spaces" \
    "$(with_timeout 5 printf '%s' 'ok with spaces')"
rc=0; with_timeout 5 /bin/sh -c 'exit 23' >/dev/null || rc=$?
assert_eq "available timeout preserves command failure" "23" "$rc"

_TIMEOUT_BIN=""
assert_eq "absent timeout runs the command unguarded with intact arguments" "ok with spaces" \
    "$(with_timeout 5 printf '%s' 'ok with spaces')"
rc=0; with_timeout 5 /bin/sh -c 'exit 23' || rc=$?
assert_eq "unguarded fallback preserves command failure" "23" "$rc"
_TIMEOUT_BIN="$saved_timeout"

echo
echo "TOTAL=$TOTAL PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
