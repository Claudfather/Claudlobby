#!/bin/bash
# install_fleet_timer_launchd.sh — Shared launchd enrollment for fleet timers.
#
# launchd sibling of install_fleet_timer.sh: copies the composer-generated
# <prefix>.<name>.plist from runtime/fleet/timers/ into ~/Library/LaunchAgents/
# and (re)loads it. One enroll implementation for every fleet timer — the
# composer already emits the plists, so nothing is regenerated here.
#
# Run `claudlobby generate` first to produce the units.
#
# Usage: install_fleet_timer_launchd.sh <timer-name> [<fleet-name>]
#
# Env overrides (the setup backbone uses these; defaults preserve the
# fleet-timer behavior above):
#   TIMER_DIR       — source dir of composed units
#                     (default: local/<fleet>/runtime/fleet/timers)
#   UNIT_NAME       — full plist basename / launchd Label
#                     (default: <service_prefix>.<timer-name>)
#   SERVICE_PREFIX  — prefix for the default UNIT_NAME
#                     (default: derived from the fleet's first bot.conf)
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

TIMER="${1:?Usage: install_fleet_timer_launchd.sh <timer-name> [<fleet-name>]}"
shift

if [ "$_OS" != "Darwin" ]; then
    echo "install_fleet_timer_launchd.sh: macOS only (launchd). On Linux, use install_fleet_timer.sh." >&2
    exit 1
fi
FLEET_DIR=""
if [ -z "${TIMER_DIR:-}" ]; then
    FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
    if [ -z "$FLEET" ]; then
        echo "install_fleet_timer_launchd.sh: pass a fleet name, set CLAUDLOBBY_FLEET, or set TIMER_DIR" >&2
        exit 2
    fi
    FLEET_DIR="$CLAUDLOBBY_ROOT/local/$FLEET"
    TIMER_DIR="$FLEET_DIR/runtime/fleet/timers"
fi
if [[ ! -d "$TIMER_DIR" ]]; then
    echo "Error: $TIMER_DIR not found — run 'claudlobby generate' first." >&2
    exit 1
fi

if [ -z "${UNIT_NAME:-}" ]; then
    # Derive service prefix from bot.conf (all bots share the same
    # SERVICE_PREFIX). setup-fleet passes SERVICE_PREFIX from fleet.yaml
    # instead, so a cold start (no bot.conf composed yet) still enrolls.
    if [ -z "${SERVICE_PREFIX:-}" ] && [ -n "$FLEET_DIR" ]; then
        _first_conf="$(find "$FLEET_DIR/runtime/bots" -name bot.conf -print -quit 2>/dev/null)"
        if [ -n "$_first_conf" ]; then
            SERVICE_PREFIX="$(extract_bot_conf_var "$_first_conf" SERVICE_PREFIX)"
        fi
    fi
    if [ -z "${SERVICE_PREFIX:-}" ]; then
        echo "install_fleet_timer_launchd.sh: SERVICE_PREFIX not set and no bot.conf found." >&2
        exit 2
    fi
    LABEL="$SERVICE_PREFIX.$TIMER"
else
    LABEL="$UNIT_NAME"
fi

SRC_PLIST="$TIMER_DIR/$LABEL.plist"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# An opt-in timer (e.g. code-audit-sweep) only has units when its fleet.yaml
# block is enabled — give a clear pointer rather than a bare cp failure.
if [ ! -f "$SRC_PLIST" ]; then
    echo "Error: $SRC_PLIST not found — is the '$TIMER' timer enabled in fleet.yaml? Run 'claudlobby generate'." >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cp "$SRC_PLIST" "$PLIST"

UID_NUM="$(id -u)"
/bin/launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
/bin/launchctl bootstrap "gui/$UID_NUM" "$PLIST"
echo "installed + loaded: $LABEL"
echo "plist:  $PLIST (source: $SRC_PLIST)"
echo "status: launchctl print gui/$UID_NUM/$LABEL | grep state"
