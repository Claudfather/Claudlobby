#!/bin/bash
# Generic cron-driven sweep dispatcher.
#
# Sends a one-shot dispatch into a bot's tmux session — useful for periodic
# work (docs-drift sweeps, alert reviews, status checks) where the bot
# already knows what to do when it sees the trigger word.
#
# Usage: bot-sweep-cron.sh <bot-name> <dispatch-text> [socket]
#   <bot-name>: tmux session name (matches the bot's runtime dir name)
#   <dispatch-text>: text sent to the pane (e.g. "SWEEP", "SWEEP DEEP",
#                    "/check-alerts", whatever the bot's CLAUDE.md defines)
#   [socket]: the bot's per-bot tmux server socket; if omitted, reverse-resolved
#             from <bot-name> (callers that already know the bot's dir pass it)
#
# Skips dispatch if the pane appears busy (active spinner). Logs to
# $CLAUDLOBBY_ROOT/lib/bot-sweep-cron.log.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

BOT_NAME="${1:?Usage: bot-sweep-cron.sh <bot-name> <dispatch-text> [socket]}"
DISPATCH="${2:?Usage: bot-sweep-cron.sh <bot-name> <dispatch-text> [socket]}"
# Per-bot tmux server socket: callers that know the bot's dir (e.g.
# code-audit-sweep.sh) pass it as $3; otherwise reverse-resolve from the name.
# (Sanitization now lives in bot_tmux_send, the one safe-send primitive.)
BOT_SOCKET="${3:-}"
[ -n "$BOT_SOCKET" ] || BOT_SOCKET="$(tmux_socket_for_session "$BOT_NAME" 2>/dev/null || true)"

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
LOG="$CLAUDLOBBY_ROOT/lib/bot-sweep-cron.log"
mkdir -p "$(dirname "$LOG")"
TS=$(date -Iseconds)

# Verify the bot's tmux session is alive.
if ! check_tmux_session "$BOT_NAME" "$BOT_SOCKET"; then
    echo "$TS ERROR — $BOT_NAME tmux session not found" >>"$LOG"
    exit 1
fi

# Don't interrupt in-flight work — skip if pane shows active processing.
PANE_TAIL=$(bot_tmux "$BOT_SOCKET" capture-pane -t "$BOT_NAME" -p 2>&1 | tail -3) || true
if echo "$PANE_TAIL" | grep -qiE '(thinking|running|reading|writing|calling|editing)'; then
    echo "$TS WARN — $BOT_NAME appears busy; skipping this tick ($DISPATCH)" >>"$LOG"
    exit 0
fi

# Fire the dispatch via the one safe cross-socket primitive (precheck + two-step
# send + logged send_miss on a miss).
if bot_tmux_send "$BOT_SOCKET" "$BOT_NAME" "$DISPATCH"; then
    echo "$TS OK — dispatched '$DISPATCH' to $BOT_NAME" >>"$LOG"
else
    echo "$TS ERROR — dispatch to $BOT_NAME failed (logged as send_miss)" >>"$LOG"
    exit 1
fi
