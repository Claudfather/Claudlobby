#!/bin/bash
# Fleet utilization rollup — refresh state/fleet-utilization.json
#
# Reads the plane's bot.heartbeat samples + fleet-state.json and writes per-bot
# busy/idle % to state/fleet-utilization.json for manager dispatch decisions
# (F18 closure R2b: the plane is the only source; keepalive.log is gone). A
# plane that cannot answer REFUSES at rc 3 -- the file is never rewritten with
# a rollup of zeros.
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

MODE=write
$SUMMARY && MODE=summary
python3 - "$CLAUDLOBBY_ROOT" "$FLEET" "$MODE" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from claudlobby.paths import Paths
from claudlobby.utilization import (PlaneUnreachable, compute_fleet_utilization,
                                    format_utilization_summary, write_utilization_json)
paths = Paths.detect(fleet=sys.argv[2] if sys.argv[2] else None)
try:
    results = compute_fleet_utilization(paths.runtime_bots, paths)
except PlaneUnreachable as exc:
    print(f"fleet-utilization: UNREACHABLE -- {exc}; nothing written", file=sys.stderr)
    sys.exit(3)
if sys.argv[3] == "summary":
    print(format_utilization_summary(results))
else:
    print(f"wrote {write_utilization_json(results, paths)}")
PY
