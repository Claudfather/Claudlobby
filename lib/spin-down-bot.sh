#!/bin/bash
# spin-down-bot.sh — the inverse of spin-up-bot.sh: a guaranteed teardown/reaper
# for canary and throwaway bots. Removes the per-bot supervision (systemd user
# unit on Linux, launchd agent on macOS), kills the private tmux server,
# surgically drops the bot's fleet-state.json key (a locked single-key delete,
# never a prune), and with --purge removes the bot directory.
#
# Cross-platform (via lib-common OS detection), idempotent, and safe to re-run:
# every leg is a no-op when its target is already gone. Designed to be invoked
# under a `trap ... EXIT` by canary flows so a throwaway is reaped even if the
# driving session crashes or dies mid-run.
#
# Usage: spin-down-bot.sh [--purge] <bot-dir>
#   --purge   also rm -rf the bot directory after supervision is reaped.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

PURGE=0
BOT_DIR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1 ;;
        -h | --help) printf 'Usage: spin-down-bot.sh [--purge] <bot-dir>\n'; exit 0 ;;
        -*) printf 'spin-down-bot: unknown option: %s\n' "$1" >&2; exit 2 ;;
        *) BOT_DIR="$1" ;;
    esac
    shift
done
[ -n "$BOT_DIR" ] || { printf 'Usage: spin-down-bot.sh [--purge] <bot-dir>\n' >&2; exit 2; }

# Canonicalize while the dir exists; keep the literal path otherwise so a repeat
# run (e.g. after a prior --purge) still logs coherently.
[ -d "$BOT_DIR" ] && BOT_DIR="$(cd "$BOT_DIR" && pwd)"
SLUG="$(basename "$BOT_DIR")"

sd_log() { printf 'spin-down[%s]: %s\n' "$SLUG" "$*"; }

# Identity comes from bot.conf: BOT_SERVICE (the systemd unit / launchd label /
# tmux socket — one value, all three), FLEET_STATE_PATH, TMUX_SOCKET/TMUX_TMPDIR,
# BOT_NAME. If bot.conf is gone the bot was already reaped (or this is not a bot
# dir) — a clean no-op keeps the reaper idempotent.
if ! load_bot_conf "$BOT_DIR" 2>/dev/null; then
    sd_log "no bot.conf found — already reaped or not a bot dir; nothing to do"
    exit 0
fi
# Emit a script_error event on an unguarded abort (parity with lifecycle peers).
install_error_trap "$BOT_DIR"

# --- Leg 1: supervision (systemd user unit / launchd agent) ------------------
reap_supervision() {
    if [ -z "${BOT_SERVICE:-}" ]; then
        sd_log "BOT_SERVICE unset — no supervised unit to remove"
        return 0
    fi
    case "$_OS" in
        Linux)
            local ud="$HOME/.config/systemd/user"
            # disable --now stops the unit (its ExecStop kills the tmux server)
            # and drops the default.target.wants symlink; guarded for idempotency.
            systemctl --user disable --now "$BOT_SERVICE.service" 2>/dev/null || true
            rm -f "$ud/$BOT_SERVICE.service" "$ud/default.target.wants/$BOT_SERVICE.service"
            systemctl --user daemon-reload 2>/dev/null || true
            systemctl --user reset-failed "$BOT_SERVICE.service" 2>/dev/null || true
            sd_log "systemd user unit $BOT_SERVICE.service stopped + disabled + removed"
            ;;
        Darwin)
            # bootout is the modern inverse of bootstrap (matches install-bot.sh).
            /bin/launchctl bootout "gui/$(id -u)/$BOT_SERVICE" 2>/dev/null || true
            rm -f "$HOME/Library/LaunchAgents/$BOT_SERVICE.plist"
            sd_log "launchd agent $BOT_SERVICE booted out + plist removed"
            ;;
        *)
            sd_log "unsupported OS ($_OS) — skipping supervision leg"
            ;;
    esac
}

# --- Leg 2: per-bot tmux server (belt-and-suspenders vs. the unit ExecStop) ---
reap_tmux() {
    local sock
    sock="$(tmux_socket_for_bot "$BOT_DIR" 2>/dev/null)" || sock=""
    if [ -n "$sock" ]; then
        bot_tmux "$sock" kill-server 2>/dev/null || true
        sd_log "tmux server -L $sock killed"
    else
        sd_log "no resolvable tmux socket — skipping tmux leg"
    fi
    rm -f "$BOT_DIR/.tmux-env" 2>/dev/null || true
}

# --- Leg 3: fleet-state key — delegate the surgical delete to its owner -------
# fleet-state-update.sh is the single writer of fleet-state.json (path, lock, and
# mutation all live there). Pass both the dir-slug and BOT_NAME identity in case
# they differ; the `delete` verb removes only those keys, never a prune of others.
reap_fleet_state() {
    if "$LIB_DIR/fleet-state-update.sh" delete "$SLUG" "${BOT_NAME:-$SLUG}" 2>/dev/null; then
        sd_log "fleet-state key removed (surgical, via fleet-state-update.sh delete)"
    else
        sd_log "fleet-state delete skipped (no state/jq or already gone)"
    fi
}

reap_supervision
reap_tmux
reap_fleet_state

if [ "$PURGE" -eq 1 ]; then
    rm -rf "$BOT_DIR"
    sd_log "purged bot directory"
fi

sd_log "reaped"
