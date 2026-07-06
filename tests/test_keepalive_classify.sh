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

# classify_pane consumes the shared pattern bases (_BUSY_PATTERN_BASE /
# _IDLE_PATTERN_BASE), so lib-common.sh must be sourced first — the bare
# extraction crashes under set -u (latent since the idle-pattern SSOT
# landed; surfaced when the busy pattern joined it in lib-common).
# shellcheck source=../lib/lib-common.sh
. "$REPO_DIR/lib/lib-common.sh"

# Source only the classify_pane function from keepalive.sh.
# keepalive.sh runs bot-level setup at the top level, so we extract just
# the function definition.
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

# BUSY fixtures — an active turn always renders the "esc to interrupt"
# affordance; that line (not the spinner or verb) is the BUSY signal.
assert_state "busy-spinner.txt" "BUSY"

# Deliberately-not-BUSY regression: a verb-painted pane WITHOUT the esc
# affordance is UNKNOWN, not BUSY — the churning verb list was dropped as a
# busy signal by marker-first liveness (#472); marker recency covers it.
# Guards against any consumer resurrecting the verb regex.
assert_state "verb-no-esc.txt" "UNKNOWN"

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
