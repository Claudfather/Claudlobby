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
#   --fleet-dir PATH  Exact selected fleet source directory (private CLI caller)
#   --lines N         Lines per log file (default: 20)
#   --grep PATTERN    Filter output lines matching this pattern
#   --bot BOT         Show logs for a single bot only
#   (events are not files: `claudlobby events --bot <bot> --tail N` reads them from the plane)
#
# Examples:
#   tail-fleet.sh --fleet my-fleet
#   tail-fleet.sh --fleet my-fleet --grep ERROR
#   tail-fleet.sh --bot astrid --lines 50
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

FLEET=""
FLEET_DIR=""
LINES=20
GREP_PATTERN=""
BOT_FILTER=""

while [ $# -gt 0 ]; do
    case "$1" in
        --fleet)  FLEET="$2"; shift 2 ;;
        --fleet-dir) FLEET_DIR="$2"; shift 2 ;;
        --lines)  LINES="$2"; shift 2 ;;
        --grep)   GREP_PATTERN="$2"; shift 2 ;;
        --bot)    BOT_FILTER="$2"; shift 2 ;;
        --events) echo "tail-fleet: --events is gone -- the fleet's events are on the plane: claudlobby events --bot <bot> --tail N" >&2; exit 2 ;;
        -h|--help) show_help; exit 0 ;;
        *) echo "tail-fleet: unknown option '$1'" >&2; exit 2 ;;
    esac
done
case "$LINES" in ''|*[!0-9]*) echo "tail-fleet: --lines must be a positive integer" >&2; exit 2 ;; esac
[ "$LINES" -ge 1 ] && [ "$LINES" -le 200 ] || {
    echo "tail-fleet: --lines must be 1–200" >&2; exit 2;
}
if [ -n "$FLEET_DIR" ]; then
    case "$FLEET_DIR" in /*) ;; *) echo "tail-fleet: --fleet-dir must be absolute" >&2; exit 2 ;; esac
    [ -n "$FLEET" ] && [ -f "$FLEET_DIR/fleet.yaml" ] || {
        echo "tail-fleet: --fleet-dir requires a selected fleet manifest" >&2; exit 2;
    }
fi

# Resolve fleet directories
if [ -n "$FLEET_DIR" ]; then
    FLEET_DIRS=("$FLEET_DIR")
elif [ -n "$FLEET" ]; then
    _fleet_dir=$(resolve_fleet_dir "$FLEET") || _fleet_dir="$CLAUDLOBBY_ROOT/local/$FLEET"
    FLEET_DIRS=("$_fleet_dir")
else
    # Both depths: flat local/<fleet> AND nested local/<system>/<fleet>. The
    # per-dir runtime/bots guard below drops containers + flat-fleet subdirs.
    FLEET_DIRS=("$CLAUDLOBBY_ROOT"/local/* "$CLAUDLOBBY_ROOT"/local/*/*)
fi

_found=0
_has_files=0
_read_failed=0

for fleet_dir in "${FLEET_DIRS[@]}"; do
    [ -d "$fleet_dir/runtime/bots" ] || continue
    if [ -L "$fleet_dir/runtime" ] || [ -L "$fleet_dir/runtime/bots" ]; then
        _read_failed=1
        continue
    fi
    _fleet_name="${FLEET:-$(basename "$fleet_dir")}"

    for bot_dir in "$fleet_dir"/runtime/bots/*/; do
        [ -d "$bot_dir" ] || continue
        _bot=$(basename "$bot_dir")

        # Filter to a single bot if requested
        [ -n "$BOT_FILTER" ] && [ "$_bot" != "$BOT_FILTER" ] && continue
        if [ -L "${bot_dir%/}" ] || [ -L "${bot_dir}logs" ]; then
            _read_failed=1
            continue
        fi

        # Collect log files: bot root *.log, logs/*.log, logs/*.jsonl
        _logs=()
        for f in "$bot_dir"*.log; do
            [ -f "$f" ] && _logs+=("$f")
        done
        for f in "$bot_dir"logs/*.log "$bot_dir"logs/*.jsonl; do
            [ -f "$f" ] && _logs+=("$f")
        done

        [ "${#_logs[@]}" -eq 0 ] && continue

        for logfile in "${_logs[@]}"; do
            _has_files=1
            if [ -L "$logfile" ]; then _read_failed=1; continue; fi
            _relpath="${logfile#"$CLAUDLOBBY_ROOT"/}"
            # A line can be arbitrarily long. Limit the bytes BEFORE Bash's
            # command substitution, then refuse a suffix that could begin in
            # one of the requested lines instead of printing a partial line.
            _max_bytes=65536
            case "$(uname -s)" in
                Darwin) _size=$(stat -f %z "$logfile" 2>/dev/null) ;;
                *) _size=$(stat -c %s "$logfile" 2>/dev/null) ;;
            esac || { _read_failed=1; continue; }
            # The sentinel keeps trailing newlines intact in command substitution.
            _suffix=$(tail -c "$_max_bytes" "$logfile" 2>/dev/null && printf '\034') || { _read_failed=1; continue; }
            _suffix="${_suffix%$'\034'}"
            if [ "$_size" -gt "$_max_bytes" ]; then
                _breaks=$(printf '%s' "$_suffix" | tr -cd '\n' | wc -c)
                if [ "$_breaks" -le "$LINES" ]; then _read_failed=1; continue; fi
            fi
            _output=$(printf '%s' "$_suffix" | tail -n "$LINES") || { _read_failed=1; continue; }
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
if [ "$_read_failed" -ne 0 ]; then
    echo "tail-fleet: selected log source could not be read safely" >&2
    exit 3
fi

if [ "$_found" -eq 0 ]; then
    if [ -n "$GREP_PATTERN" ]; then
        echo "tail-fleet: no log lines matching '$GREP_PATTERN'"
    elif [ "$_has_files" -ne 0 ]; then
        echo "tail-fleet: log files contain no lines"
    else
        echo "tail-fleet: no log files found"
    fi
fi
