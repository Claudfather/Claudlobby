#!/bin/bash
# Generic cron-driven sweep dispatcher.
#
# Sends a one-shot dispatch into a bot's tmux session — useful for periodic
# work (docs-drift sweeps, alert reviews, status checks) where the bot
# already knows what to do when it sees the trigger word.
#
# Usage: bot-sweep-cron.sh <bot-name> <dispatch-text>
#   <bot-name>: tmux session name (matches the bot's runtime dir name)
#   <dispatch-text>: text sent to the pane (e.g. "SWEEP", "SWEEP DEEP",
#                    "/check-alerts", whatever the bot's CLAUDE.md defines)
#
# Skips dispatch if the pane appears busy (active spinner). Logs to
# $CLAUDLOBBY_ROOT/lib/bot-sweep-cron.log.
set -euo pipefail

BOT_NAME="${1:?Usage: bot-sweep-cron.sh <bot-name> <dispatch-text>}"
DISPATCH="${2:?Usage: bot-sweep-cron.sh <bot-name> <dispatch-text>}"

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
LOG="$CLAUDLOBBY_ROOT/lib/bot-sweep-cron.log"
mkdir -p "$(dirname "$LOG")"
TS=$(date -Iseconds)

# Verify the bot's tmux session is alive.
if ! tmux has-session -t "$BOT_NAME" 2>/dev/null; then
    echo "$TS ERROR — $BOT_NAME tmux session not found" >>"$LOG"
    exit 1
fi

# Don't interrupt in-flight work — skip if pane shows active processing.
PANE_TAIL=$(tmux capture-pane -t "$BOT_NAME" -p 2>&1 | tail -3) || true
if echo "$PANE_TAIL" | grep -qiE '(thinking|running|reading|writing|calling|editing)'; then
    echo "$TS WARN — $BOT_NAME appears busy; skipping this tick ($DISPATCH)" >>"$LOG"
    exit 0
fi

# Fire the dispatch.
tmux send-keys -t "$BOT_NAME" "$DISPATCH" Enter
sleep 1
tmux send-keys -t "$BOT_NAME" Enter
echo "$TS OK — dispatched '$DISPATCH' to $BOT_NAME" >>"$LOG"
