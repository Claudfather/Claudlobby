#!/bin/bash
# Cold-start timing benchmark — measures restart latency end-to-end.
#
# Usage: bench-cold-start.sh [bot-name] [--fleet <fleet>] [--notes <text>]
#
#   bot-name   Bot to benchmark. If omitted, looks for a bot with `bench: true`
#              in fleet.yaml (FLEET env var or --fleet flag required in that case).
#   --fleet    Fleet name (overrides FLEET_NAME env var).
#   --notes    Optional free-text note appended to the log row.
#
# Records three timings per run:
#   start_to_rc_seconds      — start-bot.sh invocation → "remote-control is active"
#                              seen in the tmux pane
#   start_to_complete_seconds — start-bot.sh invocation → start-bot.sh exits (0)
#   total_seconds            — wall-clock from first stop to script exit
#
# Results are appended (CSV) to lib/bench-results.log and printed to stdout.
# Create the log if absent; header is written once on first creation.
#
# Columns: timestamp, bot_name, start_to_rc_seconds, start_to_complete_seconds,
#          total_seconds, notes
#
# Example cron (weekly, Sunday 03:00):
#   0 3 * * 0 /home/user/claudlobby/lib/bench-cold-start.sh --fleet myfleet >> /tmp/bench.out 2>&1
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

BOT_ARG=""
FLEET_ARG=""
NOTES_ARG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --fleet)
            FLEET_ARG="${2:?--fleet requires a value}"
            shift 2
            ;;
        --notes)
            NOTES_ARG="${2:?--notes requires a value}"
            shift 2
            ;;
        --help|-h)
            show_help "$0"
            exit 0
            ;;
        -*)
            echo "bench-cold-start: unknown flag '$1'" >&2
            exit 1
            ;;
        *)
            if [ -z "$BOT_ARG" ]; then
                BOT_ARG="$1"
            else
                echo "bench-cold-start: unexpected argument '$1'" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

FLEET_NAME="${FLEET_ARG:-${FLEET_NAME:-}}"

# ---------------------------------------------------------------------------
# Resolve bot name — from arg or bench:true in fleet.yaml
# ---------------------------------------------------------------------------

_find_bench_bot() {
    local fleet_yaml="$1"
    # Parse fleet.yaml for the first bot with a `bench: true` line.
    # Looks for the pattern (4-space indent bot key, then bench: true within
    # that stanza before the next 4-space key).
    awk '
        /^    [a-zA-Z_][a-zA-Z0-9_-]*:[ \t]*$/ { current = $0; gsub(/[ \t:]/, "", current) }
        /^      bench:[ \t]*true[ \t]*$/ && current != "" { print current; exit }
    ' "$fleet_yaml"
}

if [ -z "$BOT_ARG" ]; then
    if [ -z "$FLEET_NAME" ]; then
        echo "bench-cold-start: no bot name given and FLEET_NAME is unset." >&2
        echo "  Pass a bot name or set FLEET_NAME (or use --fleet <name>)." >&2
        exit 1
    fi
    FLEET_YAML="$CLAUDLOBBY_ROOT/local/$FLEET_NAME/fleet.yaml"
    if [ ! -f "$FLEET_YAML" ]; then
        echo "bench-cold-start: fleet.yaml not found: $FLEET_YAML" >&2
        exit 1
    fi
    BOT_NAME="$(_find_bench_bot "$FLEET_YAML")"
    if [ -z "$BOT_NAME" ]; then
        echo "bench-cold-start: no bot with 'bench: true' found in $FLEET_YAML" >&2
        echo "  Add 'bench: true' to a bot stanza or pass the bot name as an argument." >&2
        exit 1
    fi
    echo "bench-cold-start: found bench bot '$BOT_NAME' in $FLEET_YAML"
else
    BOT_NAME="$BOT_ARG"
fi

# ---------------------------------------------------------------------------
# Locate the bot directory
# ---------------------------------------------------------------------------

_find_bot_dir() {
    local bot="$1"
    # 1. Fleet-namespaced runtime (preferred when FLEET_NAME is set)
    if [ -n "$FLEET_NAME" ]; then
        local d="$CLAUDLOBBY_ROOT/local/$FLEET_NAME/runtime/bots/$bot"
        [ -d "$d" ] && { echo "$d"; return 0; }
    fi
    # 2. Root runtime (legacy / fleet-less layout)
    local d="$CLAUDLOBBY_ROOT/runtime/bots/$bot"
    [ -d "$d" ] && { echo "$d"; return 0; }
    # 3. Search all local/<fleet>/runtime/bots/<bot>
    for d in "$CLAUDLOBBY_ROOT"/local/*/runtime/bots/"$bot"; do
        [ -d "$d" ] && { echo "$d"; return 0; }
    done
    return 1
}

BOT_DIR="$(_find_bot_dir "$BOT_NAME")" || {
    echo "bench-cold-start: runtime directory not found for bot '$BOT_NAME'" >&2
    echo "  Run 'claudlobby generate' first." >&2
    exit 1
}

TMUX_SESSION="$(tmux_session_name "$BOT_DIR")"
echo "bench-cold-start: bot dir = $BOT_DIR"

# ---------------------------------------------------------------------------
# Log file setup
# ---------------------------------------------------------------------------

LOG_FILE="$LIB_DIR/bench-results.log"
LOG_HEADER="timestamp,bot_name,start_to_rc_seconds,start_to_complete_seconds,total_seconds,notes"

if [ ! -f "$LOG_FILE" ]; then
    printf '%s\n' "$LOG_HEADER" > "$LOG_FILE"
    echo "bench-cold-start: created $LOG_FILE"
fi

# ---------------------------------------------------------------------------
# Timing helpers — portable high-resolution wall clock
# ---------------------------------------------------------------------------

_now_ns() {
    # Nanosecond-resolution epoch on Linux (date +%s.%N);
    # macOS date accepts %N syntactically but outputs it literally — use
    # integer seconds there. _OS is set by lib-common.sh.
    if [ "${_OS:-}" = "Darwin" ]; then
        date +%s
    else
        date +%s.%N
    fi
}

_elapsed() {
    # _elapsed <start> <end> → decimal seconds (awk does the subtraction)
    local start="$1" end="$2"
    awk "BEGIN { printf \"%.3f\", ($end) - ($start) }"
}

# ---------------------------------------------------------------------------
# Step 1: Record total_start BEFORE the stop
# ---------------------------------------------------------------------------

TOTAL_START="$(_now_ns)"

# ---------------------------------------------------------------------------
# Step 2: Stop the bot (kill tmux session)
# ---------------------------------------------------------------------------

echo "bench-cold-start: stopping '$BOT_NAME'..."

if check_tmux_session "$TMUX_SESSION"; then
    "$_TMUX_BIN" kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    # Brief settle — give the process tree a moment to clean up
    sleep 1
    if check_tmux_session "$TMUX_SESSION"; then
        echo "bench-cold-start: WARNING — session still alive after kill; proceeding anyway" >&2
    else
        echo "bench-cold-start: session stopped."
    fi
else
    echo "bench-cold-start: session '$BOT_NAME' was not running (cold start from scratch)."
fi

# ---------------------------------------------------------------------------
# Step 3: Record start time, invoke start-bot.sh
# ---------------------------------------------------------------------------

START_TIME="$(_now_ns)"
echo "bench-cold-start: starting '$BOT_NAME' via start-bot.sh..."

"$LIB_DIR/start-bot.sh" "$BOT_DIR" &
START_PID=$!

# ---------------------------------------------------------------------------
# Step 4: Poll for "remote-control is active" in the tmux pane
# ---------------------------------------------------------------------------

RC_ACTIVE_TIME=""
RC_TIMEOUT=120   # seconds to wait for remote-control readiness

echo "bench-cold-start: waiting for remote-control (timeout ${RC_TIMEOUT}s)..."

for _i in $(seq 1 "$RC_TIMEOUT"); do
    if "$_TMUX_BIN" capture-pane -t "$TMUX_SESSION" -p 2>/dev/null \
            | grep -q "remote-control is active"; then
        RC_ACTIVE_TIME="$(_now_ns)"
        echo "bench-cold-start: remote-control active at ${_i}s"
        break
    fi
    sleep 1
done

if [ -z "$RC_ACTIVE_TIME" ]; then
    echo "bench-cold-start: WARNING — remote-control not detected within ${RC_TIMEOUT}s" >&2
    RC_ACTIVE_TIME="$(_now_ns)"   # record the timeout mark so we still get a number
fi

# ---------------------------------------------------------------------------
# Step 5: Wait for start-bot.sh to exit
# ---------------------------------------------------------------------------

echo "bench-cold-start: waiting for start-bot.sh to complete..."
wait "$START_PID" && START_EXIT=0 || START_EXIT=$?

COMPLETE_TIME="$(_now_ns)"

if [ "$START_EXIT" -ne 0 ]; then
    echo "bench-cold-start: WARNING — start-bot.sh exited with code $START_EXIT" >&2
fi

# ---------------------------------------------------------------------------
# Step 6: Compute timings
# ---------------------------------------------------------------------------

TOTAL_END="$(_now_ns)"

START_TO_RC="$(_elapsed "$START_TIME" "$RC_ACTIVE_TIME")"
START_TO_COMPLETE="$(_elapsed "$START_TIME" "$COMPLETE_TIME")"
TOTAL="$(_elapsed "$TOTAL_START" "$TOTAL_END")"

TS="$(ts_iso)"

# ---------------------------------------------------------------------------
# Step 7: Print results
# ---------------------------------------------------------------------------

echo ""
echo "=== Cold-Start Benchmark Results ==="
printf '  %-30s %s\n' "Timestamp:"              "$TS"
printf '  %-30s %s\n' "Bot:"                    "$BOT_NAME"
printf '  %-30s %s s\n' "Start → RC active:"    "$START_TO_RC"
printf '  %-30s %s s\n' "Start → start-bot done:" "$START_TO_COMPLETE"
printf '  %-30s %s s\n' "Total wall clock:"      "$TOTAL"
[ -n "$NOTES_ARG" ] && printf '  %-30s %s\n' "Notes:" "$NOTES_ARG"
echo "====================================="

# ---------------------------------------------------------------------------
# Step 8: Append to log
# ---------------------------------------------------------------------------

# Escape any commas in notes so the CSV stays parseable
_NOTES_SAFE="${NOTES_ARG//,/;}"

printf '%s,%s,%s,%s,%s,%s\n' \
    "$TS" \
    "$BOT_NAME" \
    "$START_TO_RC" \
    "$START_TO_COMPLETE" \
    "$TOTAL" \
    "$_NOTES_SAFE" \
    >> "$LOG_FILE"

echo "bench-cold-start: result appended to $LOG_FILE"
