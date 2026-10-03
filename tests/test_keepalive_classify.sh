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

# classify_pane delegates to lib-common's pane_is_busy/pane_is_idle, so
# lib-common.sh must be sourced first.
# shellcheck source=../claudlobby/_runtime_scripts/lib-common.sh
. "$REPO_DIR/claudlobby/_runtime_scripts/lib-common.sh"

# Source only the classify_pane function from keepalive.sh.
# keepalive.sh runs bot-level setup at the top level, so we extract just
# the function definition.
eval "$(sed -n '/^classify_pane()/,/^}/p' "$REPO_DIR/claudlobby/_runtime_scripts/keepalive.sh")"

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

# assert_not_state <fixture> <state> [locale]: the property is what the frame
# is NOT. Used for the frames that must never read as a held box (#2070),
# whatever else the idle patterns make of them.
assert_not_state() {
    local fixture="$1" unwanted="$2" loc="${3:-}" content actual
    total=$((total + 1))
    content=$(tail -10 "$FIXTURE_DIR/$fixture")
    if [ -n "$loc" ]; then
        actual=$(LC_ALL="$loc" classify_pane "$content")
    else
        actual=$(classify_pane "$content")
    fi
    if [ "$actual" != "$unwanted" ]; then
        passed=$((passed + 1))
        printf "  PASS  %-35s → %s (not %s)%s\n" "$fixture" "$actual" "$unwanted" "${loc:+ [LC_ALL=$loc]}"
    else
        failed=$((failed + 1))
        printf "  FAIL  %-35s → %s (must not be %s)%s\n" "$fixture" "$actual" "$unwanted" "${loc:+ [LC_ALL=$loc]}"
    fi
}

# assert_state_c <fixture> <state>: the same verdict under LC_ALL=C. keepalive
# can run with no UTF-8 locale (a unit whose environment sets none), and there
# grep reads the glyph bracket byte by byte (#2070).
assert_state_c() {
    local fixture="$1" expected="$2" content actual
    total=$((total + 1))
    content=$(tail -10 "$FIXTURE_DIR/$fixture")
    actual=$(LC_ALL=C classify_pane "$content")
    if [ "$actual" = "$expected" ]; then
        passed=$((passed + 1))
        printf "  PASS  %-35s → %s [LC_ALL=C]\n" "$fixture" "$actual"
    else
        failed=$((failed + 1))
        printf "  FAIL  %-35s → %s (expected %s) [LC_ALL=C]\n" "$fixture" "$actual" "$expected"
    fi
}

echo "=== keepalive pane-state classification tests ==="
echo ""

# BUSY fixtures — an active turn always renders the "esc to interrupt"
# affordance; that line (not the spinner or verb) is the BUSY signal.
assert_state "busy-spinner.txt" "BUSY"

# Deliberately-not-BUSY regression: verbs without the esc affordance are
# UNKNOWN (see _BUSY_PATTERN_BASE in lib-common.sh for the rationale).
assert_state "verb-no-esc.txt" "UNKNOWN"

# IDLE fixtures
assert_state "idle-prompt.txt" "IDLE"
assert_state "idle-chevron.txt" "IDLE"
assert_state "idle-remote-control.txt" "IDLE"
assert_state "idle-permission.txt" "IDLE"

# UNKNOWN fixtures
assert_state "unknown-blank.txt" "UNKNOWN"
assert_state "unknown-output.txt" "UNKNOWN"

# HELD fixtures (#2070): text sits in the input box and no turn is running. The
# input-stuck-* frames are earlier stranded sends. input-held-cr and
# input-held-after-enter are a live capture's shape (claude 2.1.285, the
# identifiers replaced): the held text with the swallowed Enter's empty line
# under it, and the same box after a first Enter removed that character
# ("review and press Enter to send"), still held.
assert_state "input-stuck-literal.txt" "HELD"
assert_state "input-stuck-wrapped.txt" "HELD"
assert_state "input-stuck-wrapped-early.txt" "HELD"
assert_state "input-stuck-collapsed-paste.txt" "HELD"
assert_state "input-held-cr.txt" "HELD"
assert_state "input-held-after-enter.txt" "HELD"

# Never HELD (#2070), the negative controls. Claude Code draws its own text in
# the box: the queued-message hint behind a running turn, and an empty box's
# suggestion. A menu's selected option sits on the glyph line too, and an Enter
# there CHOOSES. A check keyed on "the glyph line is not empty", or on a length
# floor, calls all three held; the first did, on a mid-turn bot.
assert_not_state "input-queued-hint.txt" "HELD"
assert_not_state "idle-placeholder.txt" "HELD"
assert_not_state "menu-option.txt" "HELD"
assert_state "input-clean-submit.txt" "IDLE"

# The same, with no UTF-8 locale. Under LC_ALL=C the idle bracket matches the
# box's border bytes, so a held box read IDLE there: a held box still reads
# HELD, and none of the controls does.
assert_state_c "input-stuck-literal.txt" "HELD"
assert_state_c "input-held-cr.txt" "HELD"
assert_state_c "input-held-after-enter.txt" "HELD"
assert_not_state "input-queued-hint.txt" "HELD" C
assert_not_state "idle-placeholder.txt" "HELD" C
assert_not_state "menu-option.txt" "HELD" C

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
