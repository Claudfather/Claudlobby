#!/bin/bash
# Fleet utilization rollup — refresh state/fleet-utilization.json
#
# Reads keepalive logs + fleet-state.json and writes per-bot busy/idle %
# to state/fleet-utilization.json for manager dispatch decisions.
#
# Usage:
#   fleet-utilization.sh [--fleet <name>]
#   fleet-utilization.sh --fleet eng-team --summary   # one-line Telegram digest
#
# Intended to be called by the manager bot or cron.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDLOBBY_ROOT="$(cd "$LIB_DIR/.." && pwd)"

# shellcheck source=lib-common.sh
source "$LIB_DIR/lib-common.sh"
install_error_trap ""

FLEET=""
SUMMARY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fleet) FLEET="$2"; shift 2 ;;
        --summary) SUMMARY=true; shift ;;
        *) echo "usage: fleet-utilization.sh [--fleet <name>] [--summary]" >&2; exit 1 ;;
    esac
done

if $SUMMARY; then
    python3 -c "
import sys, shlex
sys.path.insert(0, sys.argv[1])
from claudlobby.paths import Paths
from claudlobby.utilization import compute_fleet_utilization, format_utilization_summary
paths = Paths.detect(fleet=sys.argv[2] if sys.argv[2] else None)
results = compute_fleet_utilization(paths.runtime_bots, paths)
print(format_utilization_summary(results))
" "$CLAUDLOBBY_ROOT" "$FLEET"
else
    python3 -c "
import sys
sys.path.insert(0, sys.argv[1])
from claudlobby.paths import Paths
from claudlobby.utilization import compute_fleet_utilization, write_utilization_json
paths = Paths.detect(fleet=sys.argv[2] if sys.argv[2] else None)
results = compute_fleet_utilization(paths.runtime_bots, paths)
out = write_utilization_json(results, paths)
print(f'wrote {out}')
" "$CLAUDLOBBY_ROOT" "$FLEET"
fi
