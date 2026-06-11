#!/usr/bin/env bash
# update-claude-code.sh — Update Claude Code to latest, bounce fleet if changed.
#
# Intended to run daily via systemd timer. Idempotent: if already on latest,
# does nothing. On update: logs the version bump and restarts all bots via
# spin-up-bot.sh (which is itself idempotent).
#
# Usage: update-claude-code.sh [<fleet-name>]
#   If fleet-name omitted, updates Claude Code but doesn't bounce any fleet.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
LOG_DIR="${CLAUDLOBBY_ROOT}/state"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/claude-update.log"

ts=$(ts_iso)

# --- Capture current version ---
old_version=""
if command -v claude >/dev/null 2>&1; then
    old_version=$(claude --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")
fi

echo "$ts UPDATE starting (current: ${old_version:-not installed})" >> "$LOG"

# --- Detect install path and run update directly (no eval) ---
_claude_path=$(command -v claude 2>/dev/null || true)
_use_sudo=0
if [ -n "$_claude_path" ] && [[ "$_claude_path" == /usr/* ]]; then
    _use_sudo=1
fi

if [ "$_use_sudo" -eq 1 ]; then
    echo "$ts UPDATE running: sudo npm install -g @anthropic-ai/claude-code@latest" >> "$LOG"
    if sudo npm install -g @anthropic-ai/claude-code@latest >> "$LOG" 2>&1; then
        new_version=$(claude --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")
        echo "$ts UPDATE success: $old_version → $new_version" >> "$LOG"
    else
        echo "$ts UPDATE FAILED — npm returned non-zero" >> "$LOG"
        exit 1
    fi
else
    echo "$ts UPDATE running: npm install -g @anthropic-ai/claude-code@latest" >> "$LOG"
    if npm install -g @anthropic-ai/claude-code@latest >> "$LOG" 2>&1; then
        new_version=$(claude --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")
        echo "$ts UPDATE success: $old_version → $new_version" >> "$LOG"
    else
        echo "$ts UPDATE FAILED — npm returned non-zero" >> "$LOG"
        exit 1
    fi
fi

# --- Check if version actually changed ---
if [ "$old_version" = "$new_version" ]; then
    echo "$ts UPDATE no-op: already on $new_version" >> "$LOG"
    exit 0
fi

echo "$ts UPDATE version changed: $old_version → $new_version" >> "$LOG"

# --- Bounce fleet if specified ---
if [ -z "$FLEET" ]; then
    echo "$ts UPDATE done (no fleet specified, skipping bounce)" >> "$LOG"
    exit 0
fi

BOTS_DIR=$(resolve_bots_dir "$FLEET")
if [ ! -d "$BOTS_DIR" ]; then
    echo "$ts UPDATE warning: bots dir not found: $BOTS_DIR" >> "$LOG"
    exit 0
fi

echo "$ts BOUNCE starting fleet: $FLEET" >> "$LOG"
for bot_dir in "$BOTS_DIR"/*/; do
    [ -d "$bot_dir" ] || continue
    bot_id=$(basename "$bot_dir")
    echo "$ts BOUNCE restarting: $bot_id" >> "$LOG"
    "$LIB_DIR/spin-up-bot.sh" "$bot_dir" >> "$LOG" 2>&1 || {
        echo "$ts BOUNCE FAILED: $bot_id" >> "$LOG"
    }
done
echo "$ts BOUNCE complete" >> "$LOG"
