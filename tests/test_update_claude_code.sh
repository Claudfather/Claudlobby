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
echo "=== #635: targets the binary the FLEET launches, not this script's PATH ==="

# Test 7: the fleet PATH orders SYSTEM dirs before the user prefixes — the bug
# was resolving via this script's npm-first PATH (~/.npm-global) while the fleet
# runs /usr/bin/claude, so the update maintained a shadow the fleet never ran.
_fleet_line="$(grep -m1 '_FLEET_PATH=' "$LIB_DIR/update-claude-code.sh")"
_usr_pos="$(awk -v s="$_fleet_line" 'BEGIN{print index(s, "/usr/bin")}')"
_npm_pos="$(awk -v s="$_fleet_line" 'BEGIN{print index(s, ".npm-global")}')"
assert_eq "fleet PATH puts /usr/bin before ~/.npm-global" "true" \
    "$([ "$_usr_pos" -gt 0 ] && [ "$_npm_pos" -gt 0 ] && [ "$_usr_pos" -lt "$_npm_pos" ] && echo true || echo false)"

# Test 8: resolution honors CLAUDE_BIN (the same override start-bot.sh launches
# with) so the updater and the launcher agree on the fleet's binary.
assert_eq "resolver honors CLAUDE_BIN" "true" \
    "$(grep -q 'CLAUDE_BIN' "$LIB_DIR/update-claude-code.sh" && echo true || echo false)"

# Test 9 (behavioral): drive the real script against a fake binary + npm stub
# and confirm it (a) targets THAT binary and (b) reads its version — proving the
# update operates on the fleet's binary, not whatever its own PATH resolves.
_T="$(mktemp -d)"
trap 'rm -rf "$_T"' EXIT
# The npm stub MUST live in $HOME/.local/bin: update-claude-code.sh rebuilds
# PATH as "$HOME/.local/bin:$HOME/.npm-global/bin:$_HOMEBREW/bin:$PATH", so a
# stub anywhere else loses the race to a real (homebrew) npm and the test would
# fire a real global install. HOME points at the throwaway so this is hermetic.
mkdir -p "$_T/root" "$_T/home/.local/bin"
cat > "$_T/fakeclaude" <<'EOF'
#!/bin/bash
echo "9.9.9 (Claude Code)"
EOF
chmod +x "$_T/fakeclaude"
# npm stub: record the invocation, no-op success (NO real download).
cat > "$_T/home/.local/bin/npm" <<EOF
#!/bin/bash
echo "npm-stub called: \$*" >> "$_T/npm.calls"
exit 0
EOF
chmod +x "$_T/home/.local/bin/npm"

CLAUDLOBBY_ROOT="$_T/root" HOME="$_T/home" CLAUDE_BIN="$_T/fakeclaude" \
    bash "$LIB_DIR/update-claude-code.sh" testfleet >/dev/null 2>&1 || true

_log="$_T/root/state/claude-update.log"
assert_eq "logged the fleet binary as the target" "true" \
    "$([ -f "$_log" ] && grep -q "target: $_T/fakeclaude" "$_log" && echo true || echo false)"
assert_eq "read the version from the fleet binary (9.9.9)" "true" \
    "$([ -f "$_log" ] && grep -q "current: 9.9.9" "$_log" && echo true || echo false)"
assert_eq "invoked npm to install (no-op stub)" "true" \
    "$([ -f "$_T/npm.calls" ] && grep -q 'install -g @anthropic-ai/claude-code@latest' "$_T/npm.calls" && echo true || echo false)"

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
