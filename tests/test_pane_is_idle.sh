#!/bin/bash
# Test harness for lib-common.sh pane_is_idle().
# pane_is_idle is the shared idle-detection used by fleet-pulse's activity_stuck
# check to avoid flagging a finished, at-prompt bot (which legitimately makes no
# tool calls). Mirrors the IDLE branch of keepalive.sh classify_pane.
#
# Usage: bash tests/test_pane_is_idle.sh
#   Exit 0 = all pass, exit 1 = failures.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=../lib/lib-common.sh
. "$REPO_DIR/lib/lib-common.sh"

passed=0
failed=0
total=0

# assert_idle <description> <expected: idle|busy> <pane_text>
assert_idle() {
    local desc="$1" expected="$2" text="$3"
    total=$((total + 1))
    local actual="busy"
    if pane_is_idle "$text"; then actual="idle"; fi
    if [ "$actual" = "$expected" ]; then
        passed=$((passed + 1))
        printf "  PASS  %-40s → %s\n" "$desc" "$actual"
    else
        failed=$((failed + 1))
        printf "  FAIL  %-40s → %s (expected %s)\n" "$desc" "$actual" "$expected"
    fi
}

echo "=== pane_is_idle tests ==="
echo ""

assert_idle "bare prompt glyph >"        idle "$(printf 'some output\n> ')"
assert_idle "fancy prompt glyph ❯"       idle "$(printf 'done\n❯')"
assert_idle "remote control active"      idle "Remote Control active"
assert_idle "permission prompt Allow"    idle "Allow this tool call? Allow / Deny"
assert_idle "yes/no prompt"              idle "Proceed? Yes/No"
assert_idle "animated spinner (busy)"    busy "$(printf '⠹ Cogitating… (esc to interrupt)')"
assert_idle "verb activity (busy)"       busy "Running tests…"
assert_idle "arbitrary output (busy)"    busy "$(printf 'line one\nline two\nline three')"

# Operator-extended idle pattern
KEEPALIVE_IDLE_PATTERNS='WAITING_FOR_REVIEW' assert_idle "custom idle pattern" idle "WAITING_FOR_REVIEW"

echo ""
echo "=== $passed/$total passed, $failed failed ==="
[ "$failed" -eq 0 ]
