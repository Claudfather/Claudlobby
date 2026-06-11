#!/bin/bash
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"
. "$LIB_DIR/lib-common.sh"

# Create a temp bot dir with a mock bot.conf
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

cat > "$tmpdir/bot.conf" <<'CONF'
export BOT_SERVICE=com.test.eng.alpha
BOT_NAME=alpha
export SOME_VAR="hello world"
CONF

# Test 1: reads exported var
result=$(bot_conf_get "$tmpdir" BOT_SERVICE "fallback")
[ "$result" = "com.test.eng.alpha" ] || { echo "FAIL: BOT_SERVICE got '$result'"; exit 1; }

# Test 2: reads non-exported var
result=$(bot_conf_get "$tmpdir" BOT_NAME "fallback")
[ "$result" = "alpha" ] || { echo "FAIL: BOT_NAME got '$result'"; exit 1; }

# Test 3: returns default when key missing
result=$(bot_conf_get "$tmpdir" NONEXISTENT "fallback")
[ "$result" = "fallback" ] || { echo "FAIL: default got '$result'"; exit 1; }

# Test 4: reads quoted value
result=$(bot_conf_get "$tmpdir" SOME_VAR "fallback")
[ "$result" = "hello world" ] || { echo "FAIL: SOME_VAR got '$result'"; exit 1; }

# Test 5: missing bot.conf returns default
result=$(bot_conf_get "/nonexistent/path" BOT_SERVICE "fallback")
[ "$result" = "fallback" ] || { echo "FAIL: missing conf got '$result'"; exit 1; }

echo "PASS: all bot_conf_get tests passed"
