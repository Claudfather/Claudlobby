#!/bin/bash
# Fleet-level log rotation — discovers and rotates all bot logs.
#
# Walks runtime/bots/<bot>/ for each fleet and rotates the *.log and *.jsonl
# files at each bot's root and in its logs/, plus the host's state/logs/.
#
# Usage:
#   log-rotate-fleet.sh [--keep N] [--fleet <name>] [<name>]
#   log-rotate-fleet.sh --keep 200 --fleet my-fleet
#   log-rotate-fleet.sh my-fleet      # composed-timer form (positional fleet)
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
        -*) echo "log-rotate-fleet: unknown arg: $1" >&2; exit 2 ;;
        *)
            # Bare fleet name — the composed timer contract: _write_timer_units
            # appends the fleet positionally to every fleet job's ExecStart
            # (composer.py), same as fleet-pulse.sh / data-sweep.sh receive it.
            FLEET="$1"; shift ;;
    esac
done

ROTATE="$LIB_DIR/log-rotate.sh"
if [ ! -x "$ROTATE" ]; then
    echo "log-rotate-fleet: missing $ROTATE" >&2
    exit 1
fi

LOGS=()

# Host operation logs live in the data plane, never in immutable package code.
for f in "$CLAUDLOBBY_ROOT"/state/logs/*.log "$CLAUDLOBBY_ROOT"/state/logs/*.jsonl; do
    [ -f "$f" ] && LOGS+=("$f")
done

# Per-fleet bot logs
if [ -n "$FLEET" ]; then
    _fleet_dir=$(resolve_fleet_dir "$FLEET") || _fleet_dir="$CLAUDLOBBY_ROOT/local/$FLEET"
    FLEET_DIRS=("$_fleet_dir")
else
    # Both depths: flat local/<fleet> AND nested local/<system>/<fleet>. The
    # per-dir runtime/bots guard below drops containers + flat-fleet subdirs.
    FLEET_DIRS=("$CLAUDLOBBY_ROOT"/local/* "$CLAUDLOBBY_ROOT"/local/*/*)
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
        # Nothing under data/ is rotated: a bot's data/ holds its own files,
        # and no framework job writes a log there. data-sweep.sh purges no log
        # by name either.
    done
done

if [ "${#LOGS[@]}" -eq 0 ]; then
    exit 0
fi

"$ROTATE" --keep "$KEEP" "${LOGS[@]}"
