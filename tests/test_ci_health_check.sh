#!/bin/bash
# Test harness for ci-health-check.sh.
# Mocks `gh` to simulate healthy, failing, and empty CI responses.
#
# Usage: bash tests/test_ci_health_check.sh
#   Exit 0 = all pass, exit 1 = failures.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$REPO_DIR/lib/ci-health-check.sh"

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

# --- Mock gh setup -----------------------------------------------------------
MOCK_DIR=$(mktemp -d)
trap 'rm -rf "$MOCK_DIR"' EXIT

# Mock gh that returns canned JSON based on the URL pattern
cat > "$MOCK_DIR/gh" <<'MOCK'
#!/bin/bash
# Parse the URL from args to decide which response to return
for arg in "$@"; do
    case "$arg" in
        repos/healthy/repo/actions/runs*)
            cat <<'JSON'
{"workflow_runs":[
  {"conclusion":"success","name":"CI","html_url":"https://github.com/healthy/repo/actions/runs/1","updated_at":"2026-06-08T10:00:00Z"},
  {"conclusion":"success","name":"Lint","html_url":"https://github.com/healthy/repo/actions/runs/2","updated_at":"2026-06-08T09:00:00Z"}
]}
JSON
            exit 0
            ;;
        repos/failing/repo/actions/runs*)
            cat <<'JSON'
{"workflow_runs":[
  {"conclusion":"success","name":"CI","html_url":"https://github.com/failing/repo/actions/runs/1","updated_at":"2026-06-08T10:00:00Z"},
  {"conclusion":"failure","name":"Lint","html_url":"https://github.com/failing/repo/actions/runs/2","updated_at":"2026-06-08T09:00:00Z"},
  {"conclusion":"success","name":"Lint","html_url":"https://github.com/failing/repo/actions/runs/3","updated_at":"2026-06-08T08:00:00Z"}
]}
JSON
            exit 0
            ;;
        repos/empty/repo/actions/runs*)
            echo '{"workflow_runs":[]}'
            exit 0
            ;;
    esac
done
echo '{"message":"Not Found"}' >&2
exit 1
MOCK
chmod +x "$MOCK_DIR/gh"

export PATH="$MOCK_DIR:$PATH"

# --- Tests -------------------------------------------------------------------

assert_exit "healthy repo exits 0" \
    0 bash "$SCRIPT" --repo healthy/repo --quiet

assert_exit "failing repo exits 1 (lint workflow failing)" \
    1 bash "$SCRIPT" --repo failing/repo --quiet

assert_exit "empty repo (no CI) exits 0" \
    0 bash "$SCRIPT" --repo empty/repo --quiet

assert_exit "failing repo shows workflow name in output" \
    1 bash "$SCRIPT" --repo failing/repo

# Verify the failing output mentions the right workflow
_output=$(bash "$SCRIPT" --repo failing/repo 2>&1 || true)
total=$((total + 1))
if printf '%s' "$_output" | grep -q "Lint: failure"; then
    passed=$((passed + 1))
else
    failed=$((failed + 1))
    echo "FAIL: failing output should mention 'Lint: failure'" >&2
    echo "  got: $_output" >&2
fi

# Verify healthy output reports workflow count
_output=$(bash "$SCRIPT" --repo healthy/repo 2>&1 || true)
total=$((total + 1))
if printf '%s' "$_output" | grep -q "2 workflow(s) passing"; then
    passed=$((passed + 1))
else
    failed=$((failed + 1))
    echo "FAIL: healthy output should report '2 workflow(s) passing'" >&2
    echo "  got: $_output" >&2
fi

# --- Summary -----------------------------------------------------------------
echo ""
echo "$total tests: $passed passed, $failed failed"
[ "$failed" -eq 0 ] || exit 1
