#!/bin/bash
# fleet-state-update.sh — update a bot row in fleet-state.json.
#
# Called by start-bot.sh (boot → idle) and report-back.sh (completion → idle / blocked).
#
# Usage: fleet-state-update.sh <bot> <status> [<current_task>] [<current_repo>] [<last_completed>]
#   status: idle | working | blocked | offline
set -euo pipefail
CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
STATE="${FLEET_STATE_PATH:-$CLAUDLOBBY_ROOT/state/fleet-state.json}"
mkdir -p "$(dirname "$STATE")"
BOT="${1:?bot}"
STATUS="${2:?status}"
TASK="${3:-}"
REPO="${4:-}"
LAST="${5:-}"

[ -f "$STATE" ] || { echo '{"updated":"1970-01-01T00:00:00Z","bots":{},"queue":[]}' > "$STATE"; }

# Exclusive lock prevents concurrent bot updates from corrupting state
(
flock -x 200
TMP=$(mktemp)
trap "rm -f \"\$TMP\"" EXIT
jq --arg bot "$BOT" --arg status "$STATUS" --arg task "$TASK" --arg repo "$REPO" \
   --arg last "$LAST" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
  .updated = $ts
  | .bots[$bot] //= {"status":"idle","current_task":null,"current_repo":null,"last_completed":null}
  | .bots[$bot].status = $status
  | (if $task != "" then .bots[$bot].current_task = $task else . end)
  | (if $repo != "" then .bots[$bot].current_repo = $repo else . end)
  | (if $last != "" then .bots[$bot].last_completed = $last else . end)
' "$STATE" > "$TMP" && mv "$TMP" "$STATE"
) 200>"$STATE.lock"
