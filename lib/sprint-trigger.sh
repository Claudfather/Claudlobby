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
install_error_trap ""

MANAGER_TMUX="${MANAGER_TMUX:-claude-bot}"
# Manager's private tmux server socket: prefer the composed field, else
# reverse-look-up from its session name.
MANAGER_SOCKET="$(resolve_peer_socket "${MANAGER_TMUX_SOCKET:-}" "$MANAGER_TMUX")"
LOG="${SPRINT_TRIGGER_LOG:-$CLAUDLOBBY_ROOT/logs/sprint-trigger.log}"
setup_log_dir "$LOG"

TS=$(ts_iso)

if ! check_tmux_session "$MANAGER_TMUX" "$MANAGER_SOCKET"; then
  echo "$TS SKIP — manager '$MANAGER_TMUX' not alive" >> "$LOG"
  exit 0
fi

pane=$(bot_tmux "$MANAGER_SOCKET" capture-pane -t "$MANAGER_TMUX" -p | tail -3) || true
if echo "$pane" | grep -qE '(Thinking|Running|Reading|Writing|Editing|Spelunking|Prestidigitating|esc to interrupt)'; then
  echo "$TS SKIP — manager busy" >> "$LOG"
  exit 0
fi

if bot_tmux_send "$MANAGER_SOCKET" "$MANAGER_TMUX" "/autonomous-sprint"; then
  echo "$TS DISPATCH — /autonomous-sprint sent to $MANAGER_TMUX" >> "$LOG"
else
  echo "$TS SKIP — send to $MANAGER_TMUX failed (logged as send_miss)" >> "$LOG"
fi
