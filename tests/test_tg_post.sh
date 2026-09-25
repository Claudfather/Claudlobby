#!/usr/bin/env bash
# tests/test_tg_post.sh — tg-post delivery-outcome contract.
# Real jq + a stub curl (emits a canned API body): asserts tg-post exits 0 on
# ok:true and NON-ZERO on a rejected / empty / non-JSON send, so env-less callers
# (creds-check, host timers) stop logging a false success on a dead token.
# Standalone bash (not pytest-collected); runs under macOS /bin/bash (3.2).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
PASS=0; FAIL=0; TOTAL=0
assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS + 1)); else echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1)); fi
}

command -v jq >/dev/null 2>&1 || { echo "SKIP: jq not installed"; exit 0; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export CLAUDLOBBY_ROOT="$T"
mkdir -p "$T/bin"
# Stub curl: emit $CURL_STUB_JSON, exit $CURL_STUB_RC (default 0) — models the
# API body without a network call. When CURL_URL_CAPTURE is set it also records
# the url line of the --config file tg-post passes (the token rides there, never
# argv).
cat > "$T/bin/curl" <<'STUB'
#!/bin/bash
cfg=""
while [ $# -gt 0 ]; do
    if [ "$1" = "--config" ]; then cfg="$2"; fi
    shift
done
if [ -n "${CURL_URL_CAPTURE:-}" ] && [ -n "$cfg" ]; then
    grep '^url' "$cfg" > "$CURL_URL_CAPTURE"
fi
printf '%s' "$CURL_STUB_JSON"
exit "${CURL_STUB_RC:-0}"
STUB
chmod +x "$T/bin/curl"

run_tg() {  # $1=api-json  $2=curl-exit(default 0) → echoes tg-post's exit code
    local rc=0
    PATH="$T/bin:$PATH" CURL_STUB_JSON="$1" CURL_STUB_RC="${2:-0}" \
        TELEGRAM_BOT_TOKEN="123:ABC" TELEGRAM_GROUP_CHAT_ID="-100999" \
        bash "$LIB_DIR/tg-post.sh" "hello world" >"$T/out" 2>"$T/err" || rc=$?
    echo "$rc"
}

echo "=== tg-post delivery-outcome contract ==="

# 1) ok:true → exit 0, message_id surfaced
rc="$(run_tg '{"ok":true,"result":{"message_id":42}}')"
assert_eq "ok:true → exit 0" "0" "$rc"
assert_eq "ok:true surfaces the message_id" "true" "$(grep -q 42 "$T/out" && echo true || echo false)"

# 2) ok:false (dead/cross-wired token) → exit 3, LOUD on stderr. An HTTP 200 with
#    ok:false is a REJECTED send, not delivery — without the .ok check it read as
#    a silent success (curl 200 + jq exit 0) on a dropped alert.
rc="$(run_tg '{"ok":false,"error_code":401,"description":"Unauthorized"}')"
assert_eq "ok:false → exit 3 (not a silent 0)" "3" "$rc"
assert_eq "ok:false is loud (REJECTED on stderr)" "true" "$(grep -q 'REJECTED' "$T/err" && echo true || echo false)"
assert_eq "ok:false surfaces the API error text" "true" "$(grep -q 'Unauthorized' "$T/err" && echo true || echo false)"

# 3) empty response → exit 3 (fail closed)
rc="$(run_tg '')"
assert_eq "empty response → exit 3" "3" "$rc"

# 4) non-JSON body (e.g. an HTML 502) → exit 3
rc="$(run_tg '<html>502 Bad Gateway</html>')"
assert_eq "non-JSON response → exit 3" "3" "$rc"

# 5) curl network failure (nonzero exit) → exit 3
rc="$(run_tg '' 7)"
assert_eq "curl network failure → exit 3" "3" "$rc"

echo ""
echo "=== #1771: the channel-file token is read through the shared parser ==="
# Env-less callers (host timers) read the token from the channel dir's .env. A
# private grep|sed kept a quoted value's quotes, so tg-post built
# bot"<token>" and Telegram answered 404 on every timer send, while the bots
# worked (start-bot.sh fills a session's token through the shared parser). These
# are the positive control that fails on that reader.
run_tg_file() {  # $1=the channel .env token line  $2=env token (optional) → the url line built
    rm -rf "$T/chan" "$T/url"; mkdir -p "$T/chan" "$T/home"
    printf '%s\n' "$1" > "$T/chan/.env"
    env -u TELEGRAM_BOT_TOKEN PATH="$T/bin:$PATH" HOME="$T/home" \
        CURL_STUB_JSON='{"ok":true,"result":{"message_id":7}}' CURL_URL_CAPTURE="$T/url" \
        TELEGRAM_STATE_DIR="$T/chan" TELEGRAM_GROUP_CHAT_ID="-100999" ${2:+TELEGRAM_BOT_TOKEN=$2} \
        bash "$LIB_DIR/tg-post.sh" "hello" >/dev/null 2>"$T/err" || true
    cat "$T/url" 2>/dev/null || echo "<no url captured>"
}
WANT='url = "https://api.telegram.org/bot123:ABC/sendMessage"'
assert_eq "double-quoted token file → no quote in the URL" "$WANT" "$(run_tg_file 'TELEGRAM_BOT_TOKEN="123:ABC"')"
assert_eq "unquoted token file → the same URL" "$WANT" "$(run_tg_file 'TELEGRAM_BOT_TOKEN=123:ABC')"
assert_eq "an env token still wins over the file" 'url = "https://api.telegram.org/bot999:ENV/sendMessage"' \
    "$(run_tg_file 'TELEGRAM_BOT_TOKEN="123:ABC"' '999:ENV')"
# The parser warns about a line it rejects by quoting the line's head; for a
# token line that is the token, and tg-post's stderr lands in
# alert_delivery_failed.detail and the journal.
rm -rf "$T/chan"; mkdir -p "$T/chan"
printf 'TELEGRAM_BOT_TOKEN="123:ABC";\n' > "$T/chan/.env"
env -u TELEGRAM_BOT_TOKEN PATH="$T/bin:$PATH" HOME="$T/home" TELEGRAM_STATE_DIR="$T/chan" \
    TELEGRAM_GROUP_CHAT_ID=-100999 bash "$LIB_DIR/tg-post.sh" hello >/dev/null 2>"$T/err" || true
assert_eq "a rejected token line never quotes the token on stderr" "false" "$(grep -q '123:ABC' "$T/err" && echo true || echo false)"

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
