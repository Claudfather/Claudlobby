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
# A file of N lines or fewer is left alone: same inode, mode, owner and
# times. A longer one is cut to its last N lines and keeps its mode.
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
            sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) LOGS+=("$1"); shift ;;
    esac
done

case "$KEEP" in
    ''|*[!0-9]*)
        echo "log-rotate.sh: --keep takes a whole number of lines, got '$KEEP'" >&2
        exit 2
        ;;
esac

if [ "${#LOGS[@]}" -eq 0 ]; then
    echo "log-rotate.sh: no log paths given" >&2
    exit 2
fi

for log in "${LOGS[@]}"; do
    [ -f "$log" ] || continue
    # Count at most KEEP + 1 lines from the end, as tail counts them: awk
    # counts a last line that has no newline, and wc -l does not. A file
    # that cannot be read is skipped.
    lines=$(tail -n "$((10#$KEEP + 1))" "$log" | awk 'END { print NR }' || true)
    case "$lines" in ''|*[!0-9]*) continue ;; esac
    [ "$lines" -gt "$KEEP" ] || continue
    # The temp is a mktemp file (0600) and the rename puts it in the log's
    # place, so it takes the log's mode first.
    mode=$(stat_mode "$log" || true)
    [ -n "$mode" ] || continue
    tmp=$(safe_mktemp)
    tail -n "$KEEP" "$log" > "$tmp" || continue
    chmod "$mode" "$tmp" || continue
    mv "$tmp" "$log"
done
