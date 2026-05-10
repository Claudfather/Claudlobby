#!/bin/bash
# Test harness for keepalive.sh pane-state classification.
# Runs each fixture through the classification logic and checks the result.
#
# Usage: bash tests/test_keepalive_classify.sh
#   Exit 0 = all pass, exit 1 = failures.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURE_DIR="$SCRIPT_DIR/fixtures/pane-states"

passed=0
failed=0
total=0

classify() {
    local last_lines="$1"
    local busy_pattern="${KEEPALIVE_BUSY_PATTERNS:-}"
    local idle_pattern="${KEEPALIVE_IDLE_PATTERNS:-}"

    # Mirror keepalive.sh classification logic
    _busy_spinner='[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]'
    _busy_verbs='(Running|Thinking|Reading|Writing|Editing|Searching|Generating|Pondering)'
    _busy_pattern="$_busy_spinner|$_busy_verbs"
    if [ -n "$busy_pattern" ]; then
        _busy_pattern="$_busy_pattern|$busy_pattern"
    fi

    _idle_pattern='(^\s*[>❯]\s*$|Remote Control active|Enter\/Esc to close|Yes\/No|Allow|Deny|y\/n\b)'
    if [ -n "$idle_pattern" ]; then
        _idle_pattern="$_idle_pattern|$idle_pattern"
    fi

    if echo "$last_lines" | grep -qE "$_busy_pattern"; then
        echo "BUSY"
    elif echo "$last_lines" | grep -qE "$_idle_pattern"; then
        echo "IDLE"
    else
        echo "UNKNOWN"
    fi
}

assert_state() {
    local fixture="$1"
    local expected="$2"
    total=$((total + 1))

    local content
    content=$(tail -10 "$FIXTURE_DIR/$fixture")
    local actual
    actual=$(classify "$content")

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

# Extensibility test: custom busy pattern via env var
echo ""
echo "--- extensibility tests ---"
KEEPALIVE_BUSY_PATTERNS="Compiling" \
    last_lines="  Compiling main.rs..." \
    total=$((total + 1))
result=$(KEEPALIVE_BUSY_PATTERNS="Compiling" classify "  Compiling main.rs...")
if [ "$result" = "BUSY" ]; then
    passed=$((passed + 1))
    printf "  PASS  %-35s → %s\n" "custom-busy-pattern" "$result"
else
    failed=$((failed + 1))
    printf "  FAIL  %-35s → %s (expected BUSY)\n" "custom-busy-pattern" "$result"
fi

KEEPALIVE_IDLE_PATTERNS="Waiting for approval" \
    total=$((total + 1))
result=$(KEEPALIVE_IDLE_PATTERNS="Waiting for approval" classify "  Waiting for approval")
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
