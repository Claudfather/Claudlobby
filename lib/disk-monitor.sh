#!/bin/bash
# Daily disk-usage warn — checks root partition usage and logs a warning
# when it exceeds the threshold. Optional Telegram alert via tg-post.sh.
#
# Usage:
#   disk-monitor.sh [--threshold N] [--mount /]
#   disk-monitor.sh --threshold 90 --mount /home
#
# Defaults: --threshold 90, --mount /
#
# Output is a single line written to $CLAUDLOBBY_ROOT/lib/disk-monitor.log
# (creating the dir if needed). When the threshold is crossed and tg-post.sh
# is available + TG_CHAT_ID is set in the environment, also fires a Telegram
# alert. Otherwise just logs and exits 0 — non-fatal.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

THRESHOLD=90
MOUNT="/"
while [ $# -gt 0 ]; do
    case "$1" in
        --threshold) THRESHOLD="$2"; shift 2 ;;
        --mount)     MOUNT="$2"; shift 2 ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "disk-monitor: unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
LOG="$CLAUDLOBBY_ROOT/lib/disk-monitor.log"
setup_log_dir "$LOG"
TS=$(ts_iso)

USAGE=$(df_pcent "$MOUNT")

if [ -z "$USAGE" ]; then
    echo "$TS ERROR — could not parse disk usage for $MOUNT" >>"$LOG"
    exit 1
fi

if [ "$USAGE" -gt "$THRESHOLD" ]; then
    msg="WARN — disk usage at ${USAGE}% on $MOUNT (threshold ${THRESHOLD}%)"
    echo "$TS $msg" >>"$LOG"
    TG_POST="$CLAUDLOBBY_ROOT/lib/tg-post.sh"
    if [ -x "$TG_POST" ] && [ -n "${TG_CHAT_ID:-}" ]; then
        "$TG_POST" "$TG_CHAT_ID" "disk-monitor: $msg" >>"$LOG" 2>&1 || \
            echo "$TS ERROR — tg-post failed for disk warning" >>"$LOG"
    fi
else
    echo "$TS OK — disk usage at ${USAGE}% on $MOUNT" >>"$LOG"
fi

# Per-bot data/ directory sizes
FLEET="${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}"
if [ -n "$FLEET" ]; then
    BOTS_DIR="$CLAUDLOBBY_ROOT/local/$FLEET/runtime/bots"
else
    BOTS_DIR="$CLAUDLOBBY_ROOT/runtime/bots"
fi

if [ -d "$BOTS_DIR" ]; then
    echo "$TS BOT DATA SIZES:" >>"$LOG"
    for bot_dir in "$BOTS_DIR"/*/; do
        [ -d "$bot_dir" ] || continue
        data_dir="$bot_dir/data"
        bot_name="$(basename "$bot_dir")"
        if [ -d "$data_dir" ]; then
            size=$(du -sh "$data_dir" 2>/dev/null | cut -f1)
            echo "  $bot_name/data: $size" >>"$LOG"
        fi
    done
fi
