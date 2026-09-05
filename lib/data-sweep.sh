#!/bin/bash
# data-sweep.sh — report per-bot data/ directory sizes and optionally
# remove known-ephemeral files older than N days.
#
# Usage:
#   data-sweep.sh [--purge] [--days N] [<fleet-name>]
#     (report only by default; --purge deletes known-ephemeral files older
#      than N days, default 30. Composed fleet units pass flags first and
#      the fleet name last — the uniform fleet-job arg convention.)
#
# Purge scope is an allowlist of known-ephemeral classes (see
# find_stale_ephemeral for the contract); directories are always left
# intact.
#
# Reports sizes to stdout and to $CLAUDLOBBY_ROOT/lib/logs/data-sweep.log.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

PURGE=0
DAYS=30
FLEET=""
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
        -*)
            echo "data-sweep: unknown arg: $1" >&2
            exit 2
            ;;
        *) FLEET="$1"; shift ;;
    esac
done

LOG="$CLAUDLOBBY_ROOT/lib/logs/data-sweep.log"
setup_log_dir "$LOG"
TS=$(ts_iso)

# The only file classes purge may remove — protect by default. A pattern
# belongs here only when the class is regenerable or append-only history
# nothing reads back after the retention window:
#   (events/*.jsonl   were the framework event stream until the F18 closure;
#                     nothing writes them and nothing reads them -- the plane
#                     holds the events, plane prune ages its samples. Any file
#                     left is the operator's archive, not this sweep's.)
#   vetted log names  the known text logs, same name set log-rotate-fleet.sh
#                     rotates under data/ (keep in lockstep). A bare *.log
#                     glob would also match binary LevelDB / browser-profile
#                     logs — live state, not logs.
#   *.bak             backup copies
#   .plane-rc-relay-* the RC-relay Stop hook's dedupe markers (0-byte, one per relayed turn)
# Durable assets (scripts, configs, ledgers, drafts) never match and are
# never swept, wherever they sit under data/.
find_stale_ephemeral() {
    find "$1" -type f \
        \( -name 'cron.log' -o -name 'git-pull.log' \
           -o -name 'briefing*.log' -o -name 'home-assistant.log' \
           -o -name '*.bak' \
           -o -name '.plane-rc-relay-*' \) \
        -mtime +"$2" -print0 2>/dev/null
}

BOTS_DIR=$(resolve_bots_dir "$FLEET")

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
        old_count=$(find_stale_ephemeral "$data_dir" "$DAYS" | tr -dc '\0' | wc -c | tr -d ' ')
        if [ "$old_count" -gt 0 ]; then
            find_stale_ephemeral "$data_dir" "$DAYS" | xargs -0 rm -f
            echo "    purged $old_count ephemeral files older than $DAYS days" | tee -a "$LOG"
        fi
    fi
done
