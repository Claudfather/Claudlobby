#!/bin/bash
# Install fleet-pulse.sh as a systemd user timer (Linux).
#
# Enrolls a oneshot service + timer that runs fleet-pulse.sh on the
# configured interval (OBSERVABILITY_PULSE_INTERVAL from bot.conf,
# default 300s / 5 min).
#
# Usage: install-fleet-pulse-systemd.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

if [ "$_OS" != "Linux" ]; then
    echo "install-fleet-pulse-systemd.sh: Linux only." >&2
    exit 1
fi
FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
if [ -z "$FLEET" ]; then
    echo "install-fleet-pulse-systemd.sh: pass a fleet name or set CLAUDLOBBY_FLEET" >&2
    exit 2
fi

# Derive service prefix from bot.conf (all bots share the same SERVICE_PREFIX).
if [ -z "${SERVICE_PREFIX:-}" ]; then
    _first_conf="$(find "$CLAUDLOBBY_ROOT/local/$FLEET/runtime/bots" -name bot.conf -print -quit 2>/dev/null)"
    if [ -n "$_first_conf" ]; then
        SERVICE_PREFIX="$(extract_bot_conf_var "$_first_conf" SERVICE_PREFIX)"
    fi
fi
if [ -z "${SERVICE_PREFIX:-}" ]; then
    echo "install-fleet-pulse-systemd.sh: SERVICE_PREFIX not set and no bot.conf found." >&2
    echo "  Run 'claudlobby generate' first, or export SERVICE_PREFIX." >&2
    exit 2
fi

# Read pulse interval from first bot.conf (default 300s = 5 min)
PULSE_INTERVAL=300
if [ -n "${_first_conf:-}" ]; then
    _interval="$(extract_bot_conf_var "$_first_conf" OBSERVABILITY_PULSE_INTERVAL)"
    [ -n "$_interval" ] && PULSE_INTERVAL="$_interval"
fi

NAME="$SERVICE_PREFIX.fleet-pulse"
SERVICE_FILE="$HOME/.config/systemd/user/$NAME.service"
TIMER_FILE="$HOME/.config/systemd/user/$NAME.timer"
PROGRAM="$CLAUDLOBBY_ROOT/lib/fleet-pulse.sh"

if [ ! -x "$PROGRAM" ]; then
    echo "error: $PROGRAM not executable (run: chmod +x $PROGRAM)" >&2
    exit 1
fi

mkdir -p "$HOME/.config/systemd/user"

cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=claudlobby fleet-pulse ($FLEET)

[Service]
Type=oneshot
Environment=CLAUDLOBBY_ROOT=$CLAUDLOBBY_ROOT
ExecStart=$PROGRAM $FLEET
UNIT

cat > "$TIMER_FILE" <<UNIT
[Unit]
Description=claudlobby fleet-pulse timer ($FLEET) — tick every ${PULSE_INTERVAL}s

[Timer]
OnBootSec=$PULSE_INTERVAL
OnUnitActiveSec=$PULSE_INTERVAL
AccuracySec=10

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now "$NAME.timer"

echo "installed + started: $NAME.timer"
echo "service: $SERVICE_FILE"
echo "timer:   $TIMER_FILE"
echo "interval: ${PULSE_INTERVAL}s"
echo "status:  systemctl --user list-timers | grep $NAME"
echo "logs:    journalctl --user -u $NAME.service -f"
