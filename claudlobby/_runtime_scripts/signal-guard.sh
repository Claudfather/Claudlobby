#!/bin/bash
# signal-guard.sh — PreToolUse: refuse a Bash command that signals a process
# the caller did not start (#1069).
#
# WHY A HOOK. Every bot on a host runs as one user, so pkill, killall and a
# pid read back from ps, pgrep or $PPID can reach another bot. Worse, an
# orphaned job can be re-parented to the user manager, so a "parent" read back
# with ps -o ppid= can be the manager that runs every bot: a cleanup loop that
# killed a pattern match and its parent by pid stopped every bot on a host for
# 15 hours (#2158). Warnings were composed into every bot and did not stop it,
# and the host's bare Bash allow means no permission rule can refuse it.
#
# PROVENANCE, NOT VERB. Killing by pid is safe only for a pid the caller
# started, so signal-decide.py allows a signal only to the caller's own
# handles ($!, job specs, $$, a pid file) and refuses the rest. What it
# refuses, what it allows and what is out of its reach are stated once, in
# its docstring.
#
# FAILS OPEN, LOUDLY. Missing jq or python3, an unparseable payload, or a
# decider that cannot answer allows the call and leaves a breadcrumb: refusing
# every kill fleet-wide on a broken install is worse than the hazard it guards.
set -uo pipefail

PLANE_EMIT_CLASS=hook   # a live Claude Code turn waits on its plane record: its socket deadline (#1693, claudlobby/_runtime_scripts/plane-emit.sh)

_allow() { exit 0; }   # no decision — normal permission flow applies

# --- zero-fork prefilters ----------------------------------------------------
# A payload naming none of kill, pkill, killall, skill or fuser cannot send a
# signal through any form the decider judges. Quotes and backslashes are
# stripped first, so a name split by quoting (k'ill', p"k"ill) or by a line
# continuation (the JSON text \\\n, deleted first) is still seen, and the
# word skills (a directory every bot names) is dropped, since no signal verb
# is a part of it. An ANSI-C string can spell a name in escapes,
# so a payload holding one goes to the decider whatever it names. The strip
# runs on bytes: a quote byte never occurs inside a UTF-8 sequence.
payload="$(cat)"
_bare_payload() { local LC_ALL=C; _bare="${payload//\\\\\\n/}"; _bare="${_bare//[\"\'\\]/}"; _bare="${_bare//[sS]kills/}"; }
_bare_payload
case "$_bare" in
*kill*|*fuser*) ;;
*)
    case "$payload" in
    *"\$'"*) ;;
    *) _allow ;;
    esac
    ;;
esac
case "$payload" in
*Bash*) ;;
*) _allow ;;
esac

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_bail() { # <reason> — fail open, but leave a breadcrumb
    # shellcheck source=lib-common.sh
    . "$LIB_DIR/lib-common.sh" 2>/dev/null || true
    if command -v emit_script_error >/dev/null 2>&1; then
        emit_script_error "" "signal-guard.sh" 1 \
            "$1 — signal guard INACTIVE" 2>/dev/null || true
    fi
    _allow
}

_event() { # <event> <data_json> — count a refusal
    . "$LIB_DIR/lib-common.sh" 2>/dev/null || true
    if command -v emit_fleet_event >/dev/null 2>&1; then
        # Guarded: sourcing lib-common re-armed set -e, and an exit here would
        # skip the _deny that follows.
        emit_fleet_event "$1" signal-guard "$2" 2>/dev/null || true
    fi
}

_deny() { # <reason>
    jq -nc --arg r "$1" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
    exit 0
}

command -v jq >/dev/null 2>&1 || _bail "jq not available"
PY_BIN="$(command -v python3 || true)"
[ -n "$PY_BIN" ] || _bail "python3 not available"
DECIDER="$LIB_DIR/signal-decide.py"
[ -r "$DECIDER" ] || _bail "signal-decide.py missing"

# One jq for both fields: another tool's call has no command to judge.
cmd="$(jq -r 'if .tool_name == "Bash" then .tool_input.command // empty else empty end' <<<"$payload" 2>/dev/null)" \
    || _bail "unparseable hook payload"
[ -n "$cmd" ] || _allow

verdict="$("$PY_BIN" "$DECIDER" <<<"$cmd" 2>/dev/null)" || _bail "decider failed"
IFS=$'\t' read -r kind kinds reason <<<"$verdict"

case "$kind" in
allow) _allow ;;
deny)
    [ -n "$reason" ] || _bail "decider returned no reason"
    # The record names the kinds of target refused, never the command.
    _event signal_guard_refused "$(jq -nc --arg k "$kinds" '{kinds: ($k | split(","))}' 2>/dev/null || echo '{}')"
    _deny "$reason"
    ;;
esac
_bail "decider returned an unknown verdict"
