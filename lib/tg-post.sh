#!/bin/bash
# tg-post.sh — bash fallback for proactive Telegram posts.
#
# Use when:
#   - Worker is dispatched via tmux (no inbound Telegram to "reply" to)
#   - Plugin reply tool is flaking
#   - You need to guarantee Markdown renders (parse_mode=Markdown)
#
# Reads TELEGRAM_BOT_TOKEN from the bot's per-bot channel state dir.
# Posts to TELEGRAM_GROUP_CHAT_ID (env) or the default in bot.conf.
#
# Usage: tg-post.sh "<message>"
set -u
MSG="${1:?Usage: tg-post.sh <message>}"
CHAT_ID="${TELEGRAM_GROUP_CHAT_ID:-}"
STATE_DIR="${TELEGRAM_STATE_DIR:-$HOME/.claude/channels/telegram}"

if [ -z "$CHAT_ID" ]; then
  echo "tg-post: TELEGRAM_GROUP_CHAT_ID not set (export it in bot.conf or env)" >&2
  exit 2
fi

TOKEN=$(grep ^TELEGRAM_BOT_TOKEN "$STATE_DIR/.env" 2>/dev/null | cut -d= -f2)
if [ -z "$TOKEN" ]; then
  echo "tg-post: no TELEGRAM_BOT_TOKEN in $STATE_DIR/.env" >&2
  exit 1
fi

curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${MSG}" \
  -d "parse_mode=Markdown" \
  -d "disable_web_page_preview=true" | jq -r '{ok, msg_id: .result.message_id, error: .description}'
