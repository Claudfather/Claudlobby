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

# The SCOPED admission cap (#1573 PR B). This door brings up ONE bot, and its
# callers (rolling-restart.sh, weekly-worker-restart.sh, reconcile-fleet.sh, a
# manager, a human) do so one at a time -- so the caller IS the serializer and
# this bring-up must not also pay the host queue's cap. Left at the 1200s
# default, a bot that is merely QUEUED outlasts every driver's own ceiling and
# gets reported as a failure it never had.
#
# Value: the bot's own composed RC_READY_TIMEOUT_S (F4's carrier; 90 is the
# floor for an un-regenerated bot.conf) plus 60s of bring-up slack -- the same
# shape lib/keepalive-all.sh uses. ${VAR:-...} rather than an unconditional
# assignment, so a driver that already derived its own per-bot budget keeps it:
# that number is the one the driver's own ceiling was computed from.
#
# BOUND, stated rather than implied: only the *) branch below execs
# start-bot.sh in this process tree. On Linux and macOS the bring-up is handed
# to the host supervisor, which relaunches start-bot.sh with the UNIT's
# environment and inherits nothing from here. A lib/-internal call convention,
# never composed, never an operator knob, and deliberately NOT
# BOOT_ADMISSION_WAIT_MAX_S (F4 locks the composed value as the winner over the
# env for that key).
_su_rc_s="${RC_READY_TIMEOUT_S:-90}"
case "$_su_rc_s" in ''|*[!0-9]*) _su_rc_s=90 ;; esac
# 10# pins base 10 -- an all-digit value with a leading zero is octal to $(( )),
# and "value too great for base" would abort a bring-up door under set -e.
BOOT_ADMISSION_CALLER_CAP_S="${BOOT_ADMISSION_CALLER_CAP_S:-$((10#$_su_rc_s + 60))}"
export BOOT_ADMISSION_CALLER_CAP_S

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
