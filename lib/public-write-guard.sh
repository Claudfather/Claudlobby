#!/bin/bash
# public-write-guard.sh — PreToolUse: refuse a GitHub-bound write that would put
# a term from the host's list into a PUBLIC repository.
#
# The list is host configuration, never repository content:
# ~/.config/claudlobby/public-write-terms, one case-insensitive regular
# expression per line. The decision lives in public-write-guard.py (stdlib,
# unit-tested); this file is the cheap front door. It covers the Bash gh
# writers, git commit and git push, and every mcp__github__ writer. It refuses
# with a reason and never rewrites a call.
#
# COMPOSED ONLY FOR A BOT THAT OPTED IN (`public_write_guard: true` in
# fleet.yaml), so the manifest key is the canary: a composed hook is live on
# every bot the next generate composes it for (#1310).
#
# THE COST ON A BOT THAT OPTED IN: one bash and one `cat` per Bash or GitHub MCP
# call; python only when the payload is shaped like a GitHub write (the
# prefilter below).
#
# FAILS OPEN, LOUDLY: no python3 or a decider that cannot answer lets the call
# through and leaves a breadcrumb. Every other failure direction is the
# decider's, and each is stated there.
#
# THE OFF SWITCH, host-wide and instant: $CLAUDLOBBY_ROOT/state/public-write-guard/disabled.
set -uo pipefail

case "${BASH_SOURCE[0]}" in
*/*) LIB_DIR="${BASH_SOURCE[0]%/*}" ;;
*) LIB_DIR=. ;;
esac
PLANE_EMIT_CLASS=hook   # a live Claude Code turn waits on this hook (#1693)

[ -e "${CLAUDLOBBY_ROOT:-$LIB_DIR/..}/state/public-write-guard/disabled" ] && exit 0

# --- the prefilter: no python for a call that cannot be a GitHub write --------
# It over-matches on purpose (`gh` and `issue` anywhere in the payload): a false
# positive costs one python start, a false negative is a leak. Every group the
# decider treats as a write must appear here; the tests hold each one.
payload="$(cat)"
case "$payload" in
*mcp__*github*) ;;
*gh*issue* | *gh*pr* | *gh*release* | *gh*gist* | *gh*repo* | *gh*label* | *gh*api* | \
    *git*commit* | *git*push*) ;;
*) exit 0 ;;
esac

_bail() { # <reason> — fail open, but leave a breadcrumb
    # lib-common is sourced HERE and in a subshell: an `exit` inside a sourced
    # file ends the shell whatever surrounds it, and a fail-open hook must not
    # fail because its breadcrumb did.
    (
        # shellcheck source=lib-common.sh
        . "$LIB_DIR/lib-common.sh" &&
            emit_script_error "" "public-write-guard.sh" 1 \
                "$1 — public-write guard INACTIVE for this call"
    ) >/dev/null 2>&1 || true
    exit 0
}

PY_BIN="$(command -v python3 || true)"
[ -n "$PY_BIN" ] || _bail "python3 not available"
out="$("$PY_BIN" "$LIB_DIR/public-write-guard.py" <<<"$payload")" || _bail "decider failed"
[ -z "$out" ] || printf '%s\n' "$out"
exit 0
