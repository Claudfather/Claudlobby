#!/bin/bash
# Test harness for keepalive.sh pane-state classification.
# Sources classify_pane() directly from keepalive.sh — single source of truth.
#
# Usage: bash tests/test_keepalive_classify.sh
#   Exit 0 = all pass, exit 1 = failures.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURE_DIR="$SCRIPT_DIR/fixtures/pane-states"

# Source only the classify_pane function from keepalive.sh.
# keepalive.sh sources lib-common.sh and runs bot-level setup at the top
# level, so we extract just the function definition.
eval "$(sed -n '/^classify_pane()/,/^}/p' "$REPO_DIR/lib/keepalive.sh")"

passed=0
failed=0
total=0

assert_state() {
    local fixture="$1"
    local expected="$2"
    total=$((total + 1))

    local content
    content=$(tail -10 "$FIXTURE_DIR/$fixture")
    local actual
    actual=$(classify_pane "$content")

    if [ "$actual" = "$expected" ]; then
        passed=$((passed + 1))
        printf "  PASS  %-35s → %s\n" "$fixture" "$actual"
    else
        failed=$((failed + 1))
        printf "  FAIL  %-35s → %s (expected %s)\n" "$fixture" "$actual" "$expected"
    fi
}

echo "=== keepalive pane-state classification tests ==="
echo ""

# BUSY fixtures
assert_state "busy-spinner.txt" "BUSY"
assert_state "busy-verb.txt" "BUSY"

# IDLE fixtures
assert_state "idle-prompt.txt" "IDLE"
assert_state "idle-chevron.txt" "IDLE"
assert_state "idle-remote-control.txt" "IDLE"
assert_state "idle-permission.txt" "IDLE"

# UNKNOWN fixtures
assert_state "unknown-blank.txt" "UNKNOWN"
assert_state "unknown-output.txt" "UNKNOWN"

# Extensibility test: custom patterns via env vars
echo ""
echo "--- extensibility tests ---"

total=$((total + 1))
result=$(KEEPALIVE_BUSY_PATTERNS="Compiling" classify_pane "  Compiling main.rs...")
if [ "$result" = "BUSY" ]; then
    passed=$((passed + 1))
    printf "  PASS  %-35s → %s\n" "custom-busy-pattern" "$result"
else
    failed=$((failed + 1))
    printf "  FAIL  %-35s → %s (expected BUSY)\n" "custom-busy-pattern" "$result"
fi

total=$((total + 1))
result=$(KEEPALIVE_IDLE_PATTERNS="Waiting for approval" classify_pane "  Waiting for approval")
if [ "$result" = "IDLE" ]; then
    passed=$((passed + 1))
    printf "  PASS  %-35s → %s\n" "custom-idle-pattern" "$result"
else
    failed=$((failed + 1))
    printf "  FAIL  %-35s → %s (expected IDLE)\n" "custom-idle-pattern" "$result"
fi

echo ""
echo "=== $passed/$total passed, $failed failed ==="

[ "$failed" -eq 0 ]
