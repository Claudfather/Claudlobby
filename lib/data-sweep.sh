#!/bin/bash
# data-sweep.sh — report per-bot data/ directory sizes and optionally
# remove files older than N days.
#
# Usage:
#   data-sweep.sh                        # report only
#   data-sweep.sh --purge                # delete files older than 30 days
#   data-sweep.sh --purge --days 14      # delete files older than 14 days
#
# Reports sizes to stdout and to $CLAUDLOBBY_ROOT/lib/logs/data-sweep.log.
# Purge mode only removes regular files — directories are left intact.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

PURGE=0
DAYS=30
while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1; shift ;;
        --days)  DAYS="$2"; shift 2 ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "data-sweep: unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
LOG="$CLAUDLOBBY_ROOT/lib/logs/data-sweep.log"
setup_log_dir "$LOG"
TS=$(ts_iso)

FLEET="${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}"
if [ -n "$FLEET" ]; then
    BOTS_DIR="$CLAUDLOBBY_ROOT/local/$FLEET/runtime/bots"
else
    BOTS_DIR="$CLAUDLOBBY_ROOT/runtime/bots"
fi

if [ ! -d "$BOTS_DIR" ]; then
    echo "$TS ERROR — bots dir not found: $BOTS_DIR" | tee -a "$LOG"
    exit 1
fi

echo "$TS DATA SWEEP (purge=$PURGE, days=$DAYS)" | tee -a "$LOG"

for bot_dir in "$BOTS_DIR"/*/; do
    [ -d "$bot_dir" ] || continue
    data_dir="$bot_dir/data"
    bot_name="$(basename "$bot_dir")"
    if [ ! -d "$data_dir" ]; then
        continue
    fi

    size=$(du -sh "$data_dir" 2>/dev/null | cut -f1)
    file_count=$(find "$data_dir" -type f 2>/dev/null | wc -l | tr -d ' ')
    echo "  $bot_name: $size ($file_count files)" | tee -a "$LOG"

    if [ "$PURGE" -eq 1 ]; then
        old_files=$(find "$data_dir" -type f -mtime +"$DAYS" 2>/dev/null)
        if [ -n "$old_files" ]; then
            old_count=$(printf '%s\n' "$old_files" | wc -l | tr -d ' ')
            printf '%s\n' "$old_files" | xargs rm -f
            echo "    purged $old_count files older than $DAYS days" | tee -a "$LOG"
        fi
    fi
done
