#!/bin/bash
# plane-session-start.sh — SessionStart hook (PR-B T7; spec §19.6 / F12).
#
# Claude Code invokes SessionStart hooks with a JSON payload on stdin carrying
# the platform session id. This hook derives the plane session identity and
# publishes it where the doors read it:
#
#   $BOT_DIR/data/.plane-session   (0600) —
#     session_uid   sess_<sha256(platform id)[:32]>  — the TRANSCRIPT identity,
#                   stable across resume (one transcript = one uid; the bash
#                   derivation here MUST match claudlobby.plane.ids.
#                   derive_session_uid byte-for-byte, pinned by test)
#     process_uid   proc_<random 32 hex> — minted fresh EVERY process start,
#                   distinguishing concurrent resumes of one transcript (F12)
#
# report-back.sh attaches session_uid to its task facts. DORMANT unless the
# fleet armed PLANE_EMIT_ENABLED=1 (the hook inherits the session env, which
# sourced bot.conf); an empty/missing platform id is REJECTED with disclosure
# (F12 — never derive from nothing). A hook must never break a boot: every
# path exits 0.

set -u

[ "${PLANE_EMIT_ENABLED:-0}" = "1" ] || exit 0
[ "${PLANE_EMIT_DISABLED:-0}" = "1" ] && exit 0

payload="$(cat 2>/dev/null || true)"
session_id="$(printf '%s' "$payload" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"

if [ -z "$session_id" ]; then
    echo "plane-session-start: no session_id in hook payload — refusing to derive (F12)" >&2
    exit 0
fi

bot_dir="${BOT_DIR:-}"
if [ -z "$bot_dir" ] || [ ! -d "$bot_dir" ]; then
    echo "plane-session-start: BOT_DIR unset or absent — nowhere to publish session identity" >&2
    exit 0
fi

# sha256 portable: shasum (macOS) or sha256sum (Linux).
digest="$( { printf '%s' "$session_id" | shasum -a 256 2>/dev/null \
    || printf '%s' "$session_id" | sha256sum 2>/dev/null; } | cut -c1-32 )"
if [ -z "$digest" ]; then
    echo "plane-session-start: no sha256 tool available — session identity not published" >&2
    exit 0
fi
session_uid="sess_$digest"
process_uid="proc_$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"

mkdir -p "$bot_dir/data" 2>/dev/null || { echo "plane-session-start: cannot create $bot_dir/data" >&2; exit 0; }
out="$bot_dir/data/.plane-session"
tmp="$out.$$"
umask 077
printf '{"session_uid":"%s","process_uid":"%s","derived_at":"%s"}\n' \
    "$session_uid" "$process_uid" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp" \
    && mv -f "$tmp" "$out" \
    || { rm -f "$tmp"; echo "plane-session-start: publish failed" >&2; }
exit 0
