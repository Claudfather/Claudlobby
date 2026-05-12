#!/bin/bash
# fleet-state-update.sh — update a bot row in fleet-state.json.
#
# Called by start-bot.sh (boot → idle) and report-back.sh (completion → idle / blocked).
#
# Usage:
#   fleet-state-update.sh <bot> <status> [<current_task>] [<current_repo>] [<last_completed>]
#     status: idle | working | blocked | offline
#
#   fleet-state-update.sh prune <fleet-yaml-path>
#     Remove bot entries not present in the given fleet.yaml.
#
# Scaling note: the single-file + lock design works well for <50 bots.
# Beyond that, consider per-bot state files (state/<bot>.json) or a
# lightweight SQLite database to reduce lock contention.
#
# Locking is via lib-common's with_lock — flock(1) where available, otherwise
# an atomic mkdir-based spinlock (stock macOS has no flock).
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
STATE="${FLEET_STATE_PATH:-$CLAUDLOBBY_ROOT/state/fleet-state.json}"
mkdir -p "$(dirname "$STATE")"

# --- Prune subcommand ---------------------------------------------------------
if [ "${1:-}" = "prune" ]; then
    FLEET_YAML="${2:?Usage: fleet-state-update.sh prune <fleet-yaml-path>}"
    if [ ! -f "$FLEET_YAML" ]; then
        echo "fleet-state-update: $FLEET_YAML not found" >&2
        exit 1
    fi
    [ -f "$STATE" ] || exit 0  # nothing to prune

    # Extract bot names from fleet.yaml (same parser as reconcile-fleet.sh)
    DEFINED=$(awk '
        /^  bots:[ \t]*$/ {in_bots=1; next}
        in_bots && /^    [a-zA-Z_][a-zA-Z0-9_-]*:[ \t]*$/ {
            gsub(/[ \t:]/, "", $0); print
        }
        in_bots && /^  [a-zA-Z_]/ && !/^    / {in_bots=0}
    ' "$FLEET_YAML")

    # Build a JSON object of defined bot names for jq --argjson
    JQ_KEEP=$(printf '%s\n' "$DEFINED" | awk '{printf "\"%s\": 1, ", $0}' | sed 's/, $//')

    _prune_state() {
        local tmp pruned
        pruned=$(jq -r --argjson keep "{${JQ_KEEP}}" '.bots | keys[] | select($keep[.] == null)' "$STATE")
        [ -z "$pruned" ] && return 0
        tmp=$(safe_mktemp)
        jq --argjson keep "{${JQ_KEEP}}" '.bots |= with_entries(select($keep[.key] == 1))' "$STATE" > "$tmp" && mv "$tmp" "$STATE"
        echo "Pruned from fleet-state: $pruned"
    }
    with_lock "$STATE.lock" _prune_state
    exit 0
fi

# --- Normal update ------------------------------------------------------------
BOT="${1:?bot}"
STATUS="${2:?status}"
TASK="${3:-}"
REPO="${4:-}"
LAST="${5:-}"

[ -f "$STATE" ] || { echo '{"updated":"1970-01-01T00:00:00Z","bots":{},"queue":[]}' > "$STATE"; }

# Exclusive lock prevents concurrent bot updates from corrupting state
_update_state() {
    local tmp
    tmp=$(safe_mktemp)
    jq --arg bot "$BOT" --arg status "$STATUS" --arg task "$TASK" --arg repo "$REPO" \
       --arg last "$LAST" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
      .updated = $ts
      | .bots[$bot] //= {"status":"idle","current_task":null,"current_repo":null,"last_completed":null}
      | .bots[$bot].status = $status
      | (if $task != "" then .bots[$bot].current_task = $task else . end)
      | (if $repo != "" then .bots[$bot].current_repo = $repo else . end)
      | (if $last != "" then .bots[$bot].last_completed = $last else . end)
    ' "$STATE" > "$tmp" && mv "$tmp" "$STATE"
}
with_lock "$STATE.lock" _update_state
