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
export MANAGER_TMUX=alpha  # this bot is a manager
export QUOTED_HASH='a # b'
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

# Test 6: a manager bot.conf composed with the inline comment on its
# MANAGER_TMUX line (still on disk until a regenerate) reads back as the bare
# session name. It read as `alpha  # this bot is a manager` -- a session that
# does not exist -- so every FLEET ALERT / NOTICE skipped the manager pane (#910).
result=$(bot_conf_get "$tmpdir" MANAGER_TMUX "fallback")
[ "$result" = "alpha" ] || { echo "FAIL: MANAGER_TMUX got '$result'"; exit 1; }

# Test 7: CONTROL -- a hash inside quotes is data, not a comment. A strip that
# fires on every hash (the naive fix) would pass Test 6 and fail here.
result=$(bot_conf_get "$tmpdir" QUOTED_HASH "fallback")
[ "$result" = "a # b" ] || { echo "FAIL: QUOTED_HASH got '$result'"; exit 1; }

echo "PASS: all bot_conf_get tests passed"
