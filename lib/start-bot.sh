#!/bin/bash
# Shared bot start script — called by each bot's systemd service
# Usage: start-bot.sh /path/to/bot/dir
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT_DIR="${1:?Usage: start-bot.sh /path/to/bot/dir}"
load_bot_conf "$BOT_DIR"

export PATH=/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin
export HOME="$HOME"

# 3-tier env sourcing: global -> fleet -> bot (later tiers override)
source_env_tiered

# Resolve the Telegram token from the env-var name declared in fleet.yaml
if [ -n "${TELEGRAM_TOKEN_ENV_NAME:-}" ]; then
    export TELEGRAM_BOT_TOKEN="${!TELEGRAM_TOKEN_ENV_NAME:-}"
fi

cd "$BOT_DIR"

# Kill any prior session — expected to fail on first boot or after clean shutdown
"$_TMUX_BIN" kill-session -t "$BOT_NAME" 2>/dev/null || true

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

# 3-tier env sourcing inside the tmux session so Claude Code (and its MCP
# servers) inherit all env vars. Later tiers override earlier ones.
# These source commands run inside the tmux shell, NOT the parent start-bot.sh.
[ -f "$HOME/.env" ]                                && printf '. %q\n' "$HOME/.env" >> "$BOT_ENV_FILE"
if [ -n "${CLAUDLOBBY_ROOT:-}" ] && [ -f "$CLAUDLOBBY_ROOT/.env" ]; then
    printf '. %q\n' "$CLAUDLOBBY_ROOT/.env" >> "$BOT_ENV_FILE"
fi
if [ -n "${FLEET_NAME:-}" ] && [ -n "${CLAUDLOBBY_ROOT:-}" ]; then
    local_fleet_env="$CLAUDLOBBY_ROOT/local/$FLEET_NAME/.env"
    [ -f "$local_fleet_env" ] && printf '. %q\n' "$local_fleet_env" >> "$BOT_ENV_FILE"
fi
[ -f "$BOT_DIR/.env" ]                             && printf '. %q\n' "$BOT_DIR/.env" >> "$BOT_ENV_FILE"

# Per-bot identity vars — written explicitly so they override any
# same-named var from upper tiers (prevents tmux server env leakage).
[ -n "${CLAUDE_CONFIG_DIR:-}" ]                    && printf 'export CLAUDE_CONFIG_DIR=%q\n' "$CLAUDE_CONFIG_DIR" >> "$BOT_ENV_FILE"
[ -n "${TELEGRAM_STATE_DIR:-}" ]                   && printf 'export TELEGRAM_STATE_DIR=%q\n' "$TELEGRAM_STATE_DIR" >> "$BOT_ENV_FILE"
[ -n "${TELEGRAM_BOT_TOKEN:-}" ]                   && printf 'export TELEGRAM_BOT_TOKEN=%q\n' "$TELEGRAM_BOT_TOKEN" >> "$BOT_ENV_FILE"
[ -n "${TELEGRAM_BOT_HANDLE:-}" ]                  && printf 'export TELEGRAM_BOT_HANDLE=%q\n' "$TELEGRAM_BOT_HANDLE" >> "$BOT_ENV_FILE"
[ -n "${TELEGRAM_GROUP_CHAT_ID:-}" ]               && printf 'export TELEGRAM_GROUP_CHAT_ID=%q\n' "$TELEGRAM_GROUP_CHAT_ID" >> "$BOT_ENV_FILE"
[ -n "${CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION:-}" ] && printf 'export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=%q\n' "$CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION" >> "$BOT_ENV_FILE"

# Model strategy vars — config-driven model escalation/compaction/subagent preferences
for _ms_var in MODEL_STRATEGY_BASE MODEL_STRATEGY_ESCALATE_TO MODEL_STRATEGY_ESCALATE_WHEN \
               MODEL_STRATEGY_COMPACT_WHEN MODEL_STRATEGY_EXPLORE MODEL_STRATEGY_PLAN MODEL_STRATEGY_GENERAL; do
    [ -n "${!_ms_var:-}" ] && printf 'export %s=%q\n' "$_ms_var" "${!_ms_var}" >> "$BOT_ENV_FILE"
done

# Backwards-compat: if the bot.conf is from before the CLAUDE_FLAGS
# rename, fall back to the legacy hardcoded flag set + CLAUDE_EXTRA_FLAGS.
if [ -z "${CLAUDE_FLAGS:-}" ]; then
    CLAUDE_FLAGS="--channels plugin:telegram@claude-plugins-official --remote-control --dangerously-skip-permissions"
    [ -n "${CLAUDE_EXTRA_FLAGS:-}" ] && CLAUDE_FLAGS="$CLAUDE_FLAGS $CLAUDE_EXTRA_FLAGS"
fi

CLAUDE_CMD=". '$BOT_ENV_FILE' && exec claude $CLAUDE_FLAGS --name \"$SESSION_NAME\""

"$_TMUX_BIN" new-session -d -s "$BOT_NAME" "$CLAUDE_CMD"

# Wait for initialization (up to 90s) with observability
LOG="$BOT_DIR/logs/startup.log"
setup_log_dir "$LOG"
_poll_start=$(date +%s)
echo "$(ts_iso) POLL_START — waiting for remote-control readiness" >> "$LOG"
_ready=0
for _i in $(seq 1 180); do
    if ! check_tmux_session "$BOT_NAME"; then
        echo "$(ts_iso) CRASH — tmux session died during startup (after ${_i}s)" >> "$LOG"
        exit 1
    fi
    if "$_TMUX_BIN" capture-pane -t "$BOT_NAME" -p 2>/dev/null | grep -q "remote-control is active"; then
        _elapsed=$(( $(date +%s) - _poll_start ))
        echo "$(ts_iso) READY — remote-control active after ${_elapsed}s" >> "$LOG"
        _ready=1
        break
    fi
    sleep 0.5
done
if [ "$_ready" -eq 0 ]; then
    echo "$(ts_iso) TIMEOUT — 90s elapsed, remote-control string not found, proceeding anyway" >> "$LOG"
fi

# No sleep here: the remote-control wait loop above already confirms Claude Code
# is fully initialized (MCP servers connected, channel plugin active). A fixed
# sleep after that point is wasted CPU time — especially costly when 8 bots
# start in parallel on a 4-core machine. See docs/audit-cold-start-timing.md.

if [ -n "${STARTUP_PROMPT:-}" ]; then
    "$_TMUX_BIN" send-keys -t "$BOT_NAME" "$STARTUP_PROMPT" Enter
fi

# Mark bot as idle in fleet-state — non-fatal if helper is missing or fails
[ -x "$LIB_DIR/fleet-state-update.sh" ] && "$LIB_DIR/fleet-state-update.sh" "$BOT_NAME" "idle" || true

echo "$BOT_LABEL started"
