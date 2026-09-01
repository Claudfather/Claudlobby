#!/bin/bash
# plane-telegram-in.sh — UserPromptSubmit hook recognizing the telegram
# plugin's channel injection (#1402: the INBOUND half — the OPERATOR's own
# messages, the plane's founding operator-in-the-stream gap). A channel
# message arrives injected as a user turn shaped:
#
#   <channel source="telegram" chat_id="..." message_id="..." user="..."
#            ts="...">the words</channel>
#
# This hook matches THAT SHAPE ONLY — an ordinary prompt exits untouched —
# and emits the communication (sender = human:<user>, a first-class actor;
# recipient = this bot; body per capture policy at the emit door) plus a
# recipient_acknowledged transmission (we demonstrably received it: this
# hook is running inside the receiving turn).
#
# STDOUT DISCIPLINE IS LOAD-BEARING: UserPromptSubmit stdout is ADDED TO
# THE MODEL'S CONTEXT. Every path here writes NOTHING to stdout — jq/python
# output is captured or sent to stderr — and exits 0: a hook must never
# block or reshape a turn. The tag parse runs in python3 (the
# plane-session-start precedent): attribute order varies and a sed parse of
# quoted attrs diverges on escapes.
#
# DORMANT (PLANE_EMIT_ENABLED=1) and NON-BLOCKING, the estate pattern.

set -u

[ "${PLANE_EMIT_ENABLED:-0}" = "1" ] || exit 0
[ "${PLANE_EMIT_DISABLED:-0}" = "1" ] && exit 0
[ -n "${FLEET_NAME:-}" ] && [ -n "${BOT_ID:-}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
set +e

PAYLOAD="$(cat 2>/dev/null || true)"
[ -n "$PAYLOAD" ] || exit 0

MSG_ID="$(plane_mint_id msg)" || exit 0
# One python3 does everything after the mint: parse the hook JSON, match the
# channel tag, extract attrs + words, build the batch. Prints the batch JSON
# to stdout FOR THE PIPE ONLY (captured — never the hook's stdout), or
# nothing when the prompt is not a telegram injection.
# payload rides ARGV, deliberately not a pipe: `python3 -` reads its
# PROGRAM from stdin, so the heredoc owns stdin and a piped payload is
# silently dead (caught by the battery — BATCH came back empty on every
# valid injection)
BATCH="$(python3 - "$FLEET_NAME" "$BOT_ID" "$MSG_ID" "$PAYLOAD" 2>/dev/null <<'PYEOF'
import json
import re
import sys

fleet, bot, msg_id = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    hook = json.loads(sys.argv[4])
except Exception:
    sys.exit(0)
prompt = hook.get("prompt") or ""
m = re.search(r"<channel\s+([^>]*\bsource=\"telegram\"[^>]*)>(.*?)</channel>",
              prompt, re.DOTALL)
if not m:
    sys.exit(0)
attrs = dict(re.findall(r"(\w+)=\"([^\"]*)\"", m.group(1)))
body = m.group(2).strip()
if not body:
    sys.exit(0)
user = attrs.get("user") or "telegram"
chat = attrs.get("chat_id") or ""
tg_id = attrs.get("message_id") or ""
comm = {
    "event_type": "communication", "emitter": "telegram-hook",
    "fleet": fleet,
    "payload": {
        "msg_id": msg_id,
        "sender": f"human:{user}",
        "recipient": f"bot:{fleet}/{bot}",
        "message_class": "chat",
        "body": body,
    },
}
tx = {
    "event_type": "transmission", "emitter": "telegram-hook",
    "fleet": fleet,
    "payload": {
        "msg_id": msg_id, "attempt_no": 1, "carrier": "telegram-bridge",
        "destination": chat, "state": "recipient_acknowledged",
        **({"carrier_ref": f"tg:{tg_id}"} if tg_id.isdigit() else {}),
    },
}
print(json.dumps({"events": [comm, tx]}, ensure_ascii=False))
PYEOF
)" || true
[ -n "$BATCH" ] || exit 0
printf '%s' "$BATCH" | plane_emit_events telegram-hook >/dev/null 2>&1 || true
exit 0
