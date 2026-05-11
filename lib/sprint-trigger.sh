#!/bin/bash
# sprint-trigger.sh — schedule-driven nudge to run /autonomous-sprint.
#
# Wire into cron (Linux) or launchd (macOS) to fire N times per day:
#   */360 * * * * ~/claudlobby/lib/sprint-trigger.sh
#
# Skips if manager is busy or not alive. Logs each run.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

MANAGER_TMUX="${MANAGER_TMUX:-claude-bot}"
LOG="${SPRINT_TRIGGER_LOG:-$CLAUDLOBBY_ROOT/logs/sprint-trigger.log}"
setup_log_dir "$LOG"

TS=$(ts_iso)

if ! check_tmux_session "$MANAGER_TMUX"; then
  echo "$TS SKIP — manager '$MANAGER_TMUX' not alive" >> "$LOG"
  exit 0
fi

pane=$("$_TMUX_BIN" capture-pane -t "$MANAGER_TMUX" -p | tail -3) || true
if echo "$pane" | grep -qE '(Thinking|Running|Reading|Writing|Editing|Spelunking|Prestidigitating|esc to interrupt)'; then
  echo "$TS SKIP — manager busy" >> "$LOG"
  exit 0
fi

"$_TMUX_BIN" send-keys -t "$MANAGER_TMUX" "/autonomous-sprint" Enter
echo "$TS DISPATCH — /autonomous-sprint sent to $MANAGER_TMUX" >> "$LOG"
