#!/bin/bash
# credential-echo-guard.sh — PreToolUse: refuse a CLI form that prints a
# credential it reads from the environment, unless that variable is removed in
# the same command (#2090).
#
# WHY A HOOK. A bare `neonctl --help` in a live bot session printed the real
# NEON_API_KEY into the transcript: the CLI shows the variable as the default
# of `--api-key`. Every bot inherits that variable, and the only mitigation was
# one sentence of prose in one agent file. The host's settings carry a bare
# `Bash` allow, so no permission rule can refuse these commands; a hook has to.
# The forms are #2090's table A, measured with canaries: neonctl/neon help and
# usage screens, a DEBUG trace in front of neonctl, `pip config list|debug`
# (PIP_INDEX_URL and PIP_EXTRA_INDEX_URL), and `gh auth token`.
#
# IT REFUSES, IT NEVER REWRITES. The refusal names the safe form
# (`env -u NEON_API_KEY ...`) and repeats nothing from the command, which may
# itself carry a value. The decision half is credential-echo-decide.py.
#
# FAILS OPEN, LOUDLY. Missing jq or python3, an unparseable payload, or a
# decider that cannot answer allows the call and leaves a breadcrumb: refusing
# every Bash call fleet-wide is a worse outage than the hazard this guards.
set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLANE_EMIT_CLASS=hook   # a live Claude Code turn waits on its plane record: its socket deadline (#1693, claudlobby/_runtime_scripts/plane-emit.sh)

_allow() { exit 0; }   # no decision — normal permission flow applies

# --- zero-fork prefilters ----------------------------------------------------
# A payload naming none of the registry's CLIs or sub-commands cannot trip a
# row: neonctl/neon, pip ... config, gh auth token. Deliberately over-matches.
# Quotes and backslashes are stripped first, still without a fork: a name
# split by quoting (ne'on'ctl, "n"eonctl) runs the same CLI but never contains
# the name as written. An ANSI-C string can spell a name in escapes, so a
# payload holding one goes to the decider whatever it names.
payload="$(cat)"
_bare="${payload//[\"\'\\]/}"
case "$_bare" in
*neon*|*config*|*auth*) ;;
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

_bail() { # <reason> — fail open, but leave a breadcrumb
    # shellcheck source=lib-common.sh
    . "$LIB_DIR/lib-common.sh" 2>/dev/null || true
    if command -v emit_script_error >/dev/null 2>&1; then
        emit_script_error "" "credential-echo-guard.sh" 1 \
            "$1 — credential-echo guard INACTIVE" 2>/dev/null || true
    fi
    _allow
}

_event() { # <event> <data_json> — count a refusal or an unparsed command
    . "$LIB_DIR/lib-common.sh" 2>/dev/null || true
    if command -v emit_fleet_event >/dev/null 2>&1; then
        # Guarded: sourcing lib-common re-armed set -e, and an exit here would
        # skip the _deny that follows.
        emit_fleet_event "$1" credential-echo-guard "$2" 2>/dev/null || true
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
DECIDER="$LIB_DIR/credential-echo-decide.py"
[ -r "$DECIDER" ] || _bail "credential-echo-decide.py missing"

tool="$(jq -r '.tool_name // empty' <<<"$payload" 2>/dev/null)" || _bail "unparseable hook payload"
case "$tool" in
Bash) ;;
*) _allow ;;
esac
cmd="$(jq -r '.tool_input.command // empty' <<<"$payload" 2>/dev/null)" || _bail "unparseable hook payload"
[ -n "$cmd" ] || _allow

verdict="$("$PY_BIN" "$DECIDER" <<<"$cmd" 2>/dev/null)" || _bail "decider failed"
IFS=$'\t' read -r kind row cli reason <<<"$verdict"

case "$kind" in
allow) _allow ;;
unparsed)
    # Allowed, and COUNTED: a command the decider cannot read cannot be
    # judged, and bash itself refuses the same unbalanced input.
    _event credential_echo_unparsed "$(jq -nc --arg detail "$row" '{detail: $detail}' 2>/dev/null || echo '{}')"
    _allow
    ;;
deny)
    [ -n "$reason" ] || _bail "decider returned no reason"
    # The record names the row and the CLI, never the command: it can carry a value.
    _event credential_echo_refused "$(jq -nc --arg row "$row" --arg cli "$cli" '{row: $row, cli: $cli}' 2>/dev/null || echo '{}')"
    _deny "$reason"
    ;;
esac
_bail "decider returned an unknown verdict"
