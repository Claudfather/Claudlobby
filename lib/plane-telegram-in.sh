#!/bin/bash
# plane-telegram-in.sh — UserPromptSubmit hook recognizing the telegram
# plugin's channel injection (#1402: the INBOUND half — the OPERATOR's own
# messages, the plane's founding operator-in-the-stream gap). A channel
# message arrives injected as a user turn shaped:
#
#   <channel source="plugin:telegram:telegram" chat_id="..."
#            message_id="..." user="..." user_id="..." ts="...">
#   the words
#   </channel>
#
# THE SOURCE VALUE IS THE PLUGIN-QUALIFIED NAME (r4, read from a LIVE
# transcript on the deployed estate): `plugin:telegram:telegram`, not the
# bare `telegram` three rounds of fixtures carried — the deployed hook
# dropped the operator's first real message because nobody had pulled the
# tag from a transcript. The matcher accepts `telegram` or any value
# ending `:telegram`; the canonical test fixture is the live tag verbatim.
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
# The PROGRAM rides argv (-c) and the payload rides STDIN — each on the
# channel that has no ceiling for it. Both wrong plumbings failed for
# real: `python3 -` takes its program from stdin, so a piped payload under
# a heredoc is silently dead (r1, caught by the battery); a payload on
# argv dies at ARG_MAX ~1MB, and a multi-tag injection can exceed the
# per-message 4096 cap, so the loss was silent exactly when the batch was
# biggest (r3, probed at 1.5MB/2.5MB: rc 0, nothing recorded, no stderr).
#
# DORMANT (PLANE_EMIT_ENABLED=1) and NON-BLOCKING, the estate pattern.
# Deliberately does NOT source lib-common.sh: nothing here needs it, and
# the 3,865-line source costs ~50ms per channel message on a Pi (r3; the
# gh-mention-guard precedent) — plane-emit.sh is called direct, with the
# same failure disclosure plane_emit_events would have printed.

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
# ordinary prompt must exit before any mint or python spawn. Loose ON
# PURPOSE: the python regex is the decider; the prefilter only skips the
# spawn on ordinary prompts, and a prefilter FALSE NEGATIVE silently drops
# an operator message (r2 — the regex accepts a newline/tab after
# `<channel`, so the glob must not require the space).
case "$PAYLOAD" in
  *'<channel'*'telegram'*) ;;
  *) exit 0 ;;
esac

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# read -d NUL is a builtin (no fork) and returns 1 at EOF-without-NUL,
# which is the expected way this heredoc ends.
IFS= read -r -d '' PYPROG <<'PYEOF' || true
import json
import re
import secrets
import sys
from datetime import datetime

fleet, bot = sys.argv[1], sys.argv[2]
try:
    hook = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
prompt = hook.get("prompt") or ""
# EVERY tag is recorded (finditer — first-match-only silently dropped a
# second batched message). Known edge, disclosed: a body containing a
# literal </channel> truncates at that close — the non-greedy stop is what
# keeps two ADJACENT tags from merging, and the plugin's own injection
# never embeds an unescaped close.
tags = re.finditer(
    r"<channel\s+([^>]*\bsource=\"(?:[\w.:-]*:)?telegram\"[^>]*)>(.*?)</channel>",
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
    # the msg_ mint: same <prefix>_<32hex> rule as lib-common.sh's
    # plane_mint_id (per-tag minting needs it in-process; ingest pins the
    # format) — an edit to either must visit its twin
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

# One python3 (-S -E, the plane-emit.sh spawn discipline: 45ms -> 12ms
# measured there on a Pi) parses the hook JSON from STDIN, walks EVERY
# channel tag, and builds the batch — msg ids minted per message inside
# (multi-tag needs one each). Stdout is captured for the pipe only; empty
# when nothing matched (rc 0). A nonzero rc is real breakage, and the one
# path that drops a message must say so (r3: the argv plumbing lost
# oversized payloads at rc 0 with no stderr).
BATCH="$(printf '%s' "$PAYLOAD" | python3 -S -E -c "$PYPROG" "$FLEET_NAME" "$BOT_ID" 2>/dev/null)"
if [ $? -ne 0 ]; then
  echo "plane-telegram-in: parser failed (payload ${#PAYLOAD}B) — message not recorded" >&2
  exit 0
fi
[ -n "$BATCH" ] || exit 0
printf '%s' "$BATCH" | "$LIB_DIR/plane-emit.sh" >/dev/null || \
  echo "plane-telegram-in: plane record failed rc=$? — message not recorded" >&2
exit 0
