#!/bin/bash
# plane-telegram-in.sh — UserPromptSubmit hook recognizing the telegram
# plugin's channel injection (#1402: the INBOUND half — the OPERATOR's own
# messages, the plane's founding operator-in-the-stream gap). A channel
# message arrives injected as a user turn shaped:
#
#   <channel source="telegram" chat_id="..." message_id="..." user="..."
#            ts="...">the words</channel>
#
# This hook matches THAT SHAPE ONLY — an ordinary prompt exits at the
# zero-fork prefilter below — and emits, PER TAG (the plugin may batch two
# operator messages into one injection), the communication (sender =
# human:<user>, a first-class actor; recipient = this bot; body per
# capture policy at the emit door; occurred_at = the carrier's ts when it
# parses — §4: this hook is a RELAY of the carrier's instant) plus a
# recipient_acknowledged transmission (receipt is demonstrated: this hook
# runs inside the receiving turn).
#
# STDOUT DISCIPLINE IS LOAD-BEARING: UserPromptSubmit stdout is ADDED TO
# THE MODEL'S CONTEXT. Every path here writes NOTHING to stdout — parser
# output is captured, disclosures go to stderr — and exits 0: a hook must
# never block or reshape a turn.
#
# The payload rides ARGV into python (`python3 -` takes its PROGRAM from
# stdin, so a piped payload under a heredoc is silently dead — caught by
# the battery). ARG_MAX bounds that at ~1MB; Telegram messages cap at
# 4096 chars, ~250x inside the ceiling (disclosed, not re-plumbed).
#
# DORMANT (PLANE_EMIT_ENABLED=1) and NON-BLOCKING, the estate pattern.

set -u

[ "${PLANE_EMIT_ENABLED:-0}" = "1" ] || exit 0
[ "${PLANE_EMIT_DISABLED:-0}" = "1" ] && exit 0
if [ -z "${FLEET_NAME:-}" ] || [ -z "${BOT_ID:-}" ]; then
  echo "plane-telegram-in: armed but FLEET_NAME/BOT_ID unset — not recording" >&2
  exit 0
fi
command -v python3 >/dev/null 2>&1 || exit 0

PAYLOAD="$(cat 2>/dev/null || true)"
[ -n "$PAYLOAD" ] || exit 0

# ZERO-FORK prefilter (the gh-mention-guard idiom, measured there 3.5ms vs
# 137ms on a Pi): on an armed fleet EVERY prompt pays this hook, and an
# ordinary prompt must exit before any source, mint or python spawn.
# Loose ON PURPOSE: the python regex is the decider; the prefilter only
# skips the spawn on ordinary prompts, and a prefilter FALSE NEGATIVE
# silently drops an operator message (r2 — the regex accepts a newline/tab
# after `<channel`, so the glob must not require the space).
case "$PAYLOAD" in
  *'<channel'*'telegram'*) ;;
  *) exit 0 ;;
esac

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
set +e  # lib-common re-arms set -e at source time; a hook must not die

# One python3 parses the hook JSON, walks EVERY channel tag, and builds the
# batch — msg ids minted per message inside (multi-tag needs one each).
# Stdout is captured for the pipe only; empty when nothing matched.
BATCH="$(python3 - "$FLEET_NAME" "$BOT_ID" "$PAYLOAD" 2>/dev/null <<'PYEOF'
import json
import re
import secrets
import sys

fleet, bot = sys.argv[1], sys.argv[2]
try:
    hook = json.loads(sys.argv[3])
except Exception:
    sys.exit(0)
prompt = hook.get("prompt") or ""
# EVERY tag is recorded (finditer — first-match-only silently dropped a
# second batched message). Known edge, disclosed: a body containing a
# literal </channel> truncates at that close — the non-greedy stop is what
# keeps two ADJACENT tags from merging, and the plugin's own injection
# never embeds an unescaped close.
tags = re.finditer(
    r"<channel\s+([^>]*\bsource=\"telegram\"[^>]*)>(.*?)</channel>",
    prompt, re.DOTALL)
events = []
for m in tags:
    attrs = dict(re.findall(r"(\w+)=\"([^\"]*)\"", m.group(1)))
    body = m.group(2).strip()
    if not body:
        continue
    # alias hygiene: the user attr is group-member-influenced text; the
    # human: prefix is the namespace guard (a forged bot:... user becomes
    # human:bot:..., never the real actor — probed), and the clamp keeps
    # registry aliases printable
    user = re.sub(r"[^\w.:-]", "_", attrs.get("user") or "telegram")[:64]
    chat = attrs.get("chat_id") or ""
    tg_id = attrs.get("message_id") or ""
    msg_id = "msg_" + secrets.token_hex(16)
    envelope = {}
    ts = (attrs.get("ts") or "").replace("Z", "+00:00").replace("z", "+00:00")
    # §4: the carrier's ts IS the occurrence instant and this hook is a
    # relay. VALUE-validated, not shape-validated (r2, probed: 2026-13-01
    # passed the old regex, pydantic rejected it, and the validate-all-
    # atomic batch lost BOTH rows — including a legit sibling message):
    # only a ts the datetime parser itself accepts rides the envelope.
    if ts:
        try:
            from datetime import datetime
            if datetime.fromisoformat(ts).tzinfo is not None:
                envelope["occurred_at"] = ts
        except ValueError:
            pass
    events.append({
        "event_type": "communication", "emitter": "telegram-hook",
        "fleet": fleet, **envelope,
        "payload": {"msg_id": msg_id, "sender": f"human:{user}",
                    "recipient": f"bot:{fleet}/{bot}",
                    "message_class": "chat", "body": body}})
    events.append({
        "event_type": "transmission", "emitter": "telegram-hook",
        "fleet": fleet,
        "payload": {"msg_id": msg_id, "attempt_no": 1,
                    "carrier": "telegram-bridge", "destination": chat,
                    "state": "recipient_acknowledged",
                    **({"carrier_ref": f"tg:{tg_id}"}
                       if tg_id.isdigit() else {})}})
if not events:
    sys.exit(0)
print(json.dumps({"events": events}, ensure_ascii=False))
PYEOF
)" || true
[ -n "$BATCH" ] || exit 0
printf '%s' "$BATCH" | plane_emit_events telegram-hook >/dev/null 2>&1 || true
exit 0
