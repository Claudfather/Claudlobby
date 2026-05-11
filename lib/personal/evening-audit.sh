#!/bin/bash
# Trigger an automated code audit in the manager bot's session
# Schedule via cron: 0 21 * * 1-5 (weekday evenings)
#
# The manager bot picks up the prompt and uses global skills
# like /tech-debt or /security-audit to run the audit.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib-common.sh
. "$LIB_DIR/lib-common.sh"

MANAGER_SESSION="${1:-clog}"

if ! check_tmux_session "$MANAGER_SESSION"; then
    echo "$(ts_iso): No manager session, skipping evening audit"
    exit 0
fi

"$_TMUX_BIN" send-keys -t "$MANAGER_SESSION" 'Run a rolling code audit. Check which repo/area is most stale using the audit tracker, then run /tech-debt or /security-audit on it. Log the results.' Enter
