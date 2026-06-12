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
install_error_trap ""

PURGE=0
DAYS=30
while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1; shift ;;
        --days)
            DAYS="$2"
            if ! [[ "$DAYS" =~ ^[1-9][0-9]*$ ]]; then
                echo "data-sweep: --days must be a positive integer, got '$DAYS'" >&2
                exit 2
            fi
            shift 2 ;;
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

LOG="$CLAUDLOBBY_ROOT/lib/logs/data-sweep.log"
setup_log_dir "$LOG"
TS=$(ts_iso)

BOTS_DIR=$(resolve_bots_dir)

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
        old_count=$(find "$data_dir" -type f -mtime +"$DAYS" -print0 2>/dev/null | tr -dc '\0' | wc -c | tr -d ' ')
        if [ "$old_count" -gt 0 ]; then
            find "$data_dir" -type f -mtime +"$DAYS" -print0 2>/dev/null | xargs -0 rm -f
            echo "    purged $old_count files older than $DAYS days" | tee -a "$LOG"
        fi
    fi
done
