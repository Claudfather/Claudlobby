#!/bin/bash
# Pre-stop script: capture session context before systemd kills the bot
# Called by systemd ExecStop before the tmux session is terminated.
#
# Add to your .service file:
#   ExecStop=/path/to/claudlobby/lib/pre-stop-handoff.sh /path/to/bot/dir

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT_DIR="${1:?Usage: pre-stop-handoff.sh /path/to/bot/dir}"

# Clean up .tmux-env on ALL exit paths (including early returns and the
# socket-resolution guard below). This file holds resolved secrets written by
# start-bot.sh, so install the trap before anything that can exit early.
trap 'rm -f "$BOT_DIR/.tmux-env"' EXIT
install_error_trap "$BOT_DIR"
load_bot_conf "$BOT_DIR"
TMUX_SESSION="$(tmux_session_name "$BOT_DIR")"
# Per-bot tmux server socket (see start-bot.sh) — handoff is sent on the bot's
# OWN server. Fail fast (like start-bot.sh) if it can't be resolved rather than
# aborting silently on errexit.
TMUX_SOCKET="$(tmux_socket_for_bot "$BOT_DIR")" || {
    echo "pre-stop-handoff.sh: cannot resolve tmux socket for $BOT_DIR (check BOT_SERVICE in bot.conf)" >&2
    exit 1
}

# clauDNA's redesigned /session-handoff (May 2026) writes to <cwd>/.claude/session.md,
# where cwd is the bot's runtime dir (start-bot.sh `cd "$BOT_DIR"` before tmux).
HANDOFF_FILE="$BOT_DIR/.claude/session.md"

# If a fresh handoff was written in the last 5 minutes, skip
if [ -f "$HANDOFF_FILE" ]; then
    AGE=$(( $(date +%s) - $(stat_mtime "$HANDOFF_FILE" 2>/dev/null || echo 0) ))
    if [ "$AGE" -lt 300 ]; then
        echo "Recent handoff exists ($AGE seconds old), skipping"
        exit 0
    fi
fi

# Try to trigger a handoff via the running session
if check_tmux_session "$TMUX_SESSION" "$TMUX_SOCKET"; then
    bot_tmux "$TMUX_SOCKET" send-keys -t "$TMUX_SESSION" '/claudna:session-handoff --auto' Enter || true
    # Wait up to 30 seconds for handoff to complete
    for _ in $(seq 1 30); do
        if [ -f "$HANDOFF_FILE" ]; then
            AGE=$(( $(date +%s) - $(stat_mtime "$HANDOFF_FILE" 2>/dev/null || echo 0) ))
            if [ "$AGE" -lt 60 ]; then
                echo "Handoff completed"
                exit 0
            fi
        fi
        sleep 1
    done
    echo "Handoff timed out after 30s"
fi

# Best-effort handoff: never block the restart. Explicit exit 0 so the timeout
# and no-running-session fall-throughs return success — intentional-restart
# callers (the restart skill, weekly-worker-restart.sh) always proceed.
exit 0
