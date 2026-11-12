#!/bin/bash
# Generic log truncation — tail each log to the last N lines.
#
# Designed to run weekly via cron or systemd timer. Cheap and safe; trivially
# idempotent (running twice in a row produces the same result).
#
# Usage:
#   log-rotate.sh [--keep N] <log-path> [<log-path> ...]
#   log-rotate.sh --keep 200 ~/myapp/foo.log ~/myapp/bar.log
#
# Defaults to keeping the last 500 lines per file. Files that don't exist
# are silently skipped (not an error — they just haven't been created yet).
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

KEEP=500
LOGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) LOGS+=("$1"); shift ;;
    esac
done

if [ "${#LOGS[@]}" -eq 0 ]; then
    echo "log-rotate.sh: no log paths given" >&2
    exit 2
fi

for log in "${LOGS[@]}"; do
    [ -f "$log" ] || continue
    tmp=$(safe_mktemp)
    tail -n "$KEEP" "$log" > "$tmp" && mv "$tmp" "$log"
done
