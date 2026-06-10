#!/bin/bash
# Install a composed bot as a launchd LaunchAgent (macOS).
#
# claudlobby's compositor already wrote <prefix>.<bot>.plist into the bot's runtime
# directory. This script copies that plist into ~/Library/LaunchAgents/ and
# bootstraps it with launchctl.
#
# Usage: install-bot.sh /path/to/runtime/bots/<bot>
#
# Linux equivalent: see install-bot-systemd.sh (TODO) — the .service file
# also lives in the bot's runtime dir.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT_DIR="${1:?Usage: install-bot.sh /path/to/bot/dir}"
BOT_DIR="$(cd "$BOT_DIR" && pwd)"

if [ "$_OS" != "Darwin" ]; then
    echo "install-bot.sh: macOS only (saw uname=$_OS)" >&2
    echo "  on Linux, copy <bot>.service to ~/.config/systemd/user/ and run:" >&2
    echo "  systemctl --user daemon-reload && systemctl --user enable --now <bot>" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$BOT_DIR/bot.conf"

PLIST_SRC=$(find "$BOT_DIR" -maxdepth 1 -name '*.plist' | head -1)
if [ -z "$PLIST_SRC" ]; then
    echo "install-bot.sh: no .plist in $BOT_DIR — has 'claudlobby generate' run?" >&2
    exit 1
fi

# Extract Label from the plist (set by the composer's service_prefix + bot name).
LABEL=$(/usr/bin/plutil -extract Label raw -o - "$PLIST_SRC")
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$BOT_DIR/logs"
cp "$PLIST_SRC" "$PLIST_DST"

UID_NUM="$(id -u)"
/bin/launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
/bin/launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"

echo "installed + loaded: $LABEL"
echo "plist:  $PLIST_DST"
echo "logs:   tail -f $BOT_DIR/logs/launchd.*.log"
