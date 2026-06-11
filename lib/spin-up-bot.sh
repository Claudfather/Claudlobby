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
    # BOT_SERVICE is the canonical unit name (set by compositor in bot.conf).
    # Fall back to BOT_NAME for fleets that haven't regenerated yet.
    UNIT_DIR="$HOME/.config/systemd/user"
    if [ -n "${BOT_SERVICE:-}" ] && [ -f "$UNIT_DIR/$BOT_SERVICE.service" ]; then
        echo "spin-up-bot: $BOT_SERVICE.service exists — restarting"
        systemctl --user restart "$BOT_SERVICE.service"
    elif [ -f "$UNIT_DIR/$BOT_NAME.service" ]; then
        # Pre-rename unit still installed. Restart it if the new unit file
        # doesn't exist yet (fleet not regenerated); otherwise re-enroll
        # to migrate to the new name.
        if [ -n "${BOT_SERVICE:-}" ] && [ -f "$BOT_DIR/$BOT_SERVICE.service" ]; then
            echo "spin-up-bot: migrating $BOT_NAME → $BOT_SERVICE"
            "$LIB_DIR/install-bot-systemd.sh" "$BOT_DIR"
        else
            echo "spin-up-bot: $BOT_NAME.service exists (pre-rename) — restarting"
            systemctl --user restart "$BOT_NAME.service"
        fi
    else
        # install-bot-systemd.sh handles stale unit cleanup + fresh install.
        echo "spin-up-bot: enrolling $BOT_NAME as systemd-user service"
        "$LIB_DIR/install-bot-systemd.sh" "$BOT_DIR"
    fi
    ;;
Darwin)
    if [ -n "${BOT_SERVICE:-}" ]; then
        PLIST_FILE="$HOME/Library/LaunchAgents/$BOT_SERVICE.plist"
    else
        PLIST_FILE=""
    fi
    if [ -n "$PLIST_FILE" ] && [ -f "$PLIST_FILE" ]; then
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
