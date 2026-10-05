#!/bin/bash
# signal-guard.sh — PreToolUse: refuse a Bash command that signals a process
# the caller did not start (#1069).
#
# WHY A HOOK. Every bot on a host runs as one user, so pkill, killall and a
# pid read back from ps, pgrep or $PPID can reach another bot. Worse, an
# orphaned job is re-parented to the user manager, so a "parent" read back
# with ps -o ppid= can be the manager that runs every bot: a cleanup loop that
# killed a pattern match and its parent by pid stopped every bot on a host for
# 15 hours (#2158). Warnings were composed into every bot and did not stop it,
# and the host's bare Bash allow means no permission rule can refuse it.
#
# PROVENANCE, NOT VERB. Killing by pid is safe only for a pid the same command
# started, so signal-decide.py refuses a signal whose target is a lookup's
# answer, a name or pattern, every process (kill -1), or a typed pid that is
# this session's own ancestor, and allows $!, job specs, $$, group 0, a pid
# file and kill -0. It reads only the command text: a script file, eval of a
# variable or a pid passed through a file is out of reach, and the per-bot
# subreaper (#2158) is the backstop for those.
#
# FAILS OPEN, LOUDLY. Missing jq or python3, an unparseable payload, or a
# decider that cannot answer allows the call and leaves a breadcrumb: refusing
# every kill fleet-wide on a broken install is worse than the hazard it guards.
set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLANE_EMIT_CLASS=hook   # a live Claude Code turn waits on its plane record: its socket deadline (#1693, claudlobby/_runtime_scripts/plane-emit.sh)

_allow() { exit 0; }   # no decision — normal permission flow applies

# --- zero-fork prefilters ----------------------------------------------------
# A payload naming none of kill, pkill, killall, skill or fuser cannot send a
# signal through any form the decider judges. Deliberately over-matches
# (skills, kill-port): a false positive costs one python start. Quotes and
# backslashes are stripped first, still without a fork, so a name split by
# quoting (k'ill', p"k"ill) is still seen.
payload="$(cat)"
_bare="${payload//[\"\'\\]/}"
case "$_bare" in
*kill*|*fuser*) ;;
*) _allow ;;
esac
case "$payload" in
*Bash*) ;;
*) _allow ;;
esac

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

tool="$(jq -r '.tool_name // empty' <<<"$payload" 2>/dev/null)" || _bail "unparseable hook payload"
case "$tool" in
Bash) ;;
*) _allow ;;
esac
cmd="$(jq -r '.tool_input.command // empty' <<<"$payload" 2>/dev/null)" || _bail "unparseable hook payload"
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
