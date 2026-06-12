#!/bin/bash
# tail-fleet.sh — fleet-wide log tail with optional grep filter.
#
# Iterates all bot log directories and tails recent output. Useful for
# live monitoring or post-incident review across the entire fleet.
#
# Usage:
#   tail-fleet.sh [--fleet <name>] [--lines N] [--grep PATTERN] [--bot BOT]
#
# Options:
#   --fleet NAME      Target fleet (default: all fleets under local/)
#   --lines N         Lines per log file (default: 20)
#   --grep PATTERN    Filter output lines matching this pattern
#   --bot BOT         Show logs for a single bot only
#   --events          Include data/events/*.jsonl (off by default — can be noisy)
#
# Examples:
#   tail-fleet.sh --fleet crog-eng-team
#   tail-fleet.sh --fleet crog-eng-team --grep ERROR
#   tail-fleet.sh --bot astrid --lines 50 --events
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

FLEET=""
LINES=20
GREP_PATTERN=""
BOT_FILTER=""
INCLUDE_EVENTS=0

while [ $# -gt 0 ]; do
    case "$1" in
        --fleet)  FLEET="$2"; shift 2 ;;
        --lines)  LINES="$2"; shift 2 ;;
        --grep)   GREP_PATTERN="$2"; shift 2 ;;
        --bot)    BOT_FILTER="$2"; shift 2 ;;
        --events) INCLUDE_EVENTS=1; shift ;;
        -h|--help) show_help; exit 0 ;;
        *) echo "tail-fleet: unknown option '$1'" >&2; exit 2 ;;
    esac
done

# Resolve fleet directories
if [ -n "$FLEET" ]; then
    FLEET_DIRS=("$CLAUDLOBBY_ROOT/local/$FLEET")
else
    FLEET_DIRS=("$CLAUDLOBBY_ROOT"/local/*)
fi

_found=0

for fleet_dir in "${FLEET_DIRS[@]}"; do
    [ -d "$fleet_dir/runtime/bots" ] || continue
    _fleet_name=$(basename "$fleet_dir")

    for bot_dir in "$fleet_dir"/runtime/bots/*/; do
        [ -d "$bot_dir" ] || continue
        _bot=$(basename "$bot_dir")

        # Filter to a single bot if requested
        [ -n "$BOT_FILTER" ] && [ "$_bot" != "$BOT_FILTER" ] && continue

        # Collect log files: bot root *.log, logs/*.log, logs/*.jsonl
        _logs=()
        for f in "$bot_dir"*.log; do
            [ -f "$f" ] && _logs+=("$f")
        done
        for f in "$bot_dir"logs/*.log "$bot_dir"logs/*.jsonl; do
            [ -f "$f" ] && _logs+=("$f")
        done
        # Optionally include event JSONL files
        if [ "$INCLUDE_EVENTS" -eq 1 ] && [ -d "${bot_dir}data/events" ]; then
            for f in "$bot_dir"data/events/*.jsonl; do
                [ -f "$f" ] && _logs+=("$f")
            done
        fi

        [ "${#_logs[@]}" -eq 0 ] && continue

        for logfile in "${_logs[@]}"; do
            _relpath="${logfile#"$CLAUDLOBBY_ROOT"/}"
            _output=$(tail -n "$LINES" "$logfile" 2>/dev/null) || continue
            [ -z "$_output" ] && continue

            if [ -n "$GREP_PATTERN" ]; then
                _filtered=$(printf '%s\n' "$_output" | grep "$GREP_PATTERN" 2>/dev/null) || true
                [ -z "$_filtered" ] && continue
                _output="$_filtered"
            fi

            _found=1
            printf '=== [%s] %s/%s — %s ===\n' "$_fleet_name" "$_bot" "$(basename "$logfile")" "$_relpath"
            printf '%s\n\n' "$_output"
        done
    done
done

if [ "$_found" -eq 0 ]; then
    if [ -n "$GREP_PATTERN" ]; then
        echo "tail-fleet: no log lines matching '$GREP_PATTERN'"
    else
        echo "tail-fleet: no log files found"
    fi
fi
