#!/bin/bash
# usage-limit-hook.sh — StopFailure and Stop hook (#996): the bot's own record
# of a usage-limit stop, for keepalive to act on.
#
# Claude Code ends a turn that a claude.ai usage limit stopped with a
# StopFailure hook whose payload names the error and the limit line itself
# (measured on 2.1.292):
#   {"hook_event_name":"StopFailure","error":"rate_limit",
#    "last_assistant_message":"You've hit your session limit · resets 10:45am (America/New_York)", ...}
# This hook keeps that fact where keepalive reads it, so keepalive never takes a
# limit line quoted in a bot's own message for a stop:
#
#   StopFailure, error rate_limit  writes data/.usage-limit: the epoch it ran
#                                  (line 1) and the limit line (line 2), and
#                                  records usage_limit_hit on the plane
#   StopFailure, any other error   leaves the marker as it is: an overloaded
#                                  or network error says nothing about a limit
#   Stop                           a turn ended normally, so no limit holds the
#                                  bot now: removes the marker
#
# stdout stays empty and every path exits 0: a hook must never break a turn.
# Refuses without BOT_DIR (never a cwd write: a bot's cwd is its project).

set -uo pipefail
PLANE_EMIT_CLASS=hook   # a live Claude Code turn waits on its plane record: its socket deadline (#1693, claudlobby/_runtime_scripts/plane-emit.sh)
[ -n "${BOT_DIR:-}" ] || exit 0
[ -d "$BOT_DIR/data" ] || exit 0
PAYLOAD="$(cat 2>/dev/null || true)"
[ -n "$PAYLOAD" ] || exit 0
MARKER="$BOT_DIR/data/.usage-limit"
# Every turn end runs this hook, and nearly every one is a Stop with no record
# to clear: answer those without starting python (Claude Code's JSON carries no
# space after a colon; any other spelling falls through to the parse below).
if [ ! -e "$MARKER" ]; then
    case "$PAYLOAD" in
        *'"hook_event_name":"Stop"'*) exit 0 ;;
    esac
fi

# The PROGRAM rides argv (-c) and the payload rides stdin (the #1402 lesson).
IFS= read -r -d '' PYPROG <<'PYEOF' || true
import json, os, sys, tempfile, time
marker = sys.argv[1]
try:
    p = json.loads(sys.stdin.read() or "{}")
except ValueError:
    sys.exit(0)
event = p.get("hook_event_name")
if event == "Stop":
    try:
        os.unlink(marker)
    except OSError:
        pass
    print("cleared")
    sys.exit(0)
if event != "StopFailure" or p.get("error") != "rate_limit":
    sys.exit(0)
text = str(p.get("last_assistant_message") or "").split("\n", 1)[0]
text = "".join(c for c in text if c.isprintable())[:300]
fd, tmp = tempfile.mkstemp(prefix=".usage-limit.", dir=os.path.dirname(marker))
with os.fdopen(fd, "w") as fh:
    fh.write("%d\n%s\n" % (int(time.time()), text))
os.replace(tmp, marker)
print("hit\t" + text)
PYEOF

result=$(printf '%s' "$PAYLOAD" | python3 -S -E -c "$PYPROG" "$MARKER" 2>/dev/null) || exit 0
case "$result" in
    hit$'\t'*)
        # The plane's record of the stop, beside the marker keepalive acts on.
        if [ "${PLANE_EMIT_DISABLED:-0}" != 1 ] && [ -n "${FLEET_NAME:-}" ]; then
            LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
            # shellcheck source=lib-common.sh
            . "$LIB_DIR/lib-common.sh" 2>/dev/null || exit 0
            set +e
            emit_fleet_event usage_limit_hit hook \
                "{\"limit_line\":\"$(json_escape "${result#hit$'\t'}")\"}" \
                "$BOT_DIR" "${BOT_ID:-${BOT_NAME:-}}" >/dev/null 2>&1
        fi
        ;;
esac
exit 0
