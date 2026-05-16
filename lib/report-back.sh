#!/bin/bash
# Inter-bot communication — worker bots report back to manager
# Usage: report-back.sh <bot-name> <status> <summary> [pr:<url>] [issues:<urls>]
#
# The manager bot's tmux session receives a structured message it can parse.
# Format: [BOTREPORT] <bot> | <status> | <summary> [| pr:<url>] [| issues:<urls>]
#
# Example:
#   report-back.sh "work-eng" "DONE" "Fixed auth test" "pr:https://github.com/org/repo/pull/42"

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

MANAGER_SESSION="${MANAGER_TMUX:-claude-bot}"
BOT="$1"
STATUS="$2"
SUMMARY="$3"
shift 3

# Validate status against allowed set
case "$STATUS" in
    completed|progress|blocked|failed) ;;
    *)
        echo "report-back: invalid status '$STATUS' (must be: completed|progress|blocked|failed)" >&2
        exit 1
        ;;
esac

# Sanitize summary for tmux safety
SUMMARY="$(sanitize_tmux_input "$SUMMARY")"

EXTRAS=""
for arg in "$@"; do
    EXTRAS="$EXTRAS | $arg"
done

MESSAGE="[BOTREPORT] $BOT | $STATUS | $SUMMARY$EXTRAS"

"$_TMUX_BIN" send-keys -t "$MANAGER_SESSION" "$MESSAGE"
sleep 0.3
"$_TMUX_BIN" send-keys -t "$MANAGER_SESSION" Enter || true

# Mirror to fleet-state if helper is present
_FS=$(dirname "$0")/fleet-state-update.sh
if [ -x "$_FS" ]; then
  case "$STATUS" in
    completed) FS=idle ;;
    blocked)   FS=blocked ;;
    failed)    FS=idle ;;
    progress)  FS=working ;;
    *)         FS=idle ;;
  esac
  "$_FS" "$BOT" "$FS" "" "" "$SUMMARY" || true
fi
