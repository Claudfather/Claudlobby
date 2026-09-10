#!/usr/bin/env bash
# claude-session-pid.sh — answer "which Claude Code session am I running INSIDE?"
#
# A skill that reports its own bot health does not need to SEARCH for itself.
# It is already running inside the session it describes, so the honest question
# is a parent-child one: walk UP the ancestry from the caller until the Claude
# Code process is reached.
#
# The predicate this replaces asked a global namespace instead:
#
#     pgrep -f 'claude' | head -1
#
# On a host where every bot runs as the same uid that matches every bot on the
# box, and `head -1` reduces it to whichever matched process started earliest.
# Measured on the estate 2026-09-10: 65 matches, and `head -1` resolved to
# ANOTHER fleet's tmux server (etime 2-21:47:52, rss 4 MB) for every caller.
# It is the same wrong answer for everybody, so two bots comparing notes get
# identical numbers and read that as corroboration. See Claudlobby #1525.
#
# The general rule, shared with #1069: a predicate that scans a namespace the
# asker is inside will find something that is not the asker, and any
# single-element pick — `head -1`, `tail -1`, last-wins — converts that into a
# confident single answer.
#
# FAILURE IS LOUD BY DESIGN. Where the session cannot be resolved this prints
# the literal word `unknown` and exits 3. It never falls back to a process-table
# search: a fallback would be a second copy of the predicate, consulted exactly
# when the two have diverged (the `env-tiers.sh` refusal, same reasoning), and a
# plausible wrong number is the defect this door exists to remove.
#
# Usage:
#   claude-session-pid.sh [--pid]      pid of the Claude Code session (default)
#   claude-session-pid.sh --etime      elapsed run time, ps etime format
#   claude-session-pid.sh --rss-mb     resident memory, as "487 MB"
#   claude-session-pid.sh --summary    "PID 8864 | up 04:12:33 | 487 MB"
#   claude-session-pid.sh --from PID   start the walk at PID instead of the caller
#
# Exit: 0 resolved | 2 usage | 3 unresolved

set -uo pipefail

MAX_HOPS=32

usage() {
    sed -n '3,40p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

# Walk up the ancestry and return the nearest Claude Code process.
# Ancestors only, so this is a parent-child link and never a namespace scan.
resolve_session_pid() {
    local p="$1" hops=0 comm args ppid

    while [ -n "$p" ] && [ "$p" != "0" ] && [ "$p" != "1" ]; do
        hops=$((hops + 1))
        [ "$hops" -gt "$MAX_HOPS" ] && return 1

        comm=$(ps -o comm= -p "$p" 2>/dev/null) || return 1
        [ -z "$comm" ] && return 1

        # Linux reports a bare name here, macOS reports a full path.
        case "${comm##*/}" in
            claude) printf '%s\n' "$p"; return 0 ;;
        esac

        # Installs that exec the CLI through a runtime show up as node or
        # similar, so fall back to the argv of THIS ANCESTOR. Still
        # session-scoped: nothing outside the caller ancestry is ever examined.
        args=$(ps -o args= -p "$p" 2>/dev/null)
        case "$args" in
            claude|claude\ *|*/claude|*/claude\ *)
                printf '%s\n' "$p"; return 0 ;;
        esac

        ppid=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d '[:space:]')
        [ "$ppid" = "$p" ] && return 1
        p="$ppid"
    done

    return 1
}

mode="--pid"
start=""

while [ $# -gt 0 ]; do
    case "$1" in
        --pid|--etime|--rss-mb|--summary) mode="$1"; shift ;;
        --from) start="${2:-}"; shift 2 || usage ;;
        -h|--help) usage ;;
        *) printf 'claude-session-pid.sh: unknown argument: %s\n' "$1" >&2; usage ;;
    esac
done

# Default start point is the caller. When this script runs inside $( ), PPID is
# the invoking shell, so the walk begins one link below the session either way.
[ -z "$start" ] && start="$PPID"

if ! case "$start" in ''|*[!0-9]*) false ;; *) true ;; esac; then
    printf 'claude-session-pid.sh: --from expects a pid, got: %s\n' "$start" >&2
    exit 2
fi

if ! session_pid=$(resolve_session_pid "$start"); then
    printf 'claude-session-pid.sh: no Claude Code process in the ancestry of pid %s — ' "$start" >&2
    printf 'not reporting a process-table guess. See Claudlobby #1525.\n' >&2
    printf 'unknown\n'
    exit 3
fi

case "$mode" in
    --pid)
        printf '%s\n' "$session_pid"
        ;;
    --etime)
        etime=$(ps -o etime= -p "$session_pid" 2>/dev/null | tr -d '[:space:]')
        [ -z "$etime" ] && { printf 'unknown\n'; exit 3; }
        printf '%s\n' "$etime"
        ;;
    --rss-mb)
        rss=$(ps -o rss= -p "$session_pid" 2>/dev/null | tr -d '[:space:]')
        [ -z "$rss" ] && { printf 'unknown\n'; exit 3; }
        printf '%s\n' "$rss" | awk '{printf "%.0f MB\n", $1/1024}'
        ;;
    --summary)
        etime=$(ps -o etime= -p "$session_pid" 2>/dev/null | tr -d '[:space:]')
        rss=$(ps -o rss= -p "$session_pid" 2>/dev/null | tr -d '[:space:]')
        [ -z "$etime" ] && etime="unknown"
        if [ -n "$rss" ]; then
            mem=$(printf '%s\n' "$rss" | awk '{printf "%.0f MB", $1/1024}')
        else
            mem="unknown"
        fi
        printf 'PID %s | up %s | %s\n' "$session_pid" "$etime" "$mem"
        ;;
esac
