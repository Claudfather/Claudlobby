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

# The fleet PATH default orders SYSTEM dirs before the user prefixes — the bug
# was resolving via this script's npm-first PATH (~/.npm-global) while the fleet
# runs /usr/bin/claude, so the update maintained a shadow the fleet never ran.
# (Cheap structural guard; the behavioral ordering proof is (b) below.)
_fleet_line="$(grep -m1 '_FLEET_PATH=' "$LIB_DIR/update-claude-code.sh")"
_usr_pos="$(awk -v s="$_fleet_line" 'BEGIN{print index(s, "/usr/bin")}')"
_npm_pos="$(awk -v s="$_fleet_line" 'BEGIN{print index(s, ".npm-global")}')"
assert_eq "fleet PATH default orders /usr/bin before ~/.npm-global" "true" \
    "$([ "$_usr_pos" -gt 0 ] && [ "$_npm_pos" -gt 0 ] && [ "$_usr_pos" -lt "$_npm_pos" ] && echo true || echo false)"

# --- hermetic behavioral harness --------------------------------------------
# TWO layers of containment (review finding 1 — the old test had exactly one, so
# deleting the resolver's CLAUDE_BIN branch escalated into a REAL global install
# on the reviewer's Pi):
#   1. npm stub in $HOME/.local/bin — update-claude-code.sh rebuilds PATH as
#      "$HOME/.local/bin:$HOME/.npm-global/bin:$_HOMEBREW/bin:$PATH", so a stub
#      anywhere else loses the race to a real (homebrew) npm.
#   2. CLAUDE_UPDATE_FLEET_PATH pinned at an EMPTY dir on the CLAUDE_BIN run — so
#      if the CLAUDE_BIN branch ever regresses, fleet-PATH resolution finds
#      nothing and the run fails CLOSED (no real claude → no sudo → no real npm),
#      instead of falling through to whatever host runs the suite.
# HOME is the throwaway, so both layers are self-contained.
_T="$(mktemp -d)"; trap 'rm -rf "$_T"' EXIT
mkdir -p "$_T/root" "$_T/root2" "$_T/home/.local/bin" "$_T/empty" "$_T/sysbin" "$_T/userbin"
cat > "$_T/home/.local/bin/npm" <<EOF
#!/bin/bash
echo "npm-stub called: \$*" >> "$_T/npm.calls"
exit 0
EOF
chmod +x "$_T/home/.local/bin/npm"
_mkclaude() { printf '#!/bin/bash\necho "%s (Claude Code)"\n' "$2" > "$1"; chmod +x "$1"; }
_mkclaude "$_T/fakeclaude" "9.9.9"
_mkclaude "$_T/sysbin/claude" "1.1.1"
_mkclaude "$_T/userbin/claude" "2.2.2"

# (a) CLAUDE_BIN is honored — the same override start-bot.sh:176 launches with.
#     Replaces the old bare `grep CLAUDE_BIN` decoy, which passed even with the
#     real check deleted because the word also appears in a comment (finding 2).
: > "$_T/npm.calls"
CLAUDLOBBY_ROOT="$_T/root" HOME="$_T/home" CLAUDE_BIN="$_T/fakeclaude" \
    CLAUDE_UPDATE_FLEET_PATH="$_T/empty" \
    bash "$LIB_DIR/update-claude-code.sh" testfleet >/dev/null 2>&1 || true
_log="$_T/root/state/claude-update.log"
assert_eq "CLAUDE_BIN is the resolved target" "true" \
    "$([ -f "$_log" ] && grep -q "target: $_T/fakeclaude" "$_log" && echo true || echo false)"
assert_eq "version read from the CLAUDE_BIN binary (9.9.9)" "true" \
    "$([ -f "$_log" ] && grep -q "current: 9.9.9" "$_log" && echo true || echo false)"
assert_eq "npm install invoked via the stub (no real download)" "true" \
    "$([ -f "$_T/npm.calls" ] && grep -q 'install -g @anthropic-ai/claude-code@latest' "$_T/npm.calls" && echo true || echo false)"

# (b) fleet-PATH ORDERING via a REAL command -v resolution (not a string-index
#     check): CLAUDE_BIN unset, a fake claude in both a "system" and a "user"
#     dir — the resolver must pick the system-first one. This is the behavioral
#     coverage the ordering half of the fix previously lacked.
CLAUDLOBBY_ROOT="$_T/root2" HOME="$_T/home" \
    CLAUDE_UPDATE_FLEET_PATH="$_T/sysbin:$_T/userbin" \
    bash "$LIB_DIR/update-claude-code.sh" testfleet >/dev/null 2>&1 || true
_log2="$_T/root2/state/claude-update.log"
assert_eq "fleet-PATH resolution prefers the system-first binary (1.1.1, not 2.2.2)" "true" \
    "$([ -f "$_log2" ] && grep -q "current: 1.1.1" "$_log2" && grep -q "target: $_T/sysbin/claude" "$_log2" && echo true || echo false)"

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
