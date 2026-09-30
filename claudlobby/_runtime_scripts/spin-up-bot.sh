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

_spin_up_before_restart() {
    case "$1" in
        *" (pre-rename)") echo "spin-up-bot: $BOT_NAME.service exists (pre-rename) — restarting" ;;
        "systemctl "*) echo "spin-up-bot: $BOT_SERVICE.service exists — restarting" ;;
        *) echo "spin-up-bot: $BOT_SERVICE.plist exists — kickstart" ;;
    esac
}

_spin_up_bot() {
    local rc=0
    case "$_OS" in
        Linux)
            # A regenerated canonical file migrates an installed legacy unit.
            # Keep this caller policy separate from the adapter restart ladder.
            local unit_dir="$HOME/.config/systemd/user"
            if [ -n "${BOT_SERVICE:-}" ] && [ ! -f "$unit_dir/$BOT_SERVICE.service" ] &&
                [ -f "$unit_dir/$BOT_NAME.service" ] && [ -f "$BOT_DIR/$BOT_SERVICE.service" ]; then
                echo "spin-up-bot: migrating $BOT_NAME → $BOT_SERVICE"
                svc_enroll "$BOT_DIR"
                return
            fi
            ;;
        Darwin) ;;
        *)
            echo "spin-up-bot: unsupported host ($_OS) — falling back to start-bot.sh"
            echo "spin-up-bot: install cron-tmux supervision separately if you want auto-restart"
            "$LIB_DIR/start-bot.sh" "$BOT_DIR"
            return
            ;;
    esac
    svc_kick "$BOT_DIR" _spin_up_before_restart "${BOT_SERVICE:-}" "$BOT_NAME" || rc=$?
    if [ "$SVC_KICK_SELECTED" -eq 1 ]; then
        # Let an unguarded function return restore the existing ERR trap;
        # selected native rc 2 is failure, never an enrollment fallback.
        return "$rc"
    fi
    if [ "$_OS" = Linux ]; then
        echo "spin-up-bot: enrolling $BOT_NAME as systemd-user service"
    else
        echo "spin-up-bot: enrolling $BOT_NAME as launchd LaunchAgent"
    fi
    svc_enroll "$BOT_DIR"
}

_spin_up_bot
