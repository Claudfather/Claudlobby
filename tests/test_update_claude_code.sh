#!/usr/bin/env bash
# tests/test_update_claude_code.sh — smoke tests for update script
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

echo "=== update-claude-code.sh structure tests ==="

# Test 1: script exists
assert_eq "update script exists" "true" "$([ -f "$LIB_DIR/update-claude-code.sh" ] && echo true || echo false)"

# Test 2: script sources lib-common.sh
assert_eq "sources lib-common.sh" "true" "$(grep -q 'lib-common.sh' "$LIB_DIR/update-claude-code.sh" && echo true || echo false)"

# Test 3: script uses strict mode
assert_eq "uses strict mode" "true" "$(grep -q 'set -euo pipefail' "$LIB_DIR/update-claude-code.sh" && echo true || echo false)"

# Test 4: script detects system vs user install path
assert_eq "detects system vs user install" "true" "$(grep -q '/usr/' "$LIB_DIR/update-claude-code.sh" && echo true || echo false)"

# Test 5: script compares versions before bouncing
assert_eq "compares old vs new version" "true" "$(grep -q 'old_version.*new_version' "$LIB_DIR/update-claude-code.sh" && echo true || echo false)"

# Test 6: download-only — the update script never bounces bots itself
assert_eq "download-only (no bot restart)" "false" "$(grep -q 'spin-up-bot.sh' "$LIB_DIR/update-claude-code.sh" && echo true || echo false)"

echo ""
echo "=== composed host-job spine (system.yaml claude-update) ==="
# The self-generating installer is retired; the unit semantics live in
# system.yaml host.jobs and are emitted by compose_host_timers (also
# asserted by pytest in tests/test_system_defaults.py — CI-enforced there).
SYSTEM_YAML="$SCRIPT_DIR/../claudlobby/system.yaml"

# Test 7: host job declared
assert_eq "system.yaml declares claude-update host job" "true" "$(grep -q 'claude-update:' "$SYSTEM_YAML" && echo true || echo false)"

# Test 8: daily at 04:00
assert_eq "timer fires at 04:00" "true" "$(grep -q '04:00:00' "$SYSTEM_YAML" && echo true || echo false)"

# Test 9: 10min jitter
assert_eq "timer has 10min jitter" "true" "$(grep -q 'randomized_delay: 600' "$SYSTEM_YAML" && echo true || echo false)"

# Test 10: persistent catch-up of missed runs
assert_eq "timer is persistent" "true" "$(grep -q 'persistent: true' "$SYSTEM_YAML" && echo true || echo false)"

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
