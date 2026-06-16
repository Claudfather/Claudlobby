#!/bin/bash
# Fleet-wide keepalive — iterates every enrolled bot in a fleet's runtime
# directory and runs keepalive.sh against each.
#
# Designed to be invoked every 60s by a launchd LaunchAgent (macOS) or
# a systemd timer (Linux). See install-keepalive.sh for the macOS plist.
#
# Usage: keepalive-all.sh [<fleet-runtime-bots-dir>]
#   default: $CLAUDLOBBY_ROOT/local/$CLAUDLOBBY_FLEET/runtime/bots
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

if [ -n "${1:-}" ]; then
    BOTS_DIR="$1"
else
    BOTS_DIR=$(resolve_bots_dir)
    if [ -z "${CLAUDLOBBY_FLEET:-}${FLEET_NAME:-}" ]; then
        echo "keepalive-all: pass a runtime/bots dir or set CLAUDLOBBY_FLEET" >&2
        exit 2
    fi
fi

KEEPALIVE="$CLAUDLOBBY_ROOT/lib/keepalive.sh"
LOG="$CLAUDLOBBY_ROOT/lib/logs/keepalive-all.log"

setup_log_dir "$LOG"
TS=$(ts_iso)

if [ ! -x "$KEEPALIVE" ]; then
    echo "$TS FATAL — $KEEPALIVE not executable" >>"$LOG"
    exit 1
fi

if [ ! -d "$BOTS_DIR" ]; then
    echo "$TS FATAL — runtime bots dir not found: $BOTS_DIR" >>"$LOG"
    exit 1
fi

install_error_trap ""

# fleet.yaml is authoritative for which bots this fleet owns. Skip stale/cross-fleet
# residue dirs that are no longer declared — otherwise two fleets' keepalive timers
# both supervise a same-named bot. Derive the fleet.yaml from the fleet name, or
# from the bots-dir parent when called with an explicit dir. Empty result (no/
# unreadable fleet.yaml) → scan every dir, preserving prior behavior.
_kf_fleet="${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}"
if [ -n "$_kf_fleet" ]; then
    _kf_yaml="$CLAUDLOBBY_ROOT/local/$_kf_fleet/fleet.yaml"
else
    _kf_yaml="$(dirname "$(dirname "$BOTS_DIR")")/fleet.yaml"
fi
declared_bots=$(parse_fleet_bots "$_kf_yaml")

AGENTS_DIR="$HOME/Library/LaunchAgents"

for conf in "$BOTS_DIR"/*/bot.conf; do
    [ -f "$conf" ] || continue
    bot_dir="$(dirname "$conf")"
    bot_name="$(basename "$bot_dir")"
    bot_in_fleet "$bot_name" "$declared_bots" || continue   # not in fleet.yaml → not ours to supervise

    # Read BOT_SERVICE from bot.conf (falls back to bot_name for pre-generate fleets).
    svc=$(bot_conf_get "$bot_dir" BOT_SERVICE "$bot_name")

    # Only touch bots whose service is registered with the host's init.
    if [ "$_OS" = "Darwin" ]; then
        if [ ! -f "$AGENTS_DIR/$svc.plist" ]; then
            # Fallback: check for legacy plist pattern (*.$bot_name.plist)
            plist=$(find "$AGENTS_DIR" -maxdepth 1 -name "*.$bot_name.plist" 2>/dev/null | head -1) || true
            [ -n "$plist" ] || continue
        fi
    else
        # Linux: check BOT_SERVICE unit first, fall back to bot_name
        if [ ! -f "$HOME/.config/systemd/user/$svc.service" ] && \
           [ ! -f "$HOME/.config/systemd/user/$bot_name.service" ]; then
            continue
        fi
    fi

    "$KEEPALIVE" "$bot_dir" || echo "$TS WARN — keepalive.sh failed for $bot_name (exit $?)" >>"$LOG"
done
