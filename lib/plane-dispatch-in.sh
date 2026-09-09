#!/bin/bash
# plane-dispatch-in.sh — UserPromptSubmit hook that records what the RECEIVER
# actually got (chunk P, #1501: delivery becomes a JOIN). Delivery used to be
# the SENDER's inference — `pane_submitted` means "the sender pressed Enter and
# the input box looked empty after" — which was wrong for 94 of 180 large sends
# the week before chunk O, and wrong BY CONSTRUCTION: the sender cannot see what
# the receiver's model ingested. This hook is the receiver's own row.
#
# A tracked dispatch/report/briefing arrives with a routing trailer that
# `bot_tmux_send` appended on its OWN final line:
#
#     set +H; [BOTCOMMAND] mgr | task | …          <- the message proper
#     ⟦plane:msg_<32hex>⟧                          <- the trailer (last line)
#
# The trailer rides the LAST tmux chunk (chunk O), so it survives the head loss
# that was the measured failure mode; a send whose TAIL was lost carries no
# trailer and is an honest UNCONFIRMED. A prompt with no trailer is not a
# tracked message and is recorded as nothing — the zero-fork prefilter below
# exits before any python spawn, so an ordinary prompt pays almost nothing.
#
# THE STRIP BOUNDARY IS THE CRUX (chunk P fold F1). The sender records a WIRE
# proof: the sha256 + byte length of the EXACT bytes bot_tmux_send put on the
# wire for the message proper — sanitize_tmux_input(payload), the `set +H; `
# prefix `lib/dispatch.sh` may prepend INCLUDED, the trailer bot_tmux_send
# appends on its own last line NOT. So this hook strips EXACTLY ONE thing: the
# trailer (with its own-line separator). What remains is that same wire form; it
# is hashed (sha256, UTF-8 bytes) and byte-counted, and both ride the `received`
# transmission keyed to <msg_id>. The delivery JOIN
# (queries.DELIVERY_STATUS_SQL) compares them to the sender's WIRE proof: equal
# -> DELIVERED, genuinely shorter -> TRUNCATED, transformed -> ALTERED, a pane
# submission with no such row -> UNCONFIRMED. Comparing the wire form on BOTH
# ends is what makes DELIVERED fire for every shape — a multi-line / tabbed /
# double-spaced dispatch (whose newlines sanitize collapses to spaces on the way
# out) now delivers cleanly, where the pre-fold body-hash comparison read it
# ALTERED though fully delivered.
#
# STDOUT DISCIPLINE IS LOAD-BEARING: UserPromptSubmit stdout is ADDED TO THE
# MODEL'S CONTEXT. Every path here writes NOTHING to stdout — the parser output
# is captured for the emit pipe only, disclosures go to stderr — and exits 0: a
# hook must never block or reshape a turn.
#
# The PROGRAM rides argv (-c) and the payload rides STDIN, the plane-telegram-in
# plumbing (both wrong plumbings failed there for real). SILENT only under
# PLANE_EMIT_DISABLED=1 and NON-BLOCKING, the estate pattern. Deliberately does
# NOT source lib-common.sh (~50ms/prompt on a Pi; the gh-mention-guard
# precedent) — plane-emit.sh is called direct, with the same failure disclosure.

set -u

[ "${PLANE_EMIT_DISABLED:-0}" = "1" ] && exit 0
if [ -z "${FLEET_NAME:-}" ] || [ -z "${BOT_ID:-}" ]; then
  echo "plane-dispatch-in: armed but FLEET_NAME/BOT_ID unset — not recording" >&2
  exit 0
fi
command -v python3 >/dev/null 2>&1 || exit 0

PAYLOAD="$(cat 2>/dev/null || true)"
[ -n "$PAYLOAD" ] || exit 0

# ZERO-FORK prefilter (the gh-mention-guard idiom): on an armed fleet EVERY
# prompt pays this hook, and an ordinary prompt — which has no trailer — must
# exit before any python spawn. The anchor is the marker's ASCII CORE
# `plane:msg_`, NOT the multibyte bracket `⟦`: this hook reads the RAW hook JSON
# here, and a JSON encoder that escapes non-ASCII (Python's default, and a real
# possibility for the harness) writes the bracket as `⟦` — so a glob on the
# literal `⟦` would silently miss the escaped wire form, a false negative that
# drops a real receipt. `plane:msg_` is unescaped in every JSON encoding, so it
# matches both the literal-UTF-8 and the \u-escaped forms; the python decider
# below sees the DECODED prompt (json.loads restores the bracket either way) and
# validates the msg-id grammar and the end-anchor. Loose ON PURPOSE: a false
# positive only spends one python spawn.
case "$PAYLOAD" in
  *'plane:msg_'*) ;;
  *) exit 0 ;;
esac

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# read -d NUL is a builtin (no fork) and returns 1 at EOF-without-NUL, which is
# how this heredoc ends.
IFS= read -r -d '' PYPROG <<'PYEOF' || true
import hashlib
import json
import re
import sys

fleet, bot = sys.argv[1], sys.argv[2]
try:
    hook = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
prompt = hook.get("prompt") or ""
# The trailer, at the very END (a re-send would only append a fresh one; the
# end anchor + the minted-id grammar make a body that merely QUOTES a marker
# harmless). `\s*$` tolerates any trailing whitespace the terminal/pane added
# after the marker.
m = re.search(r"⟦plane:(msg_[0-9a-f]{32})⟧\s*$", prompt)
if not m:
    # A marker-shaped tail that is not a valid trailer is disclosed, never
    # recorded — the quiet-drop seam telegram-in learned to speak at.
    print("plane-dispatch-in: a plane marker at the prompt tail did not parse"
          " as a minted trailer — not recorded", file=sys.stderr)
    sys.exit(0)
msg_id = m.group(1)
# fold F1: strip ONLY the trailer and its own-line separator (rstrip \r\n — a
# trailing SPACE is the sanitized wire's own and stays). What remains is the
# FULL wire form the sender put on the pane: sanitize_tmux_input(payload),
# INCLUDING any leading `set +H; ` dispatch.sh prepended. The sender records the
# sha256 of exactly those bytes as its wire proof, so hashing the arrival minus
# trailer matches it for EVERY shape. (Before the fold this also stripped a
# leading `set +H; ` to match the body hash — which only held for a sanitize-
# stable single-line body; multi-line / tabbed / double-spaced dispatches then
# read ALTERED though fully delivered.)
remainder = prompt[:m.start()].rstrip("\r\n")
raw = remainder.encode("utf-8")
event = {
    "event_type": "transmission", "emitter": "dispatch-in-hook", "fleet": fleet,
    "payload": {
        "msg_id": msg_id, "attempt_no": 1, "carrier": "tmux",
        "destination": bot, "state": "received",
        "received_bytes": len(raw),
        "received_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    },
}
print(json.dumps({"events": [event]}, ensure_ascii=False))
PYEOF

# One python3 (-S -E, the plane-emit.sh spawn discipline) parses the hook JSON
# from STDIN and builds the single `received` event. Stdout is captured for the
# pipe only; empty when nothing matched (rc 0). A nonzero rc is real breakage.
BATCH="$(printf '%s' "$PAYLOAD" | python3 -S -E -c "$PYPROG" "$FLEET_NAME" "$BOT_ID")"
if [ $? -ne 0 ]; then
  echo "plane-dispatch-in: parser failed (payload ${#PAYLOAD}B) — receipt not recorded" >&2
  exit 0
fi
[ -n "$BATCH" ] || exit 0
printf '%s' "$BATCH" | "$LIB_DIR/plane-emit.sh" >/dev/null || \
  echo "plane-dispatch-in: plane record failed rc=$? — receipt not recorded" >&2
exit 0
