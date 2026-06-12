#!/bin/bash
# Fleet-level log rotation — discovers and rotates all bot logs.
#
# Walks runtime/bots/<bot>/ for each fleet and rotates *.log files,
# plus lib/logs/ for package-level logs.
#
# Usage:
#   log-rotate-fleet.sh [--keep N] [--fleet <name>]
#   log-rotate-fleet.sh --keep 200 --fleet crog
#
# Without --fleet, rotates logs for ALL fleets under local/.
# Defaults to keeping the last 500 lines per file.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""
KEEP=500
FLEET=""

while [ $# -gt 0 ]; do
    case "$1" in
        --keep)  KEEP="$2"; shift 2 ;;
        --fleet) FLEET="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "log-rotate-fleet: unknown arg: $1" >&2; exit 2 ;;
    esac
done

ROTATE="$CLAUDLOBBY_ROOT/lib/log-rotate.sh"
if [ ! -x "$ROTATE" ]; then
    echo "log-rotate-fleet: missing $ROTATE" >&2
    exit 1
fi

LOGS=()

# Package-level logs
for f in "$CLAUDLOBBY_ROOT"/lib/logs/*.log "$CLAUDLOBBY_ROOT"/lib/logs/*.jsonl "$CLAUDLOBBY_ROOT"/lib/*.log "$CLAUDLOBBY_ROOT"/lib/*.jsonl; do
    [ -f "$f" ] && LOGS+=("$f")
done

# Per-fleet bot logs
if [ -n "$FLEET" ]; then
    FLEET_DIRS=("$CLAUDLOBBY_ROOT/local/$FLEET")
else
    FLEET_DIRS=("$CLAUDLOBBY_ROOT"/local/*)
fi

for fleet_dir in "${FLEET_DIRS[@]}"; do
    [ -d "$fleet_dir/runtime/bots" ] || continue
    for bot_dir in "$fleet_dir"/runtime/bots/*/; do
        [ -d "$bot_dir" ] || continue
        # Bot root logs
        for f in "$bot_dir"*.log "$bot_dir"*.jsonl; do
            [ -f "$f" ] && LOGS+=("$f")
        done
        # Bot logs/ subdir
        for f in "$bot_dir"logs/*.log "$bot_dir"logs/*.jsonl; do
            [ -f "$f" ] && LOGS+=("$f")
        done
        # Bot data/ logs (only named cron.log, briefing*.log, git-pull.log, etc.)
        # Skip binary logs (LevelDB, browser profiles) by matching known text log names.
        if [ -d "${bot_dir}data" ]; then
            while IFS= read -r -d '' f; do
                LOGS+=("$f")
            done < <(find "${bot_dir}data" -maxdepth 3 -type f \
                \( -name 'cron.log' -o -name 'git-pull.log' -o -name 'briefing*.log' \
                   -o -name 'home-assistant.log' -o -name 'activity.jsonl' \) \
                -print0 2>/dev/null)
        fi
    done
done

if [ "${#LOGS[@]}" -eq 0 ]; then
    exit 0
fi

"$ROTATE" --keep "$KEEP" "${LOGS[@]}"
