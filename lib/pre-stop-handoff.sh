#!/bin/bash
# Pre-stop script: capture session context before systemd kills the bot
# Called by systemd ExecStop before the tmux session is terminated.
#
# Add to your .service file:
#   ExecStop=/path/to/claudlobby/bot-common/pre-stop-handoff.sh /path/to/bot/dir

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT_DIR="${1:?Usage: pre-stop-handoff.sh /path/to/bot/dir}"
load_bot_conf "$BOT_DIR"

HANDOFF_DIR="$HOME/.claude/notes/projects/$(basename "$BOT_DIR")"
HANDOFF_FILE="$HANDOFF_DIR/context-resume.md"

# If a fresh handoff was written in the last 5 minutes, skip
if [ -f "$HANDOFF_FILE" ]; then
    AGE=$(( $(date +%s) - $(stat_mtime "$HANDOFF_FILE" 2>/dev/null || echo 0) ))
    if [ "$AGE" -lt 300 ]; then
        echo "Recent handoff exists ($AGE seconds old), skipping"
        exit 0
    fi
fi

# Try to trigger a handoff via the running session
if check_tmux_session "$BOT_NAME"; then
    "$_TMUX_BIN" send-keys -t "$BOT_NAME" '/session-handoff --auto' Enter || true
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
