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
if [ -z "${FLEET_NAME:-}" ] || [ -z "${BOT_ID:-}" ]; then
  echo "plane-telegram-out: armed but FLEET_NAME/BOT_ID unset — not recording" >&2
  exit 0
fi
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
TEXT="$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.text // empty' 2>/dev/null)"
[ -n "$CHAT_ID" ] && [ -n "$TEXT" ] || exit 0

# The carrier ref. GROUND TRUTH (r3, probed against the installed plugin,
# telegram@0.0.7): the reply tool's success text is `sent (id: 12345)` /
# `sent 2 parts (ids: 12345, 12346)` — it NEVER says message_id, so the
# live rung captures the real string (first id = the head of the reply).
# The structured .message_id walk and the message_id-string capture stay
# as shape-drift tolerance only. Every rung walks `..`-recursively — real
# MCP hook responses arrive OBJECT- or ARRAY-shaped across plugin
# versions, and the first version's fixed paths hard-errored on the array
# shape (gauntlet, measured).
TG_MSGID="$(printf '%s' "$PAYLOAD" | jq -r '
  first(
    (.tool_response? | .. | strings
       | (capture("sent(?: [0-9]+ parts)? \\(ids?: (?<id>[0-9]+)").id)?
         // empty),
    (.tool_response? | .. | .message_id? // empty),
    (.tool_response? | .. | strings
       | (capture("message_id[\"=: ]+(?<id>[0-9]+)").id)? // empty)
  ) // empty' 2>/dev/null || true)"
case "$TG_MSGID" in *[!0-9]*) TG_MSGID="" ;; esac

# The transmission state is a CARRIER-API FACT (contracts: carrier_accepted
# means the carrier took it) — recording accepted on a FAILED send is a
# false claim (r1 MAJOR, probed). Detection is SHAPE-GUARDED and
# structured-first (r2 MAJOR, probed: `.isError?` on an ARRAY response
# yields empty, and jq's if-with-empty-condition evaluates NO branch — the
# r1 fix was dead on the exact shape it certified as real). The tree is
# walked ONCE and each rung reads its own slice (r3 dedup, pin-equivalent):
#   1. any object anywhere carrying isError:true  -> failed (strings joined)
#      — THE LIVE RUNG: the installed plugin (telegram@0.0.7, probed) fails
#      as {content:[...], isError:true} with text `reply failed after N of
#      M chunk(s) sent: ...`, delivered whole per the CC hook docs
#   2. any object carrying isError:false          -> accepted (explicit)
#   3. any object carrying an error field         -> failed (that text)
#   4. a BARE-ARRAY response with ANY string starting "Error"/"Telegram
#      error"/"reply failed" (the real plugin prefix, r3) -> failed —
#      arrays only by design: an object-wrapped success that merely ECHOES
#      error-ish text (a bot relaying an alert) must stay accepted (r2
#      MEDIUM, probed false-FAILED); and ANY string, never just the first —
#      an array item's first string is its `type` field's value (r2 pin)
TG_ERR="$(printf '%s' "$PAYLOAD" | jq -r '
  [.tool_response? | ..] as $nodes
  | ([$nodes[] | objects | select(has("isError")) | .isError]) as $flags
  | ([$nodes[] | objects | .error? | select(. != null)]) as $errs
  | ([$nodes[] | strings
      | select(test("^(Error|Telegram error|reply failed)"))]) as $anchored
  | if ($flags | any(. == true)) then
      ([$nodes[] | strings] | join(" ") | .[0:200])
    elif ($flags | length) > 0 then empty
    elif ($errs | length) > 0 then ($errs | first | tostring | .[0:200])
    elif ((.tool_response? | type) == "array") and (($anchored | length) > 0)
    then ($anchored | first | .[0:200])
    else empty end' 2>/dev/null || true)"

MSG_ID="$(plane_mint_id msg)" || exit 0
jq -nc --arg fleet "$FLEET_NAME" --arg msg_id "$MSG_ID" \
   --arg sender "bot:$FLEET_NAME/$BOT_ID" \
   --arg dest "$CHAT_ID" --arg body "$TEXT" --arg ref "$TG_MSGID" \
   --arg err "$TG_ERR" \
   '{events:([
      {event_type:"communication",emitter:"telegram-hook",fleet:$fleet,
       payload:{msg_id:$msg_id,sender:$sender,recipient_raw:$dest,
                message_class:"chat",body:$body}},
      {event_type:"transmission",emitter:"telegram-hook",fleet:$fleet,
       payload:({msg_id:$msg_id,attempt_no:1,carrier:"telegram-bridge",
                 destination:$dest}
                + (if $err == ""
                   then ({state:"carrier_accepted"}
                         + (if $ref == "" then {}
                            else {carrier_ref:("tg:"+$ref)} end))
                   else {state:"failed",error:$err} end))}
    ])}' \
  | plane_emit_events telegram-hook || true
exit 0
