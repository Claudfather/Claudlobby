#!/bin/bash
# plane-session-start.sh — SessionStart hook (PR-B T7; spec §19.6 / F12).
#
# Claude Code invokes SessionStart hooks with a JSON payload on stdin carrying
# the platform session id. This hook derives the plane session identity and
# publishes it where the doors read it:
#
#   $BOT_DIR/data/.plane-session   (0600) —
#     session_uid   sess_<sha256(platform id)[:32]>  — the TRANSCRIPT identity,
#                   stable across resume; derivation MUST match
#                   claudlobby.plane.ids.derive_session_uid byte-for-byte
#                   (pinned by test), so the payload is parsed and the digest
#                   computed by python3 — a sed-and-shasum parse diverged on
#                   \uXXXX escapes and escaped quotes (#1372 review F8).
#     process_uid   proc_<random 32 hex> — minted fresh EVERY process start.
#
# CONCURRENCY BOUND (#1372 review F9, disclosed not solved): the file is
# bot-global and latest-writer-wins. Concurrent resumes of ONE transcript
# agree on session_uid by derivation, so reads stay correct; concurrent
# DIFFERENT transcripts on one bot leave the older process reading the newer
# identity. process_uid is recorded for the future OTel/process join and is
# deliberately NOT attached to plane events yet — a reader cannot know which
# process invoked it, and propagating a possibly-wrong uid is worse than none.
#
# A refused start INVALIDATES any previous identity (the stale file is
# removed) — retaining it attributed the new session's work to the old one.
# SILENT only under PLANE_EMIT_DISABLED=1 (the harness exemption); every path exits 0 —
# a hook must never break a boot.

set -u

[ "${PLANE_EMIT_DISABLED:-0}" != "1" ] || exit 0
[ "${PLANE_EMIT_DISABLED:-0}" = "1" ] && exit 0

bot_dir="${BOT_DIR:-}"
out=""
if [ -n "$bot_dir" ] && [ -d "$bot_dir" ]; then
    out="$bot_dir/data/.plane-session"
fi

_refuse() {
    echo "plane-session-start: $1 — refusing to derive (F12)" >&2
    # Invalidate stale identity: keeping the old file would attribute THIS
    # session's reports to the PREVIOUS session.
    [ -n "$out" ] && rm -f "$out" 2>/dev/null
    exit 0
}

command -v python3 >/dev/null 2>&1 || _refuse "no python3 (JSON parse + digest parity need it)"
[ -n "$out" ] || { echo "plane-session-start: BOT_DIR unset or absent — nowhere to publish session identity" >&2; exit 0; }

payload="$(cat 2>/dev/null || true)"

# One python3 -S spawn does parse + validation + BOTH uids: json.loads gives
# the decoded string (\uXXXX, escaped quotes — the exact cases sed got wrong),
# and hashlib on its UTF-8 bytes is derive_session_uid verbatim. The payload
# rides an env var, NOT stdin — `python3 -` + a heredoc would make the SCRIPT
# consume stdin and leave json.load nothing to read.
identity="$(PLANE_HOOK_PAYLOAD="$payload" python3 -S -E -c '
import hashlib, json, os, secrets, sys
try:
    sid = json.loads(os.environ.get("PLANE_HOOK_PAYLOAD", "")).get("session_id")
except Exception:
    sys.exit(3)
if not isinstance(sid, str) or not sid.strip():
    sys.exit(3)
digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32]
print("sess_" + digest + " proc_" + secrets.token_hex(16))
' 2>/dev/null)" || true

case "$identity" in
    sess_*" proc_"*) ;;
    *) _refuse "no valid session_id in hook payload" ;;
esac
session_uid="${identity%% *}"
process_uid="${identity##* }"

mkdir -p "$bot_dir/data" 2>/dev/null || { echo "plane-session-start: cannot create $bot_dir/data" >&2; exit 0; }
tmp="$out.$$"
umask 077
printf '{"session_uid":"%s","process_uid":"%s","derived_at":"%s"}\n' \
    "$session_uid" "$process_uid" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp" \
    && mv -f "$tmp" "$out" \
    || { rm -f "$tmp"; echo "plane-session-start: publish failed" >&2; }
exit 0
