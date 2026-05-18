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
FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
if [ -z "$FLEET" ]; then
    echo "install-creds-check-systemd.sh: pass a fleet name or set CLAUDLOBBY_FLEET" >&2
    exit 2
fi

# Derive service prefix from bot.conf (all bots share the same SERVICE_PREFIX).
if [ -z "${SERVICE_PREFIX:-}" ]; then
    _first_conf="$(find "$CLAUDLOBBY_ROOT/local/$FLEET/runtime/bots" -name bot.conf -print -quit 2>/dev/null)"
    if [ -n "$_first_conf" ]; then
        SERVICE_PREFIX="$(grep -m1 '^export SERVICE_PREFIX=' "$_first_conf" | cut -d= -f2- | tr -d "'")"
    fi
fi
if [ -z "${SERVICE_PREFIX:-}" ]; then
    echo "install-creds-check-systemd.sh: SERVICE_PREFIX not set and no bot.conf found." >&2
    echo "  Run 'claudlobby generate' first, or export SERVICE_PREFIX." >&2
    exit 2
fi

NAME="$SERVICE_PREFIX.creds-check"
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
