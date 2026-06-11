#!/bin/bash
# Install a composed bot as a launchd LaunchAgent (macOS).
#
# claudlobby's compositor writes <prefix>.<bot>.plist into the bot's runtime
# directory. This script:
#   1. Reads BOT_SERVICE from bot.conf to identify the correct plist
#   2. Cleans up any stale plist files (old naming conventions) from both the
#      bot dir and ~/Library/LaunchAgents/
#   3. Copies the correct plist into ~/Library/LaunchAgents/
#   4. Bootstraps it with launchctl
#
# Idempotent: safe to run repeatedly — stale cleanup only touches plists that
# don't match the current BOT_SERVICE name.
#
# Usage: install-bot.sh /path/to/runtime/bots/<bot>
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT_DIR="${1:?Usage: install-bot.sh /path/to/bot/dir}"
BOT_DIR="$(cd "$BOT_DIR" && pwd)"

if [ "$_OS" != "Darwin" ]; then
    echo "install-bot.sh: macOS only (saw uname=$_OS)" >&2
    echo "  on Linux, use install-bot-systemd.sh" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$BOT_DIR/bot.conf"

# BOT_SERVICE is the source of truth (set by compositor in bot.conf).
if [ -z "${BOT_SERVICE:-}" ]; then
    echo "install-bot.sh: BOT_SERVICE not set in $BOT_DIR/bot.conf" >&2
    exit 1
fi

PLIST_NAME="$BOT_SERVICE.plist"
PLIST_SRC="$BOT_DIR/$PLIST_NAME"

if [ ! -f "$PLIST_SRC" ]; then
    echo "install-bot.sh: expected $PLIST_SRC not found — has 'claudlobby generate' run?" >&2
    exit 1
fi

AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS_DIR" "$BOT_DIR/logs"

UID_NUM="$(id -u)"
bot_id="$(basename "$BOT_DIR")"

# --- Cleanup stale plists ---------------------------------------------------
for old_plist in "$BOT_DIR"/*.plist; do
    [ -f "$old_plist" ] || continue
    old_name="$(basename "$old_plist")"
    [ "$old_name" = "$PLIST_NAME" ] && continue
    echo "install-bot.sh: removing stale plist from bot dir: $old_name"
    rm -f "$old_plist"
done

for old_installed in "$AGENTS_DIR"/*.plist; do
    [ -f "$old_installed" ] || continue
    old_name="$(basename "$old_installed" .plist)"
    [ "$old_name" = "$BOT_SERVICE" ] && continue
    # Only clean plists that belong to THIS bot.
    case "$old_name" in
        "$bot_id"|*".$bot_id")
            echo "install-bot.sh: unloading stale LaunchAgent: $old_name"
            /bin/launchctl bootout "gui/$UID_NUM/$old_name" 2>/dev/null || true
            rm -f "$old_installed"
            ;;
    esac
done

# --- Install current plist --------------------------------------------------
PLIST_DST="$AGENTS_DIR/$PLIST_NAME"
cp "$PLIST_SRC" "$PLIST_DST"

/bin/launchctl bootout "gui/$UID_NUM/$BOT_SERVICE" 2>/dev/null || true
/bin/launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"

echo "installed + loaded: $BOT_SERVICE"
echo "plist:  $PLIST_DST"
echo "logs:   tail -f $BOT_DIR/logs/launchd.*.log"
