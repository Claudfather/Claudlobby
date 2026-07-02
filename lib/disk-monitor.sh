#!/bin/bash
# Daily disk-usage warn — checks root partition usage and raises a FLEET
# ALERT (fleet event + manager tmux nudge + Telegram) when it exceeds the
# threshold. Runs as the disk-monitor host job (system.yaml host.jobs,
# enrolled by setup-system).
#
# Usage:
#   disk-monitor.sh [--threshold N] [--mount /]
#   disk-monitor.sh --threshold 90 --mount /home
#
# Defaults: --threshold 90, --mount /
#
# Output is written to $CLAUDLOBBY_ROOT/lib/disk-monitor.log (creating the
# dir if needed). Alerting rides the shared emit_failure_alert primitive, so
# delivery works fleet-less (host job) via the cross-fleet fallback. Exits 0
# on OK and on alert — non-fatal.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

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

LOG="$CLAUDLOBBY_ROOT/lib/disk-monitor.log"
setup_log_dir "$LOG"
TS=$(ts_iso)

USAGE=$(df_pcent "$MOUNT")

if [ -z "$USAGE" ]; then
    echo "$TS ERROR — could not parse disk usage for $MOUNT" >>"$LOG"
    exit 1
fi

FLEET="${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}"

if [ "$USAGE" -gt "$THRESHOLD" ]; then
    msg="disk usage at ${USAGE}% on $MOUNT of $(hostname) (threshold ${THRESHOLD}%)"
    echo "$TS WARN — $msg" >>"$LOG"
    emit_failure_alert "$(resolve_bots_dir "$FLEET")" "disk_high" "$msg"
else
    echo "$TS OK — disk usage at ${USAGE}% on $MOUNT" >>"$LOG"
fi

# Per-bot data/ directory sizes. Fleet-scoped when a fleet is set; the host
# job runs fleet-less and reports across every fleet on the host.
if [ -n "$FLEET" ]; then
    BOTS_DIRS=("$CLAUDLOBBY_ROOT/local/$FLEET/runtime/bots")
else
    BOTS_DIRS=("$CLAUDLOBBY_ROOT/runtime/bots" "$CLAUDLOBBY_ROOT"/local/*/runtime/bots)
fi

echo "$TS BOT DATA SIZES:" >>"$LOG"
for bots_dir in "${BOTS_DIRS[@]}"; do
    [ -d "$bots_dir" ] || continue
    for bot_dir in "$bots_dir"/*/; do
        [ -d "$bot_dir" ] || continue
        data_dir="$bot_dir/data"
        bot_name="$(basename "$bot_dir")"
        if [ -d "$data_dir" ]; then
            size=$(du -sh "$data_dir" 2>/dev/null | cut -f1)
            echo "  $bot_name/data: $size" >>"$LOG"
        fi
    done
done
exit 0
