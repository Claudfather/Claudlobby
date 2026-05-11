#!/bin/bash
# Install the fleet-wide keepalive as a systemd user timer (Linux).
#
# Companion to install-bot-systemd.sh. The per-bot units have
# Restart=on-failure but won't bring back a tmux session that exited 0,
# and they don't nudge an idle pane. This timer ticks every 60s and runs
# keepalive-all.sh, which kickstarts dead sessions and nudges idle bots.
#
# Usage: install-keepalive-systemd.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

if [ "$_OS" != "Linux" ]; then
    echo "install-keepalive-systemd.sh: Linux only. On macOS, use install-keepalive.sh" >&2
    exit 1
fi
FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
if [ -z "$FLEET" ]; then
    echo "install-keepalive-systemd.sh: pass a fleet name or set CLAUDLOBBY_FLEET" >&2
    exit 2
fi

NAME="claudlobby-$FLEET-keepalive"
SERVICE_FILE="$HOME/.config/systemd/user/$NAME.service"
TIMER_FILE="$HOME/.config/systemd/user/$NAME.timer"
PROGRAM="$CLAUDLOBBY_ROOT/lib/keepalive-all.sh"

if [ ! -x "$PROGRAM" ]; then
    echo "error: $PROGRAM not executable (run: chmod +x $PROGRAM)" >&2
    exit 1
fi

mkdir -p "$HOME/.config/systemd/user"

cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=claudlobby fleet keepalive ($FLEET)

[Service]
Type=oneshot
Environment=CLAUDLOBBY_ROOT=$CLAUDLOBBY_ROOT
Environment=CLAUDLOBBY_FLEET=$FLEET
ExecStart=$PROGRAM
UNIT

cat > "$TIMER_FILE" <<UNIT
[Unit]
Description=claudlobby fleet keepalive timer ($FLEET) — tick every 60s

[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=10

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now "$NAME.timer"

echo "installed + started: $NAME.timer"
echo "service: $SERVICE_FILE"
echo "timer:   $TIMER_FILE"
echo "status:  systemctl --user list-timers | grep $NAME"
echo "logs:    journalctl --user -u $NAME.service -f"
