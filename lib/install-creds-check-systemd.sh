#!/bin/bash
# Install the credential keepalive as a systemd user timer (Linux).
#
# Companion to install-keepalive-systemd.sh. Where install-keepalive ticks
# every 60s, this timer fires once a day at 09:00 local to ping
# fleet-critical credentials and alert Telegram on state transitions.
# See lib/creds-check.sh for the probe implementation.
#
# Usage: install-creds-check-systemd.sh
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

if [ "$_OS" != "Linux" ]; then
    echo "install-creds-check-systemd.sh: Linux only. On macOS, use install-creds-check.sh" >&2
    exit 1
fi
NAME="claudlobby-creds-check"
SERVICE_FILE="$HOME/.config/systemd/user/$NAME.service"
TIMER_FILE="$HOME/.config/systemd/user/$NAME.timer"
PROGRAM="$CLAUDLOBBY_ROOT/lib/creds-check.sh"

if [ ! -x "$PROGRAM" ]; then
    echo "error: $PROGRAM not executable (run: chmod +x $PROGRAM)" >&2
    exit 1
fi

mkdir -p "$HOME/.config/systemd/user"

cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=claudlobby credential keepalive

[Service]
Type=oneshot
Environment=CLAUDLOBBY_ROOT=$CLAUDLOBBY_ROOT
ExecStart=$PROGRAM
UNIT

cat > "$TIMER_FILE" <<UNIT
[Unit]
Description=claudlobby credential keepalive timer — daily at 09:00 local

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now "$NAME.timer"

echo "installed + started: $NAME.timer"
echo "next fire: $(systemctl --user list-timers --no-pager | grep $NAME || echo 'check: systemctl --user list-timers')"
echo "logs:      journalctl --user -u $NAME.service -f"
echo "manual:    $PROGRAM"
