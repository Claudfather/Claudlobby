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
# Usage: spin-down-bot.sh [--purge] [--reason <text>] [--expected-return <iso8601|none>] <bot-dir>
#   --purge            also rm -rf the bot directory after supervision is reaped.
#   --reason           why this teardown is happening (free text).
#   --expected-return  ISO8601 timestamp the bot is expected back, or "none" to
#                      assert a genuine decommission. Absent records as
#                      "unspecified" — which is NOT the same claim as "none".
#   $SPINDOWN_ACTOR    overrides the recorded actor, so a bot-driven teardown
#                      names itself instead of masquerading as the host user.
#
# NOT the tool for transient RAM pressure — see $_sd_warning below / --help.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

_sd_usage='Usage: spin-down-bot.sh [--purge] [--reason <text>] [--expected-return <iso8601|none>] <bot-dir>'
_sd_warning='NOT the tool for transient RAM pressure. To free a bots memory and get it back:
  systemctl --user stop <BOT_SERVICE>      (launchctl bootout ... on macOS)
That frees the identical RAM, leaves the bot ENROLLED, and lets keepalive walk it
back up. This script REMOVES supervision: nothing will revive the bot, and it
reports as unsupervised-down until spun back up. Reserve it for a genuine
decommission or a throwaway canary.'
PURGE=0
BOT_DIR=""
REASON="unspecified"
EXPECTED_RETURN="unspecified"
while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1 ;;
        --reason) REASON="${2:?--reason requires a value}"; shift ;;
        --expected-return) EXPECTED_RETURN="${2:?--expected-return requires a value}"; shift ;;
        # The warning prints on --help, not just in the source: an operator
        # reaching for this under memory pressure never opens the file.
        -h | --help) printf '%s\n\n%s\n' "$_sd_usage" "$_sd_warning"; exit 0 ;;
        -*) printf 'spin-down-bot: unknown option: %s\n' "$1" >&2; exit 2 ;;
        *) BOT_DIR="$1" ;;
    esac
    shift
done
[ -n "$BOT_DIR" ] || { printf '%s\n' "$_sd_usage" >&2; exit 2; }

# Canonicalize while the dir exists; keep the literal path otherwise so a repeat
# run (e.g. after a prior --purge) still logs coherently.
[ -d "$BOT_DIR" ] && BOT_DIR="$(cd "$BOT_DIR" && pwd)"
SLUG="$(basename "$BOT_DIR")"

sd_log() { printf 'spin-down[%s]: %s\n' "$SLUG" "$*"; }

# --- The receipt -------------------------------------------------------------
# sd_log is stdout-only, so it dies with the invoking terminal: the ledger row is
# the only durable record of WHO tore a bot down and WHY.
#
# It goes to the FLEET ledger, not the bot's own data/events, because --purge
# deletes the bot dir — a record written there would die with the very
# transaction it documents. An explicitly empty bot_dir arg forces
# emit_fleet_event's fleet-level branch while keeping bot identity explicit.
#
# fleet and bot_dir are recorded because they are unrecoverable after --purge,
# and they are what a reader needs to bring the bot back. That ledger is
# host-global, so the fleet is also what distinguishes same-named bots.
emit_teardown_receipt() {
    local action="spin-down" actor fleet data
    [ "$PURGE" -eq 1 ] && action="spin-down --purge"
    actor="${SPINDOWN_ACTOR:-${USER:-unknown}@$(hostname)}"
    fleet="${FLEET_NAME:-unknown}"
    data=$(printf '{"action":"%s","actor":"%s","fleet":"%s","bot_dir":"%s","expected_return":"%s","reason":"%s"}' \
        "$(json_escape "$action")" "$(json_escape "$actor")" \
        "$(json_escape "$fleet")" "$(json_escape "$BOT_DIR")" \
        "$(json_escape "$EXPECTED_RETURN")" "$(json_escape "$REASON")")
    # Named for what it is: emitted BEFORE the legs, so it records an intent the
    # script has not yet carried out, never an observed outcome.
    emit_fleet_event bot_teardown_started spin-down "$data" "" "$SLUG"
    sd_log "receipt: actor=$actor action=$action expected_return=$EXPECTED_RETURN reason=$REASON"
}

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

# Receipt first: a crash mid-teardown then leaves a record of an unfinished
# teardown, never a finished teardown with no record.
emit_teardown_receipt
reap_supervision
reap_tmux
reap_fleet_state

if [ "$PURGE" -eq 1 ]; then
    rm -rf "$BOT_DIR"
    sd_log "purged bot directory"
fi

sd_log "reaped"
