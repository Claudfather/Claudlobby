#!/bin/bash
# sprint-trigger.sh — schedule-driven nudge to run /autonomous-sprint.
#
# Wire into cron (Linux) or launchd (macOS) to fire N times per day:
#   */360 * * * * ~/claudlobby/bot-common/sprint-trigger.sh
#
# Skips if manager is busy or not alive. Logs each run.
set -u
MANAGER_TMUX="${MANAGER_TMUX:-claude-bot}"
LOG="${SPRINT_TRIGGER_LOG:-$HOME/claudlobby/logs/sprint-trigger.log}"
TMUX="${TMUX_BIN:-$(command -v tmux)}"
mkdir -p "$(dirname "$LOG")"

TS=$(date -Iseconds 2>/dev/null || date +%Y-%m-%dT%H:%M:%S%z)

if ! "$TMUX" has-session -t "$MANAGER_TMUX" 2>/dev/null; then
  echo "$TS SKIP — manager '$MANAGER_TMUX' not alive" >> "$LOG"
  exit 0
fi

pane=$("$TMUX" capture-pane -t "$MANAGER_TMUX" -p | tail -3)
if echo "$pane" | grep -qE '(Thinking|Running|Reading|Writing|Editing|Spelunking|Prestidigitating|esc to interrupt)'; then
  echo "$TS SKIP — manager busy" >> "$LOG"
  exit 0
fi

"$TMUX" send-keys -t "$MANAGER_TMUX" "/autonomous-sprint" Enter
echo "$TS DISPATCH — /autonomous-sprint sent to $MANAGER_TMUX" >> "$LOG"
