#!/bin/bash
# install_fleet_timer.sh — Shared systemd user-timer enrollment for fleet timers.
#
# Copies the composer-generated <prefix>.<name>.{service,timer} units from
# runtime/fleet/timers/ into ~/.config/systemd/user/ and enables them. One
# enroll implementation for every fleet timer (fleet-pulse, creds-check,
# code-audit-sweep, ...) — the per-timer installers differ only by <name>.
#
# Run `claudlobby generate` first to produce the units.
#
# Usage: install_fleet_timer.sh <timer-name> [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

TIMER="${1:?Usage: install_fleet_timer.sh <timer-name> [<fleet-name>]}"
shift

if [ "$_OS" != "Linux" ]; then
    echo "install_fleet_timer.sh: Linux only (systemd). On macOS, use the launchd installer." >&2
    exit 1
fi
FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
if [ -z "$FLEET" ]; then
    echo "install_fleet_timer.sh: pass a fleet name or set CLAUDLOBBY_FLEET" >&2
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
    echo "install_fleet_timer.sh: SERVICE_PREFIX not set and no bot.conf found." >&2
    exit 2
fi

NAME="$SERVICE_PREFIX.$TIMER"

# An opt-in timer (e.g. code-audit-sweep) only has units when its fleet.yaml
# block is enabled — give a clear pointer rather than a bare cp failure.
if [[ ! -f "$TIMER_DIR/$NAME.service" ]] || [[ ! -f "$TIMER_DIR/$NAME.timer" ]]; then
    echo "Error: $NAME.{service,timer} not found in $TIMER_DIR — is the '$TIMER' timer enabled in fleet.yaml? Run 'claudlobby generate'." >&2
    exit 1
fi

mkdir -p "$HOME/.config/systemd/user"
cp "$TIMER_DIR/$NAME.service" "$HOME/.config/systemd/user/"
cp "$TIMER_DIR/$NAME.timer" "$HOME/.config/systemd/user/"

systemctl --user daemon-reload
systemctl --user enable --now "$NAME.timer"

echo "installed + started: $NAME.timer"
echo "source:  $TIMER_DIR/$NAME.{service,timer}"
echo "status:  systemctl --user list-timers | grep $NAME"
echo "logs:    journalctl --user -u $NAME.service -f"
