#!/bin/bash
# Pre-stop script: capture session context before systemd kills the bot
# Called by systemd ExecStop before the tmux session is terminated.
#
# Add to your .service file:
#   ExecStop=/path/to/claudlobby/bot-common/pre-stop-handoff.sh /path/to/bot/dir

BOT_DIR="${1:?Usage: pre-stop-handoff.sh /path/to/bot/dir}"
source "$BOT_DIR/bot.conf"

HANDOFF_DIR="$HOME/.claude/notes/projects/$(basename $BOT_DIR)"
HANDOFF_FILE="$HANDOFF_DIR/context-resume.md"

# If a fresh handoff was written in the last 5 minutes, skip
if [ -f "$HANDOFF_FILE" ]; then
    AGE=$(( $(date +%s) - $(stat -c %Y "$HANDOFF_FILE" 2>/dev/null || echo 0) ))
    if [ "$AGE" -lt 300 ]; then
        echo "Recent handoff exists ($AGE seconds old), skipping"
        exit 0
    fi
fi

# Try to trigger a handoff via the running session
if /usr/bin/tmux has-session -t "$BOT_NAME" 2>/dev/null; then
    /usr/bin/tmux send-keys -t "$BOT_NAME" '/session-handoff --auto' Enter
    # Wait up to 30 seconds for handoff to complete
    for i in $(seq 1 30); do
        if [ -f "$HANDOFF_FILE" ]; then
            AGE=$(( $(date +%s) - $(stat -c %Y "$HANDOFF_FILE") ))
            if [ "$AGE" -lt 60 ]; then
                echo "Handoff completed"
                exit 0
            fi
        fi
        sleep 1
    done
    echo "Handoff timed out after 30s"
fi
