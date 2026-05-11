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

AGENTS_DIR="$HOME/Library/LaunchAgents"

for conf in "$BOTS_DIR"/*/bot.conf; do
    [ -f "$conf" ] || continue
    bot_dir="$(dirname "$conf")"
    bot_name="$(basename "$bot_dir")"

    # Only touch bots whose service is registered with the host's init.
    if [ "$_OS" = "Darwin" ]; then
        plist=$(find "$AGENTS_DIR" -maxdepth 1 -name "*.$bot_name.plist" 2>/dev/null | head -1) || true
        [ -n "$plist" ] || continue
    else
        # Linux: rely on the user's session bus; assume registered if .service exists.
        [ -f "$HOME/.config/systemd/user/$bot_name.service" ] || continue
    fi

    "$KEEPALIVE" "$bot_dir" || echo "$TS WARN — keepalive.sh failed for $bot_name (exit $?)" >>"$LOG"
done
