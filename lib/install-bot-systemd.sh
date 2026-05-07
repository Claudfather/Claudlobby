#!/bin/bash
# Install a composed bot as a systemd user service (Linux).
#
# claudlobby's compositor already wrote <bot>.service into the bot's runtime
# directory. This script copies that unit file into ~/.config/systemd/user/,
# runs `systemctl --user daemon-reload`, then enables + starts it.
#
# Usage: install-bot-systemd.sh /path/to/runtime/bots/<bot>
#
# Recommended: run `loginctl enable-linger $USER` once on the host so user
# services keep running when no one is logged in (otherwise services stop
# at the end of every login session).
set -euo pipefail

BOT_DIR="${1:?Usage: install-bot-systemd.sh /path/to/bot/dir}"
BOT_DIR="$(cd "$BOT_DIR" && pwd)"

if [ "$(uname)" != "Linux" ]; then
    echo "install-bot-systemd.sh: Linux only (saw uname=$(uname))" >&2
    echo "  on macOS, use install-bot.sh (launchd LaunchAgent)" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$BOT_DIR/bot.conf"

UNIT_SRC=$(find "$BOT_DIR" -maxdepth 1 -name '*.service' | head -1)
if [ -z "$UNIT_SRC" ]; then
    echo "install-bot-systemd.sh: no .service in $BOT_DIR — has 'claudlobby generate' run?" >&2
    exit 1
fi

UNIT_NAME="$(basename "$UNIT_SRC")"
UNIT_DST="$HOME/.config/systemd/user/$UNIT_NAME"

mkdir -p "$HOME/.config/systemd/user" "$BOT_DIR/logs"
cp "$UNIT_SRC" "$UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

echo "installed + started: $UNIT_NAME"
echo "unit:   $UNIT_DST"
echo "status: systemctl --user status $UNIT_NAME"
echo "logs:   journalctl --user -u $UNIT_NAME -f"
echo
echo "Heads-up: if you haven't already, run \`loginctl enable-linger $USER\`"
echo "on this host so user services persist past your login session."
