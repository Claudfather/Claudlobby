#!/bin/bash
# Install a composed bot as a systemd user service (Linux).
#
# claudlobby's compositor writes <prefix>.<bot>.service into the bot's runtime
# directory. This script:
#   1. Reads BOT_SERVICE from bot.conf to identify the correct unit
#   2. Cleans up any stale unit files (old naming conventions) from both the
#      bot dir and ~/.config/systemd/user/
#   3. Copies the correct unit into ~/.config/systemd/user/
#   4. Runs daemon-reload, enables + starts
#
# Idempotent: safe to run repeatedly — stale cleanup only touches units that
# don't match the current BOT_SERVICE name.
#
# Usage: install-bot-systemd.sh /path/to/runtime/bots/<bot>
#
# Recommended: run `loginctl enable-linger $USER` once on the host so user
# services keep running when no one is logged in (otherwise services stop
# at the end of every login session).
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT_DIR="${1:?Usage: install-bot-systemd.sh /path/to/bot/dir}"
BOT_DIR="$(cd "$BOT_DIR" && pwd)"
install_error_trap "$BOT_DIR"

if [ "$_OS" != "Linux" ]; then
    echo "install-bot-systemd.sh: Linux only (saw uname=$_OS)" >&2
    echo "  on macOS, use install-bot.sh (launchd LaunchAgent)" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$BOT_DIR/bot.conf"

# BOT_SERVICE is the source of truth (set by compositor in bot.conf).
if [ -z "${BOT_SERVICE:-}" ]; then
    echo "install-bot-systemd.sh: BOT_SERVICE not set in $BOT_DIR/bot.conf" >&2
    exit 1
fi

UNIT_NAME="$BOT_SERVICE.service"
UNIT_SRC="$BOT_DIR/$UNIT_NAME"

if [ ! -f "$UNIT_SRC" ]; then
    echo "install-bot-systemd.sh: expected $UNIT_SRC not found — has 'claudlobby generate' run?" >&2
    exit 1
fi

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR" "$BOT_DIR/logs"

# --- Cleanup stale units ---------------------------------------------------
# After a service_prefix rename (e.g. ari.service → com.example.eng.ari.service),
# old units linger in the bot dir and in systemd. Stop, disable, and remove
# any .service files that don't match the current BOT_SERVICE name.

bot_id="$(basename "$BOT_DIR")"

for old_unit in "$BOT_DIR"/*.service; do
    [ -f "$old_unit" ] || continue
    old_name="$(basename "$old_unit")"
    [ "$old_name" = "$UNIT_NAME" ] && continue
    echo "install-bot-systemd.sh: removing stale unit from bot dir: $old_name"
    rm -f "$old_unit"
done

for old_installed in "$UNIT_DIR"/*.service; do
    [ -f "$old_installed" ] || continue
    old_name="$(basename "$old_installed")"
    [ "$old_name" = "$UNIT_NAME" ] && continue
    # Only clean units that belong to THIS bot (contain the bot_id).
    case "$old_name" in
        "$bot_id.service"|*".$bot_id.service")
            echo "install-bot-systemd.sh: disabling stale installed unit: $old_name"
            systemctl --user disable --now "$old_name" 2>/dev/null || true
            rm -f "$old_installed"
            ;;
    esac
done

# --- Install current unit ---------------------------------------------------
cp "$UNIT_SRC" "$UNIT_DIR/$UNIT_NAME"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

echo "installed + started: $UNIT_NAME"
echo "unit:   $UNIT_DIR/$UNIT_NAME"
echo "status: systemctl --user status $UNIT_NAME"
echo "logs:   journalctl --user -u $UNIT_NAME -f"
echo
echo "Heads-up: if you haven't already, run \`loginctl enable-linger $USER\`"
echo "on this host so user services persist past your login session."
