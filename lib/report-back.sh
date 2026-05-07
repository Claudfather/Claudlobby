#!/bin/bash
# Inter-bot communication — worker bots report back to manager
# Usage: report-back.sh <bot-name> <status> <summary> [pr:<url>] [issues:<urls>]
#
# The manager bot's tmux session receives a structured message it can parse.
# Format: [BOTREPORT] <bot> | <status> | <summary> [| pr:<url>] [| issues:<urls>]
#
# Example:
#   report-back.sh "work-eng" "DONE" "Fixed auth test" "pr:https://github.com/org/repo/pull/42"

MANAGER_SESSION="${MANAGER_BOT_NAME:-claude-bot}"  # Override in bot.conf if needed
BOT="$1"
STATUS="$2"
SUMMARY="$3"
shift 3

EXTRAS=""
for arg in "$@"; do
    EXTRAS="$EXTRAS | $arg"
done

MESSAGE="[BOTREPORT] $BOT | $STATUS | $SUMMARY$EXTRAS"

/usr/bin/tmux send-keys -t "$MANAGER_SESSION" "$MESSAGE" Enter

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
  "$_FS" "$BOT_NAME" "$FS" "" "" "$SUMMARY" || true
fi
