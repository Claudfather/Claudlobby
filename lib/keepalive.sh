#!/bin/bash
# Bot keepalive — restart dead sessions, log idle state.
# Usage: keepalive.sh /path/to/bot/dir
#
# IMPORTANT: this script intentionally does NOT press Enter on idle panes.
# Pressing Enter at the prompt submits any "ghost" text — including Claude
# Code's greyed-out auto-completion suggestions — which causes the bot to
# act on input the user never typed. If you want a nudge mechanism, build
# one in your local overlay with eyes open about the input-injection risk.

BOT_DIR="${1:?Usage: keepalive.sh /path/to/bot/dir}"
source "$BOT_DIR/bot.conf"

LOG="$BOT_DIR/keepalive.log"

# Cron runs with a minimal env. `systemctl --user` needs XDG_RUNTIME_DIR to
# reach the user bus, otherwise it fails silently with "Failed to connect to
# bus" while we still log a "RESTART" line — so the bot looks supervised
# but isn't. Set it here if missing. Requires `loginctl enable-linger $USER`
# (recommended by install-bot-systemd.sh) so /run/user/<uid> exists at boot.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# If session is dead, restart the bot's service.
# Pick the right control plane:
#   Linux  → systemctl --user restart $BOT_NAME.service (user units, no sudo)
#   macOS  → launchctl kickstart -k gui/<uid>/<label>   (LaunchAgent)
#   else   → fall back to start-bot.sh directly (cron+tmux pattern)
if ! /usr/bin/tmux has-session -t "$BOT_NAME" 2>/dev/null; then
    UNAME=$(uname)
    if [ "$UNAME" = "Linux" ] && [ -f "$HOME/.config/systemd/user/$BOT_NAME.service" ]; then
        echo "$(date -Iseconds) RESTART — session dead, systemctl --user restart $BOT_NAME" >> "$LOG"
        systemctl --user restart "$BOT_NAME.service" >>"$LOG" 2>&1
    elif [ "$UNAME" = "Darwin" ] && [ -n "$BOT_SERVICE" ] && [ -f "$HOME/Library/LaunchAgents/$BOT_SERVICE.plist" ]; then
        echo "$(date -Iseconds) RESTART — session dead, launchctl kickstart $BOT_SERVICE" >> "$LOG"
        launchctl kickstart -k "gui/$(id -u)/$BOT_SERVICE" >>"$LOG" 2>&1
    else
        echo "$(date -Iseconds) RESTART — session dead, falling back to start-bot.sh $BOT_DIR" >> "$LOG"
        "$(dirname "$0")/start-bot.sh" "$BOT_DIR" >>"$LOG" 2>&1
    fi
    exit 0
fi

pane_content=$(/usr/bin/tmux capture-pane -t "$BOT_NAME" -p 2>/dev/null)
last_lines=$(echo "$pane_content" | tail -10)

# Log state — useful for fleet-health dashboards. Does NOT act on idle.
if echo "$last_lines" | grep -qE '(Running|Thinking|Reading|Writing|Editing)'; then
    echo "$(date -Iseconds) BUSY — active processing" >> "$LOG"
elif echo "$last_lines" | grep -qE '(^\s*[>❯]|Remote Control active|Enter/Esc to close)'; then
    echo "$(date -Iseconds) IDLE — at prompt" >> "$LOG"
else
    echo "$(date -Iseconds) UNKNOWN — pane state did not match known patterns" >> "$LOG"
fi
