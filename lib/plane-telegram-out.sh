#!/bin/bash
# plane-telegram-out.sh — PostToolUse hook on the telegram plugin's `reply`
# tool (#1402: the OUTBOUND half of the Telegram carrier). The plugin's MCP
# reply path was the plane's blind spot: a bot answering the operator left
# no record while tg-post.sh notices did — the operator's own conversations
# were invisible (operator-flagged with side-by-side evidence).
#
# Claude Code invokes PostToolUse hooks with JSON on stdin: {tool_name,
# tool_input, tool_response, ...}. The composed matcher scopes this to
# mcp__plugin_telegram_telegram__reply; the tool_name gate below is defense
# in depth, not the router. Emits ONE batch: the communication (sender =
# this bot, recipient_raw = the chat id, body per capture policy at the
# emit door) + the carrier_accepted transmission (carrier telegram-bridge —
# the vocabulary's MCP-bridge token; carrier_ref = tg:<message_id> when the
# tool response carries one).
#
# DORMANT (PLANE_EMIT_ENABLED=1, the transcript-digest pattern) and
# NON-BLOCKING: every path exits 0 — a hook must never break a turn.
# edit_message is deliberately NOT recorded in v1: an edit mutates a prior
# send, and a fresh communication row per edit would double-count the
# conversation (disclosed deferral, #1402).

set -u

[ "${PLANE_EMIT_ENABLED:-0}" = "1" ] || exit 0
[ "${PLANE_EMIT_DISABLED:-0}" = "1" ] && exit 0
[ -n "${FLEET_NAME:-}" ] && [ -n "${BOT_ID:-}" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
set +e  # lib-common re-arms set -e at source time; a hook must not die

PAYLOAD="$(cat 2>/dev/null || true)"
[ -n "$PAYLOAD" ] || exit 0

TOOL="$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // empty' 2>/dev/null)"
case "$TOOL" in
  mcp__plugin_telegram_telegram__reply) ;;
  *) exit 0 ;;
esac

CHAT_ID="$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.chat_id // empty' 2>/dev/null)"
TEXT="$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.text // .tool_input.message // empty' 2>/dev/null)"
[ -n "$CHAT_ID" ] && [ -n "$TEXT" ] || exit 0

# the tool response MAY carry the carrier's message id — shapes vary across
# plugin versions, so every candidate path is tried and absence is fine
TG_MSGID="$(printf '%s' "$PAYLOAD" | jq -r '
  .tool_response.message_id
  // .tool_response.result.message_id
  // (.tool_response.content[0].text? // "" | capture("message_id[\"=: ]+(?<id>[0-9]+)").id)?
  // empty' 2>/dev/null || true)"
case "$TG_MSGID" in *[!0-9]*) TG_MSGID="" ;; esac

MSG_ID="$(plane_mint_id msg)" || exit 0
jq -nc --arg fleet "$FLEET_NAME" --arg msg_id "$MSG_ID" \
   --arg sender "bot:$FLEET_NAME/$BOT_ID" \
   --arg dest "$CHAT_ID" --arg body "$TEXT" --arg ref "$TG_MSGID" \
   '{events:([
      {event_type:"communication",emitter:"telegram-hook",fleet:$fleet,
       payload:{msg_id:$msg_id,sender:$sender,recipient_raw:$dest,
                message_class:"chat",body:$body}},
      {event_type:"transmission",emitter:"telegram-hook",fleet:$fleet,
       payload:({msg_id:$msg_id,attempt_no:1,carrier:"telegram-bridge",
                 destination:$dest,state:"carrier_accepted"}
                + (if $ref == "" then {} else {carrier_ref:("tg:"+$ref)} end))}
    ])}' \
  | plane_emit_events telegram-hook >/dev/null 2>&1 || true
exit 0
