#!/bin/bash
# install-auto-deploy.sh — Enroll the auto-deploy timer as a launchd LaunchAgent
# (macOS).
#
# Copies the composer-generated plist (single source of truth) into
# ~/Library/LaunchAgents/ and (re)loads it. Run `claudlobby generate` (with an
# `auto_deploy:` block in fleet.yaml) first.
#
# Usage: install-auto-deploy.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

if [ "$_OS" != "Darwin" ]; then
    echo "install-auto-deploy.sh: macOS only. On Linux, use install-auto-deploy-systemd.sh" >&2
    exit 1
fi
FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
if [ -z "$FLEET" ]; then
    echo "install-auto-deploy.sh: pass a fleet name or set CLAUDLOBBY_FLEET" >&2
    exit 2
fi

FLEET_DIR="$CLAUDLOBBY_ROOT/local/$FLEET"
TIMER_DIR="$FLEET_DIR/runtime/fleet/timers"

# Derive service prefix from bot.conf (all bots share the same SERVICE_PREFIX).
if [ -z "${SERVICE_PREFIX:-}" ]; then
    _first_conf="$(find "$FLEET_DIR/runtime/bots" -name bot.conf -print -quit 2>/dev/null)"
    if [ -n "$_first_conf" ]; then
        SERVICE_PREFIX="$(extract_bot_conf_var "$_first_conf" SERVICE_PREFIX)"
    fi
fi
if [ -z "${SERVICE_PREFIX:-}" ]; then
    echo "install-auto-deploy.sh: SERVICE_PREFIX not set and no bot.conf found. Run 'claudlobby generate' first." >&2
    exit 2
fi

LABEL="$SERVICE_PREFIX.auto-deploy"
SRC_PLIST="$TIMER_DIR/$LABEL.plist"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -f "$SRC_PLIST" ]; then
    echo "Error: $SRC_PLIST not found — enable the auto_deploy: block in fleet.yaml and run 'claudlobby generate'." >&2
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
