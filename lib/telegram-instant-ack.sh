#!/bin/bash
# telegram-instant-ack.sh — UserPromptSubmit hook for sub-second Telegram ack.
#
# Posts "<bot>: got it — thinking..." the instant an inbound Telegram message
# becomes a prompt, before the model starts thinking. Without this, an
# Opus-max-effort bot can take 60-180s to emit its first token, during which
# the human sees nothing and assumes the bot is broken.
#
# Mirrors the bot's own response logic: in a group, only ack messages
# addressed to this bot (require_mention=true → @handle match required;
# require_mention=false → any message NOT addressed to a different bot).
#
# Exits 0 with no stdout so the prompt reaches the model unchanged.

set -eu

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

if [ -f "$BOT_DIR/bot.conf" ]; then
  set +u
  # shellcheck disable=SC1091
  . "$BOT_DIR/bot.conf"
  set -u
fi

payload="$(cat || true)"

if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

prompt="$(printf '%s' "$payload" | jq -r '.prompt // ""' 2>/dev/null || true)"
[ -n "$prompt" ] || exit 0

case "$prompt" in
  *'source="telegram"'*) : ;;
  *) exit 0 ;;
esac

chat_id="$(printf '%s' "$prompt" | sed -nE 's/.*source="telegram"[^>]*chat_id="([^"]+)".*/\1/p' | head -1)"
[ -n "${chat_id:-}" ] || chat_id="${TELEGRAM_GROUP_CHAT_ID:-}"
[ -n "${chat_id:-}" ] || exit 0

handle="${TELEGRAM_BOT_HANDLE:-}"
require_mention="${TELEGRAM_REQUIRE_MENTION:-true}"

is_dm=true
case "$chat_id" in -*) is_dm=false ;; esac

if [ "$is_dm" = "false" ]; then
  mentions_me=false
  if [ -n "$handle" ] && printf '%s' "$prompt" | grep -qF "@$handle"; then
    mentions_me=true
  fi
  if [ "$require_mention" = "true" ]; then
    [ "$mentions_me" = "true" ] || exit 0
  else
    if [ "$mentions_me" = "false" ] && \
       printf '%s' "$prompt" | grep -qE '@[A-Za-z0-9_]+_bot'; then
      exit 0
    fi
  fi
fi

label="${BOT_NAME:-bot}"
ack="$label: got it — thinking..."

(
  TELEGRAM_GROUP_CHAT_ID="$chat_id" \
  TELEGRAM_STATE_DIR="${TELEGRAM_STATE_DIR:-$HOME/.claude/channels/telegram-${BOT_ID:-unknown}}" \
    "$LIB_DIR/tg-post.sh" "$ack" >/dev/null 2>&1
) &
disown

exit 0
