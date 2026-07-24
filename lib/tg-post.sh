#!/bin/bash
# tg-post.sh — bash fallback for proactive Telegram posts.
#
# Use when:
#   - Worker is dispatched via tmux (no inbound Telegram to "reply" to)
#   - Plugin reply tool is flaking
#   - An env-less / host-timer job needs to deliver (creds-check, fleet-pulse escalation)
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

# Bot sessions already carry the token in the environment (start-bot.sh
# resolves it via the TELEGRAM_TOKEN_ENV_NAME indirection) — prefer it; the
# channel-dir .env files are the fallback for env-less callers (host timers).
TOKEN="${TELEGRAM_BOT_TOKEN:-$(grep ^TELEGRAM_BOT_TOKEN "$STATE_DIR/.env" 2>/dev/null | sed 's/^TELEGRAM_BOT_TOKEN=//' || true)}"
if [ -z "$TOKEN" ]; then
  echo "tg-post: no TELEGRAM_BOT_TOKEN in $STATE_DIR/.env" >&2
  exit 1
fi

URL_CFG=$(safe_mktemp)
printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TOKEN" > "$URL_CFG"

# Plain text — no parse_mode. Telegram parses Markdown/MarkdownV2 entities only
# when asked; forcing a mode makes the API reject any message with unbalanced
# markup (underscores in identifiers, em-dashes) as "can't parse entities",
# silently dropping the post. Callers that need rich formatting escape for
# MarkdownV2 and pass it themselves (see telegram-formatting protocol).
# Capture the response instead of piping straight to jq. A dead/cross-wired
# token or a bad chat returns HTTP 200 with {"ok":false,...}, so `curl -s`
# succeeds and the delivery failure lives ONLY in the body — piping to jq (whose
# exit became the script's) reported exit 0 on a REJECTED send, so env-less
# callers (creds-check, host timers) logged a false success and the operator was
# never told (#552). Parse `.ok` and exit NON-ZERO on failure so the caller can
# escalate a genuinely undelivered alert instead of trusting a silent drop.
RESP="$(curl -s -X POST --config "$URL_CFG" \
  -d "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${MSG}" \
  -d "disable_web_page_preview=true")" || RESP=""

OK="$(printf '%s' "$RESP" | jq -r '.ok // empty' 2>/dev/null || true)"
if [ "$OK" = "true" ]; then
  printf '%s' "$RESP" | jq -r '{ok, msg_id: .result.message_id}' 2>/dev/null || true
  exit 0
fi

ERR="$(printf '%s' "$RESP" | jq -r '.description // empty' 2>/dev/null || true)"
echo "tg-post: send REJECTED — message NOT delivered (ok=${OK:-<none>}${ERR:+; error: $ERR})" >&2
printf '%s' "$RESP" | jq -r '{ok, error: .description}' 2>/dev/null || printf '%s\n' "${RESP:-<no response>}"
exit 3
