#!/bin/bash
# Bring a bot up under proper host supervision.
#
# This is the canonical entry point for "spin up a bot" — used by managers
# dispatching workers, by reconcile-fleet.sh, and by humans manually
# enrolling. Picks the right install method by host:
#
#   Linux  → install-bot-systemd.sh (user systemd unit, Restart=on-failure)
#   macOS  → install-bot.sh         (launchd LaunchAgent, KeepAlive)
#   other  → fall back to start-bot.sh in tmux (cron-supervised pattern)
#
# Idempotent: if the unit/plist is already installed, restarts it instead
# of re-installing.
#
# Usage: spin-up-bot.sh /path/to/runtime/bots/<bot>
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT_DIR="${1:?Usage: spin-up-bot.sh /path/to/bot/dir}"
BOT_DIR="$(cd "$BOT_DIR" && pwd)"

load_bot_conf "$BOT_DIR" || exit 1
install_error_trap "$BOT_DIR"

case "$_OS" in
Linux)
    UNIT_FILE="$HOME/.config/systemd/user/$BOT_NAME.service"
    if [ -f "$UNIT_FILE" ]; then
        echo "spin-up-bot: $BOT_NAME.service exists — restarting"
        systemctl --user restart "$BOT_NAME.service"
    else
        echo "spin-up-bot: enrolling $BOT_NAME as systemd-user service"
        "$LIB_DIR/install-bot-systemd.sh" "$BOT_DIR"
    fi
    ;;
Darwin)
    PLIST_FILE="$HOME/Library/LaunchAgents/$BOT_SERVICE.plist"
    if [ -f "$PLIST_FILE" ]; then
        echo "spin-up-bot: $BOT_SERVICE.plist exists — kickstart"
        launchctl kickstart -k "gui/$(id -u)/$BOT_SERVICE"
    else
        echo "spin-up-bot: enrolling $BOT_NAME as launchd LaunchAgent"
        "$LIB_DIR/install-bot.sh" "$BOT_DIR"
    fi
    ;;
*)
    echo "spin-up-bot: unsupported host ($_OS) — falling back to start-bot.sh"
    echo "spin-up-bot: install cron-tmux supervision separately if you want auto-restart"
    "$LIB_DIR/start-bot.sh" "$BOT_DIR"
    ;;
esac
