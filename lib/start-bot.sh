#!/bin/bash
# Shared bot start script — called by each bot's systemd service
# Usage: start-bot.sh /path/to/bot/dir
set -euo pipefail

BOT_DIR="${1:?Usage: start-bot.sh /path/to/bot/dir}"
source "$BOT_DIR/bot.conf"

export PATH=/usr/local/bin:/usr/bin:/bin:$HOME/.bun/bin:$HOME/.npm-global/bin
export HOME="$HOME"

# 3-tier env sourcing: global → fleet → bot (later tiers override)
[ -f "${CLAUDLOBBY_ROOT:-}/.env" ] && . "$CLAUDLOBBY_ROOT/.env"
if [ -n "${FLEET_NAME:-}" ] && [ -n "${CLAUDLOBBY_ROOT:-}" ]; then
    FLEET_ENV="$CLAUDLOBBY_ROOT/local/$FLEET_NAME/.env"
    # shellcheck source=/dev/null
    [ -f "$FLEET_ENV" ] && . "$FLEET_ENV"
fi
[ -f "$BOT_DIR/.env" ] && . "$BOT_DIR/.env"

# Resolve the Telegram token from the env-var name declared in fleet.yaml
if [ -n "${TELEGRAM_TOKEN_ENV_NAME:-}" ]; then
    export TELEGRAM_BOT_TOKEN="${!TELEGRAM_TOKEN_ENV_NAME:-}"
fi

cd "$BOT_DIR"

# Kill any prior session — expected to fail on first boot or after clean shutdown
tmux kill-session -t "$BOT_NAME" 2>/dev/null || true

SESSION_NAME="$BOT_LABEL-$(date '+%Y%m%d-%H%M')"

# Build claude command. CLAUDE_FLAGS comes from bot.conf (composed by
# `claudlobby generate` from fleet.yaml). It contains all the per-bot
# CLI flags: --channels, --remote-control, --dangerously-skip-permissions,
# --model, --effort, plus any extras. We add only --name here since it
# uses a per-launch timestamp.
#
# IMPORTANT: tmux runs ONE SERVER PER USER. The first bot to start creates
# the server, and the server's environment becomes the inherited default
# for every subsequent session spawned in it. Anything not set explicitly
# leaks from whichever bot created the server — including, critically,
# tokens and identity vars.
#
# Bug pattern: workers can end up posting to Telegram under another
# bot's identity if TELEGRAM_BOT_TOKEN isn't set per-session — they
# inherit whatever TOKEN the tmux server was first started with.
#
# Fix: per-bot env vars are written to a chmod-600 env file and sourced
# by the tmux session's shell. This avoids unquoted tokens in command
# strings and process listings while ensuring per-session isolation.
BOT_ENV_FILE="$BOT_DIR/.tmux-env"
(umask 177; : > "$BOT_ENV_FILE")
chmod 600 "$BOT_ENV_FILE"
[ -n "${CLAUDE_CONFIG_DIR:-}" ]                    && printf 'export CLAUDE_CONFIG_DIR=%q\n' "$CLAUDE_CONFIG_DIR" >> "$BOT_ENV_FILE"
[ -n "${TELEGRAM_STATE_DIR:-}" ]                   && printf 'export TELEGRAM_STATE_DIR=%q\n' "$TELEGRAM_STATE_DIR" >> "$BOT_ENV_FILE"
[ -n "${TELEGRAM_BOT_TOKEN:-}" ]                   && printf 'export TELEGRAM_BOT_TOKEN=%q\n' "$TELEGRAM_BOT_TOKEN" >> "$BOT_ENV_FILE"
[ -n "${TELEGRAM_BOT_HANDLE:-}" ]                  && printf 'export TELEGRAM_BOT_HANDLE=%q\n' "$TELEGRAM_BOT_HANDLE" >> "$BOT_ENV_FILE"
[ -n "${TELEGRAM_GROUP_CHAT_ID:-}" ]               && printf 'export TELEGRAM_GROUP_CHAT_ID=%q\n' "$TELEGRAM_GROUP_CHAT_ID" >> "$BOT_ENV_FILE"
[ -n "${CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION:-}" ] && printf 'export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=%q\n' "$CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION" >> "$BOT_ENV_FILE"

# Backwards-compat: if the bot.conf is from before the CLAUDE_FLAGS
# rename, fall back to the legacy hardcoded flag set + CLAUDE_EXTRA_FLAGS.
if [ -z "${CLAUDE_FLAGS:-}" ]; then
    CLAUDE_FLAGS="--channels plugin:telegram@claude-plugins-official --remote-control --dangerously-skip-permissions"
    [ -n "${CLAUDE_EXTRA_FLAGS:-}" ] && CLAUDE_FLAGS="$CLAUDE_FLAGS $CLAUDE_EXTRA_FLAGS"
fi

CLAUDE_CMD=". '$BOT_ENV_FILE' && exec claude $CLAUDE_FLAGS --name \"$SESSION_NAME\""

tmux new-session -d -s "$BOT_NAME" "$CLAUDE_CMD"

# Wait for initialization (up to 90s)
for _ in $(seq 1 90); do
    if tmux capture-pane -t "$BOT_NAME" -p 2>/dev/null | grep -q "remote-control is active"; then
        break
    fi
    sleep 1
done

sleep 5  # buffer for MCP servers and channels

if [ -n "${STARTUP_PROMPT:-}" ]; then
    tmux send-keys -t "$BOT_NAME" "$STARTUP_PROMPT" Enter
fi

# Mark bot as idle in fleet-state — non-fatal if helper is missing or fails
[ -x "$(dirname "$0")/fleet-state-update.sh" ] && "$(dirname "$0")/fleet-state-update.sh" "$BOT_NAME" "idle" || true

echo "$BOT_LABEL started"
