#!/usr/bin/env bash
# tests/test_idle_markers.sh — marker-based idle detection tests
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
PASS=0; FAIL=0; TOTAL=0

assert_eq() {
    TOTAL=$((TOTAL + 1))
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc (expected '$expected', got '$actual')"
        FAIL=$((FAIL + 1))
    fi
}

# --- Setup temp bot dir ---
TMPBOT=$(mktemp -d)
mkdir -p "$TMPBOT/data/events"
trap 'rm -rf "$TMPBOT"' EXIT

echo "=== marker_is_newer tests ==="

# Source lib-common for the helper
. "$LIB_DIR/lib-common.sh"

# Test 1: .idle newer than .last-tool-call → idle
touch "$TMPBOT/data/.last-tool-call"
sleep 1
touch "$TMPBOT/data/.idle"
result=$(marker_is_newer "$TMPBOT/data/.idle" "$TMPBOT/data/.last-tool-call" && echo "yes" || echo "no")
assert_eq "idle marker newer → returns true" "yes" "$result"

# Test 2: .last-tool-call newer than .idle → not idle
sleep 1
touch "$TMPBOT/data/.last-tool-call"
result=$(marker_is_newer "$TMPBOT/data/.idle" "$TMPBOT/data/.last-tool-call" && echo "yes" || echo "no")
assert_eq "tool-call marker newer → returns false" "no" "$result"

# Test 3: .idle missing → not idle
rm -f "$TMPBOT/data/.idle"
result=$(marker_is_newer "$TMPBOT/data/.idle" "$TMPBOT/data/.last-tool-call" && echo "yes" || echo "no")
assert_eq "idle marker missing → returns false" "no" "$result"

# Test 4: both missing → not idle (conservative)
rm -f "$TMPBOT/data/.last-tool-call" "$TMPBOT/data/.idle"
result=$(marker_is_newer "$TMPBOT/data/.idle" "$TMPBOT/data/.last-tool-call" && echo "yes" || echo "no")
assert_eq "both markers missing → returns false" "no" "$result"

# Test 5: equal mtime → idle (tie goes to idle — conservative against false alerts)
touch "$TMPBOT/data/.last-tool-call" "$TMPBOT/data/.idle"
result=$(marker_is_newer "$TMPBOT/data/.idle" "$TMPBOT/data/.last-tool-call" && echo "yes" || echo "no")
assert_eq "equal mtime → returns true (tie = idle)" "yes" "$result"

echo ""
echo "=== fleet-pulse activity_stuck marker logic tests ==="

# Simulate the fleet-pulse decision logic as a function
_should_alert_activity_stuck() {
    local bot_dir="$1" threshold="$2"
    local idle_marker="$bot_dir/data/.idle"
    local tool_marker="$bot_dir/data/.last-tool-call"

    # If idle marker is newer, bot is idle — no alert
    if marker_is_newer "$idle_marker" "$tool_marker"; then
        echo "skip"
        return
    fi

    # Check tool-call staleness
    local now_epoch last_epoch gap
    now_epoch=$(date +%s)
    last_epoch=$(stat_mtime "$tool_marker" 2>/dev/null || echo "$now_epoch")
    gap=$(( now_epoch - last_epoch ))
    if [ "$gap" -ge "$threshold" ]; then
        echo "alert"
    else
        echo "skip"
    fi
}

# Test 6: idle marker present and newer → no alert even with stale tool-call
touch "$TMPBOT/data/.last-tool-call"
sleep 1
touch "$TMPBOT/data/.idle"
# Backdate .last-tool-call to simulate staleness
touch -d "2 hours ago" "$TMPBOT/data/.last-tool-call" 2>/dev/null || \
    touch -t "$(date -v-2H +%Y%m%d%H%M.%S 2>/dev/null)" "$TMPBOT/data/.last-tool-call"
result=$(_should_alert_activity_stuck "$TMPBOT" 300)
assert_eq "idle bot with stale tool-call → skip (no false positive)" "skip" "$result"

# Test 7: no idle marker + stale tool-call → alert
rm -f "$TMPBOT/data/.idle"
result=$(_should_alert_activity_stuck "$TMPBOT" 300)
assert_eq "no idle marker + stale tool-call → alert" "alert" "$result"

# Test 8: no idle marker + fresh tool-call → skip
touch "$TMPBOT/data/.last-tool-call"
rm -f "$TMPBOT/data/.idle"
result=$(_should_alert_activity_stuck "$TMPBOT" 300)
assert_eq "no idle marker + fresh tool-call → skip" "skip" "$result"

echo ""
echo "=== classify_pane idle detection tests ==="

# Test classify_pane logic using the same patterns from lib-common.sh
_test_classify_pane() {
    local text="$1"
    local _busy_spinner='[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]'
    local _busy_verbs='(Running|Thinking|Reading|Writing|Editing|Searching|Generating|Pondering)'
    local _busy_pattern="$_busy_spinner|$_busy_verbs"
    local _idle_pattern="$_IDLE_PATTERN_BASE"

    if echo "$text" | grep -qE "$_busy_pattern"; then
        echo "BUSY"
    elif echo "$text" | grep -qE "$_idle_pattern"; then
        echo "IDLE"
    else
        echo "UNKNOWN"
    fi
}

# Test 9: bare prompt glyph
result=$(_test_classify_pane "  ❯  ")
assert_eq "bare prompt glyph → IDLE" "IDLE" "$result"

# Test 10: prompt with box-drawing (real Claude Code TUI)
result=$(_test_classify_pane "╰─ ❯ ")
assert_eq "prompt with box-drawing → IDLE" "IDLE" "$result"

# Test 11: ">" prompt (legacy/fallback)
result=$(_test_classify_pane "> ")
assert_eq "angle-bracket prompt → IDLE" "IDLE" "$result"

# Test 12: spinner present → BUSY
result=$(_test_classify_pane "⠹ Reading file.txt")
assert_eq "spinner → BUSY" "BUSY" "$result"

# Test 13: shell $ prompt
result=$(_test_classify_pane "user@host:~$ ")
assert_eq "shell dollar prompt → IDLE" "IDLE" "$result"

# Test 14: real Claude Code prompt with non-breaking space (U+00A0)
result=$(_test_classify_pane "$(printf '\xe2\x9d\xaf\xc2\xa0')")
assert_eq "real Claude Code prompt (❯ + NBSP) → IDLE" "IDLE" "$result"

# Test 15: Remote Control active on status bar
result=$(_test_classify_pane "  main | +100/-5 | Opus 4.6 | 51%   Remote Control active")
assert_eq "status bar with Remote Control → IDLE" "IDLE" "$result"

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
