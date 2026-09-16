#!/bin/bash
# Enroll a composed HOST SERVICE unit (systemd): the .service-without-.timer
# shape install_fleet_timer.sh refuses by contract (it requires both files).
# First tenant: claudlobby-plane-daemon. Idempotent; same TIMER_DIR/UNIT_NAME
# override convention as the timer enroller so setup-system drives both alike.
#
# Usage: TIMER_DIR=<dir> UNIT_NAME=<basename> lib/install-host-service-systemd.sh <jobname> [--adopt]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$SCRIPT_DIR/lib-common.sh"

JOB="${1:?usage: TIMER_DIR=<dir> UNIT_NAME=<name> $0 <jobname> [--adopt]}"
ADOPT=0
[ "${2:-}" = "--adopt" ] && ADOPT=1
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

# Same ownership gate as install_fleet_timer.sh: host units are one-per-host
# under a fixed name, so enrolling from a second tree would silently capture
# the first tree's unit — refused unless --adopt says that is the intent.
UNIT_DEST="$HOME/.config/systemd/user/$NAME.service"
ENROLLING_ROOT="$(unit_owner_root "$TIMER_DIR/$NAME.service")"
if [ "$ADOPT" = 1 ]; then
    PREV_OWNER="$(unit_owner_root "$UNIT_DEST")"
    if [ -f "$UNIT_DEST" ] && [ "$PREV_OWNER" != "$ENROLLING_ROOT" ]; then
        printf 'adopting %s: %s -> %s\n' \
            "$NAME" "${PREV_OWNER:-<no ownership marker>}" "${ENROLLING_ROOT:-<unknown>}"
    fi
else
    guard_unit_capture "$UNIT_DEST" "$ENROLLING_ROOT" "$NAME" || exit $?
fi

mkdir -p "$HOME/.config/systemd/user"
cp "$TIMER_DIR/$NAME.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now "$NAME.service"

echo "installed + started: $NAME.service"
echo "status:  systemctl --user status $NAME.service"
echo "logs:    journalctl --user -u $NAME.service -f"
