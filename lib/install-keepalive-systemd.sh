#!/bin/bash
# Install the fleet-wide keepalive as a systemd user timer (Linux).
#
# Thin wrapper: copies generated units from runtime/fleet/timers/ and enrolls.
# Run `claudlobby generate` first to produce the units.
#
# Usage: install-keepalive-systemd.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

if [ "$_OS" != "Linux" ]; then
    echo "install-keepalive-systemd.sh: Linux only. On macOS, use install-keepalive.sh" >&2
    exit 1
fi
FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
if [ -z "$FLEET" ]; then
    echo "install-keepalive-systemd.sh: pass a fleet name or set CLAUDLOBBY_FLEET" >&2
    exit 2
fi

FLEET_DIR="$CLAUDLOBBY_ROOT/local/$FLEET"
TIMER_DIR="$FLEET_DIR/runtime/fleet/timers"
if [[ ! -d "$TIMER_DIR" ]]; then
    echo "Error: $TIMER_DIR not found — run 'claudlobby generate' first." >&2
    exit 1
fi

# Derive service prefix from bot.conf (all bots share the same SERVICE_PREFIX).
if [ -z "${SERVICE_PREFIX:-}" ]; then
    _first_conf="$(find "$FLEET_DIR/runtime/bots" -name bot.conf -print -quit 2>/dev/null)"
    if [ -n "$_first_conf" ]; then
        SERVICE_PREFIX="$(extract_bot_conf_var "$_first_conf" SERVICE_PREFIX)"
    fi
fi
if [ -z "${SERVICE_PREFIX:-}" ]; then
    echo "install-keepalive-systemd.sh: SERVICE_PREFIX not set and no bot.conf found." >&2
    exit 2
fi

NAME="$SERVICE_PREFIX.keepalive"

mkdir -p "$HOME/.config/systemd/user"
cp "$TIMER_DIR/$NAME.service" "$HOME/.config/systemd/user/"
cp "$TIMER_DIR/$NAME.timer" "$HOME/.config/systemd/user/"

systemctl --user daemon-reload
systemctl --user enable --now "$NAME.timer"

echo "installed + started: $NAME.timer"
echo "source:  $TIMER_DIR/$NAME.{service,timer}"
echo "status:  systemctl --user list-timers | grep $NAME"
echo "logs:    journalctl --user -u $NAME.service -f"
