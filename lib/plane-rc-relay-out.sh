#!/bin/bash
# plane-rc-relay-out.sh — Stop hook (#1412): the OUTBOUND Telegram half the
# reply-tool hook cannot see. When the operator messages a bot and the bot
# answers with plain final text (no `reply` tool call), the bridge's RC
# auto-relay delivers that text to Telegram — and no PostToolUse fires, so
# the conversation rendered one-sided. Estate census (2026-09-02, 197
# transcripts): 685 Telegram-initiated turns, 622 via the reply tool
# (recorded), 20 genuine RC-relayed answers — and 26 that ENDED IN AN API
# ERROR ("You've hit your weekly limit…"), which a naive door would have
# recorded as the bot's reply. Every rule below is from that capture:
#
#   1. channel-initiated: the turn's prompt entry carries the plugin's
#      structured origin {kind: channel, server: …telegram} and the
#      <channel …> tag (chat_id/message_id parsed with the inbound hook's
#      recognizer) — anything else is not this door's turn;
#   2. NOT already recorded: no mcp__plugin_telegram_telegram__reply
#      tool_use in the turn (that path is the PostToolUse hook's);
#   3. a FINAL answer only: the last assistant entry has stop_reason
#      end_turn and a non-empty text block, and is not an API-error entry
#      (isApiErrorMessage / error / apiErrorStatus) — 9 of the 20 RC turns
#      ended in tool_use (interstitial narration), never a delivered reply;
#   4. carrier state told HONESTLY: carrier telegram-bridge, state
#      `unknown` — this hook cannot observe the bridge accept or deliver;
#      it records that a relayable final answer was produced, no more;
#   5. dedupe on the assistant entry's uuid (a marker under $BOT_DIR/data),
#      so a re-fired Stop never double-records; stdout EMPTY every path;
#      dormant on PLANE_EMIT_ENABLED; exit 0 every path.
#
# The PROGRAM rides argv (-c) and the hook payload rides stdin (the #1402
# lesson: `python3 -` eats a piped payload); the transcript is read by
# PATH inside python, so size never meets ARG_MAX.

set -uo pipefail
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
set +e

[ "${PLANE_EMIT_ENABLED:-0}" = "1" ] || exit 0
if [ -z "${FLEET_NAME:-}" ] || [ -z "${BOT_ID:-}" ]; then
    echo "plane-rc-relay-out: armed but FLEET_NAME/BOT_ID unset — not recording" >&2
    exit 0
fi
if [ -z "${BOT_DIR:-}" ]; then
    # never default the marker dir to cwd: a bot's cwd is its project
    # checkout, and telemetry written there is the #874 class (gauntlet)
    echo "plane-rc-relay-out: armed but BOT_DIR unset — not recording" >&2
    exit 0
fi
PAYLOAD="$(cat 2>/dev/null || true)"
[ -n "$PAYLOAD" ] || exit 0
MARKER_DIR="$BOT_DIR/data"

IFS= read -r -d '' PYPROG <<'PYEOF' || true
import json, os, re, sys
fleet, bot, marker_dir, msg_id = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    payload = json.loads(sys.stdin.read() or "{}")
except ValueError:
    sys.exit(0)
tp = payload.get("transcript_path") or ""
if not tp or not os.path.isfile(tp):
    sys.exit(0)
# The turn is always at the END of the transcript, so read a bounded TAIL
# and widen only until a prompt entry appears — never the whole file. A
# whole-file parse measured 422 MB peak RSS on a 62 MB transcript at every
# turn end of every armed bot (gauntlet), a half-gigabyte transient the Pi
# cannot afford. Cap: 32 MB.
def _tail_entries(path, start_mb=2, cap_mb=32):
    size = os.path.getsize(path)
    window = start_mb * 1024 * 1024
    while True:
        offset = max(0, size - window)
        with open(path, "rb") as fh:
            fh.seek(offset)
            raw = fh.read().decode("utf-8", errors="replace")
        lines = raw.splitlines()
        if offset > 0 and lines:
            lines = lines[1:]          # drop the partial first line
        ents = []
        for ln in lines:
            try:
                ents.append(json.loads(ln))
            except ValueError:
                ents.append(None)
        has_prompt = any(e and e.get("type") == "user" and not _is_tool_result(e)
                         for e in ents)
        if has_prompt or offset == 0 or window >= cap_mb * 1024 * 1024:
            return ents
        window *= 4

def _is_tool_result(e):
    c = (e.get("message") or {}).get("content")
    return isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c)

try:
    ents = _tail_entries(tp)
except OSError:
    sys.exit(0)
# 1. the turn's prompt entry: the last user entry that is not a tool_result.
# KNOWN MISS CLASS (gauntlet, source-derived, unchanged until a live capture
# per the r4 rule): a skill expansion or a task-notification lands as a
# LATER user entry inside the same operator turn, so this rule reads it as
# the prompt and skips the turn — a miss, never a false record.
idx = None
for i in range(len(ents) - 1, -1, -1):
    e = ents[i]
    if e and e.get("type") == "user" and not _is_tool_result(e):
        idx = i
        break
if idx is None:
    sys.exit(0)
u = ents[idx]
origin = u.get("origin") if isinstance(u.get("origin"), dict) else {}
c = (u.get("message") or {}).get("content")
text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text")
TAG = re.compile(r"<channel\s+([^>]*(?<![\w-])source=\"(?:[^\"]*:)?telegram\"[^>]*)>")
m = TAG.search(text)
if not (origin.get("kind") == "channel" and "telegram" in str(origin.get("server", ""))) and not m:
    sys.exit(0)
attrs = dict(re.findall(r"([a-z_]+)=\"([^\"]*)\"", m.group(1))) if m else {}
chat = attrs.get("chat_id") or ""
if not chat:
    sys.exit(0)
# 2./3. walk the turn
last = None
for e in ents[idx + 1:]:
    if not e:
        continue
    if e.get("type") == "user" and not _is_tool_result(e):
        break
    if e.get("type") != "assistant":
        continue
    content = (e.get("message") or {}).get("content") or []
    for x in content:
        if isinstance(x, dict) and x.get("type") == "tool_use" and x.get("name") == "mcp__plugin_telegram_telegram__reply":
            sys.exit(0)   # the PostToolUse hook owns this turn
    if any(isinstance(x, dict) and x.get("type") == "text" and (x.get("text") or "").strip() for x in content):
        last = e
if not last:
    sys.exit(0)
msg = last.get("message") or {}
if msg.get("stop_reason") != "end_turn":
    sys.exit(0)
if last.get("isApiErrorMessage") or last.get("error") or last.get("apiErrorStatus"):
    sys.exit(0)
body = [x.get("text") for x in msg.get("content") if isinstance(x, dict) and x.get("type") == "text" and (x.get("text") or "").strip()][-1]
# 5. dedupe on the assistant entry uuid
uid = str(last.get("uuid") or "")
if uid:
    try:
        os.makedirs(marker_dir, exist_ok=True)
        fd = os.open(os.path.join(marker_dir, f".plane-rc-relay-{uid}"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        sys.exit(0)
    except OSError as exc:
        # no dedupe is possible → do not record; a re-fired Stop would
        # otherwise double-record exactly when the marker cannot be written
        print(f"plane-rc-relay-out: marker unwritable ({exc}) — not recording", file=sys.stderr)
        sys.exit(0)
print(json.dumps({"events": [
    {"event_type": "communication", "emitter": "rc-relay-hook", "fleet": fleet,
     "payload": {"msg_id": msg_id, "sender": f"bot:{fleet}/{bot}",
                 "recipient_raw": chat, "message_class": "chat", "body": body}},
    {"event_type": "transmission", "emitter": "rc-relay-hook", "fleet": fleet,
     "payload": {"msg_id": msg_id, "attempt_no": 1, "carrier": "telegram-bridge",
                 "destination": chat, "state": "unknown"}},
]}))
PYEOF

MSG_ID="$(plane_mint_id msg)" || exit 0
BATCH="$(printf '%s' "$PAYLOAD" | python3 -S -E -c "$PYPROG" "$FLEET_NAME" "$BOT_ID" "$MARKER_DIR" "$MSG_ID")"
[ -n "$BATCH" ] || exit 0
printf '%s' "$BATCH" | plane_emit_events rc-relay-hook >&2 || true
exit 0
