#!/bin/bash
# Test harness for dispatch-task.sh.
# Mocks tmux and dispatch.sh to capture envelope formatting.
#
# Usage: bash tests/test_dispatch_task.sh
#   Exit 0 = all pass, exit 1 = failures.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$REPO_DIR/lib/dispatch-task.sh"

passed=0
failed=0
total=0

assert_exit() {
    local label="$1"
    local expected_exit="$2"
    shift 2
    total=$((total + 1))

    local actual_exit=0
    "$@" >/dev/null 2>&1 || actual_exit=$?

    if [ "$actual_exit" -eq "$expected_exit" ]; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
        echo "FAIL: $label — expected exit $expected_exit, got $actual_exit" >&2
    fi
}

assert_contains() {
    local label="$1"
    local haystack="$2"
    local needle="$3"
    total=$((total + 1))
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
        echo "FAIL: $label — expected to find '$needle'" >&2
        echo "  got: $haystack" >&2
    fi
}

assert_not_contains() {
    local label="$1"
    local haystack="$2"
    local needle="$3"
    total=$((total + 1))
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        failed=$((failed + 1))
        echo "FAIL: $label — should NOT contain '$needle'" >&2
        echo "  got: $haystack" >&2
    else
        passed=$((passed + 1))
    fi
}

# --- Mock setup ---------------------------------------------------------------
MOCK_DIR=$(mktemp -d)
trap 'rm -rf "$MOCK_DIR"' EXIT

# Mock tmux — has-session always succeeds
cat > "$MOCK_DIR/tmux" <<'MOCK'
#!/bin/bash
# has-session always succeeds; ignore everything else
exit 0
MOCK
chmod +x "$MOCK_DIR/tmux"

# Mock dispatch.sh — capture the message to a file
CAPTURE_FILE="$MOCK_DIR/dispatched_msg"
cat > "$MOCK_DIR/dispatch.sh" <<MOCK
#!/bin/bash
shift  # skip worker-session
printf '%s' "\$*" > "$CAPTURE_FILE"
MOCK
chmod +x "$MOCK_DIR/dispatch.sh"

# Override LIB_DIR resolution: dispatch-task.sh calls $LIB_DIR/dispatch.sh,
# so we symlink the mock dispatch.sh into a fake lib dir alongside the real
# lib-common.sh.
FAKE_LIB="$MOCK_DIR/lib"
mkdir -p "$FAKE_LIB"
cp "$MOCK_DIR/dispatch.sh" "$FAKE_LIB/dispatch.sh"
ln -s "$REPO_DIR/lib/lib-common.sh" "$FAKE_LIB/lib-common.sh"

# Build a patched copy of dispatch-task.sh that uses our fake LIB_DIR
PATCHED="$MOCK_DIR/dispatch-task.sh"
sed "s|LIB_DIR=.*|LIB_DIR=\"$FAKE_LIB\"|" "$SCRIPT" > "$PATCHED"
chmod +x "$PATCHED"

export PATH="$MOCK_DIR:$PATH"
export TMUX_BIN="$MOCK_DIR/tmux"
export CLAUDLOBBY_ROOT="$MOCK_DIR"

# --- Tests -------------------------------------------------------------------

# 1. Freeform fallback: no metadata flags → raw task text
: > "$CAPTURE_FILE"
BOT_NAME=ari bash "$PATCHED" myworker "Fix the widget" 2>/dev/null
_msg=$(cat "$CAPTURE_FILE")
assert_not_contains "freeform: no BOTCOMMAND prefix" "$_msg" "[BOTCOMMAND]"
assert_contains "freeform: raw task text" "$_msg" "Fix the widget"

# 2. Structured envelope with --repo
: > "$CAPTURE_FILE"
BOT_NAME=ari bash "$PATCHED" --repo Claudlobby myworker "Fix the widget" 2>/dev/null
_msg=$(cat "$CAPTURE_FILE")
assert_contains "envelope: has BOTCOMMAND prefix" "$_msg" "[BOTCOMMAND]"
assert_contains "envelope: has caller" "$_msg" "[BOTCOMMAND] ari |"
assert_contains "envelope: has task" "$_msg" "| task | Fix the widget"
assert_contains "envelope: has repo" "$_msg" "| repo:Claudlobby"

# 3. All metadata flags
: > "$CAPTURE_FILE"
BOT_NAME=ari bash "$PATCHED" --repo Claudlobby --priority high --ref "https://github.com/org/repo/issues/42" myworker "Add feature X" 2>/dev/null
_msg=$(cat "$CAPTURE_FILE")
assert_contains "all flags: BOTCOMMAND" "$_msg" "[BOTCOMMAND] ari | task | Add feature X"
assert_contains "all flags: repo" "$_msg" "| repo:Claudlobby"
assert_contains "all flags: priority" "$_msg" "| priority:high"
assert_contains "all flags: ref" "$_msg" "| ref:https://github.com/org/repo/issues/42"

# 4. Caller falls back to MANAGER_TMUX when BOT_NAME is unset
: > "$CAPTURE_FILE"
env -u BOT_NAME MANAGER_TMUX=mgr-bot bash "$PATCHED" --repo X myworker "Do thing" 2>/dev/null
_msg=$(cat "$CAPTURE_FILE")
assert_contains "caller fallback: MANAGER_TMUX" "$_msg" "[BOTCOMMAND] mgr-bot |"

# 5. Caller is 'unknown' when neither BOT_NAME nor MANAGER_TMUX is set
: > "$CAPTURE_FILE"
env -u BOT_NAME -u MANAGER_TMUX bash "$PATCHED" --repo X myworker "Do thing" 2>/dev/null
_msg=$(cat "$CAPTURE_FILE")
assert_contains "caller fallback: unknown" "$_msg" "[BOTCOMMAND] unknown |"

# 6. --deadline-min still works alongside metadata flags
: > "$CAPTURE_FILE"
BOT_NAME=ari bash "$PATCHED" --deadline-min 10 --repo Claudlobby myworker "Urgent fix" 2>/dev/null
_msg=$(cat "$CAPTURE_FILE")
assert_contains "deadline+repo: has envelope" "$_msg" "[BOTCOMMAND] ari | task | Urgent fix"
assert_contains "deadline+repo: has repo" "$_msg" "| repo:Claudlobby"

# 7. Empty task is rejected
assert_exit "empty task exits 1" \
    1 bash "$PATCHED" myworker

# 8. Missing worker is rejected
assert_exit "missing worker exits 1" \
    1 bash "$PATCHED"

# 9. Unknown flag is rejected
assert_exit "unknown flag exits 1" \
    1 bash "$PATCHED" --bogus myworker "task"

# --- Summary -----------------------------------------------------------------
echo ""
echo "$total tests: $passed passed, $failed failed"
[ "$failed" -eq 0 ] || exit 1
