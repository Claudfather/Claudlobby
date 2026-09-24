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
# The file is read through the shared restricted parser, which strips one outer
# quote pair as start-bot.sh does when it fills a session's token from the
# tiered .env — a private grep|sed kept the quotes, so a quoted token built a
# bot"<token>" URL and every timer send was a 404 while the bots themselves
# worked (#1771). The telegram plugin's own .env loader keeps a value verbatim,
# so a quoted channel file is still one it could not read. Parsed in a subshell
# so none of the file's other keys reach this env; the parser's skipped-line
# warnings are dropped because they quote the head of the line, which for a
# malformed token line is the token; and its failure on an unreadable file is a
# missing token (the verdict below), never a script_error.
# shellcheck disable=SC2030,SC2031  # subshell-local by design: never touch this env
TOKEN="${TELEGRAM_BOT_TOKEN:-$(TELEGRAM_BOT_TOKEN=; parse_env_file "$STATE_DIR/.env" 2>/dev/null || true; printf '%s' "${TELEGRAM_BOT_TOKEN:-}")}"
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
# succeeds and the delivery failure lives ONLY in the body — an HTTP 200 does
# NOT imply delivery. Piping to jq (its exit becoming the script exit) surfaces
# exit 0 on a REJECTED send, so env-less callers (creds-check, host timers) log
# a false success on an alert that never went out. Parse `.ok` and exit NON-ZERO
# on failure so the caller can escalate a genuinely undelivered alert instead of
# trusting a silent drop.
# --- observable-plane record (PR-B T6; disclosed, non-blocking) -----------
# Always on (PLANE_EMIT_DISABLED=1 is the one silencer) when this caller has a
# bot identity (host timers have no FLEET_NAME/BOT_NAME and skip naturally).
# The sender alias prefers BOT_ID -- the alias every plane door anchors on --
# falling back to BOT_NAME for a hand caller (bot-sweep-cron.sh) that sets
# only that; the plane_armed --require-bot gate below still keys on BOT_NAME,
# which every session exports beside BOT_ID.
# Intent BEFORE the send (F9); outcome-typed transmission after — telegram
# carrier semantics per §7: API ok=true is carrier_accepted (acceptance, not
# delivery), a rejected/empty response is failed.
PLANE_ARMED=0
if plane_armed tg-post --require-fleet --require-bot; then
  PLANE_ARMED=1
fi
PLANE_MSG_ID=""
if [ "$PLANE_ARMED" = "1" ]; then
  PLANE_MSG_ID="$(plane_mint_id msg)"
  # ONE jq -nc per emission (gauntlet round, measured): the prior per-string
  # jq -Rs escaper spawned jq 4x here — ~60-120ms of pure spawn overhead per
  # armed post on the Pi, on the ALERTING path. --arg is also safer than
  # printf interpolation: jq owns every escape (the F14 tab class included)
  # by construction. jq stays because it is already this script's hard dep.
  jq -nc --arg fleet "$FLEET_NAME" --arg msg_id "$PLANE_MSG_ID" \
     --arg sender "bot:$FLEET_NAME/${BOT_ID:-$BOT_NAME}" \
     --arg dest "$CHAT_ID" --arg body "$MSG" \
     '{events:[{event_type:"communication",emitter:"tg-post",fleet:$fleet,payload:{msg_id:$msg_id,sender:$sender,recipient_raw:$dest,message_class:"notice",body:$body}}]}' \
    | plane_emit_events tg-post || true
fi

RESP="$(curl -s -X POST --config "$URL_CFG" \
  -d "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${MSG}" \
  -d "disable_web_page_preview=true")" || RESP=""

# `.ok // empty` would turn a definitive ok:false into nothing, and the
# rejection line below would read ok=<none>: keep false as false.
OK="$(printf '%s' "$RESP" | jq -r 'if type == "object" and has("ok") then (.ok | tostring) else empty end' 2>/dev/null || true)"
if [ "$OK" = "true" ]; then
  if [ "$PLANE_ARMED" = "1" ]; then
    TG_MSGID="$(printf '%s' "$RESP" | jq -r '.result.message_id // empty' 2>/dev/null || true)"
    # Digits-only gate (gauntlet round): TG_MSGID is jq -r output from the
    # carrier's response — interpolating a non-numeric value into the ref
    # would corrupt the batch; a message_id is always an integer.
    case "$TG_MSGID" in *[!0-9]*) TG_MSGID="" ;; esac
    jq -nc --arg fleet "$FLEET_NAME" --arg msg_id "$PLANE_MSG_ID" \
       --arg dest "$CHAT_ID" --arg ref "$TG_MSGID" \
       '{events:[{event_type:"transmission",emitter:"tg-post",fleet:$fleet,payload:({msg_id:$msg_id,attempt_no:1,carrier:"telegram-tgpost",destination:$dest,state:"carrier_accepted"} + (if $ref == "" then {} else {carrier_ref:("tg:"+$ref)} end))}]}' \
      | plane_emit_events tg-post || true
  fi
  printf '%s' "$RESP" | jq -r '{ok, msg_id: .result.message_id}' 2>/dev/null || true
  exit 0
fi

ERR="$(printf '%s' "$RESP" | jq -r '.description // empty' 2>/dev/null || true)"
if [ "$PLANE_ARMED" = "1" ]; then
  jq -nc --arg fleet "$FLEET_NAME" --arg msg_id "$PLANE_MSG_ID" \
     --arg dest "$CHAT_ID" --arg err "${ERR:-rejected}" \
     '{events:[{event_type:"transmission",emitter:"tg-post",fleet:$fleet,payload:{msg_id:$msg_id,attempt_no:1,carrier:"telegram-tgpost",destination:$dest,state:"failed",error:$err}}]}' \
    | plane_emit_events tg-post || true
fi
echo "tg-post: send REJECTED — message NOT delivered (ok=${OK:-<none>}${ERR:+; error: $ERR})" >&2
printf '%s' "$RESP" | jq -r '{ok, error: .description}' 2>/dev/null || printf '%s\n' "${RESP:-<no response>}"
exit 3
