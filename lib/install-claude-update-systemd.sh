#!/bin/bash
# Install daily Claude Code update as a systemd user timer (Linux).
#
# Creates a oneshot service + timer that runs update-claude-code.sh daily
# at 04:00 local time with 10-minute randomized jitter.
#
# Usage: install-claude-update-systemd.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

if [ "$_OS" != "Linux" ]; then
    echo "install-claude-update-systemd.sh: Linux only." >&2
    exit 1
fi

FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
NAME="claudlobby-claude-update"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

# --- Service unit ---
cat > "$UNIT_DIR/$NAME.service" << EOF
[Unit]
Description=Claude Code daily update${FLEET:+ (fleet: $FLEET)}

[Service]
Type=oneshot
ExecStart=$LIB_DIR/update-claude-code.sh${FLEET:+ $FLEET}
Environment=HOME=$HOME
Environment=PATH=/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/.npm-global/bin
Environment=CLAUDLOBBY_ROOT=$CLAUDLOBBY_ROOT
EOF

# --- Timer unit (daily at 4 AM local with 10min jitter) ---
cat > "$UNIT_DIR/$NAME.timer" << EOF
[Unit]
Description=Daily Claude Code update timer

[Timer]
OnCalendar=*-*-* 04:00:00
Persistent=true
RandomizedDelaySec=600

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$NAME.timer"

echo "installed + started: $NAME.timer"
echo "  runs daily at 04:00 (±10min jitter)"
echo "  status: systemctl --user list-timers | grep $NAME"
echo "  logs:   journalctl --user -u $NAME.service -f"
echo "  manual: systemctl --user start $NAME.service"
