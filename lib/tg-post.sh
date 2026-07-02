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
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

MSG="${1:?Usage: tg-post.sh <message>}"
CHAT_ID="${TELEGRAM_GROUP_CHAT_ID:-}"
STATE_DIR="${TELEGRAM_STATE_DIR:-$HOME/.claude/channels/telegram}"
# An unprovisioned channel dir (no .env) must not silently mute the post: fall
# back to the default channel's token. This applies to EVERY caller — the
# message goes out under the default sender identity rather than not at all —
# so leave a breadcrumb when it happens.
if [ ! -f "$STATE_DIR/.env" ] && [ -f "$HOME/.claude/channels/telegram/.env" ]; then
  echo "tg-post: no .env in $STATE_DIR — falling back to the default channel token" >&2
  STATE_DIR="$HOME/.claude/channels/telegram"
fi

if [ -z "$CHAT_ID" ]; then
  echo "tg-post: TELEGRAM_GROUP_CHAT_ID not set (export it in bot.conf or env)" >&2
  exit 2
fi

TOKEN=$(grep ^TELEGRAM_BOT_TOKEN "$STATE_DIR/.env" 2>/dev/null | sed 's/^TELEGRAM_BOT_TOKEN=//' || true)
if [ -z "$TOKEN" ]; then
  echo "tg-post: no TELEGRAM_BOT_TOKEN in $STATE_DIR/.env" >&2
  exit 1
fi

URL_CFG=$(safe_mktemp)
printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TOKEN" > "$URL_CFG"

curl -s -X POST --config "$URL_CFG" \
  -d "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${MSG}" \
  -d "parse_mode=Markdown" \
  -d "disable_web_page_preview=true" | jq -r '{ok, msg_id: .result.message_id, error: .description}'
