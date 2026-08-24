#!/bin/bash
# Enroll a composed HOST SERVICE unit (systemd): the .service-without-.timer
# shape install_fleet_timer.sh refuses by contract (it requires both files).
# First tenant: claudlobby-plane-daemon. Idempotent; same TIMER_DIR/UNIT_NAME
# override convention as the timer enroller so setup-system drives both alike.
#
# Usage: TIMER_DIR=<dir> UNIT_NAME=<basename> lib/install-host-service-systemd.sh <jobname>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$SCRIPT_DIR/lib-common.sh"

JOB="${1:?usage: TIMER_DIR=<dir> UNIT_NAME=<name> $0 <jobname>}"
TIMER_DIR="${TIMER_DIR:?TIMER_DIR must point at the composed units dir}"
NAME="${UNIT_NAME:-claudlobby-$JOB}"

if [ ! -f "$TIMER_DIR/$NAME.service" ]; then
    printf 'install-host-service: no composed unit %s.service in %s\n' \
        "$NAME" "$TIMER_DIR" >&2
    printf 'install-host-service: (a dormant service composes NO units — arm it in system.yaml first)\n' >&2
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
    printf 'install-host-service: systemctl unavailable\n' >&2
    exit 1
fi

mkdir -p "$HOME/.config/systemd/user"
cp "$TIMER_DIR/$NAME.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now "$NAME.service"

echo "installed + started: $NAME.service"
echo "status:  systemctl --user status $NAME.service"
echo "logs:    journalctl --user -u $NAME.service -f"
