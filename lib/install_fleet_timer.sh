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
#
# Env overrides (the setup backbone uses these; defaults preserve the
# fleet-timer behavior above):
#   TIMER_DIR       — source dir of composed units
#                     (default: local/<fleet>/runtime/fleet/timers)
#   UNIT_NAME       — full unit basename
#                     (default: <service_prefix>.<timer-name>)
#   SERVICE_PREFIX  — prefix for the default UNIT_NAME
#                     (default: derived from the fleet's first bot.conf)
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

TIMER="${1:?Usage: install_fleet_timer.sh <timer-name> [<fleet-name>]}"
shift

# --adopt — take over a unit another root owns, deliberately and out loud.
# Consumed before the positional fleet-name so it can be passed either side.
ADOPT=0
_args=()
for _a in "$@"; do
    case "$_a" in
        --adopt) ADOPT=1 ;;
        *) _args+=("$_a") ;;
    esac
done
set -- ${_args[@]+"${_args[@]}"}

if [ "$_OS" != "Linux" ]; then
    echo "install_fleet_timer.sh: Linux only (systemd). On macOS, use the launchd installer." >&2
    exit 1
fi
resolve_timer_unit "$(basename "$0")" "$TIMER" "${1:-}" || exit $?
NAME="$UNIT_BASENAME"

# An opt-in timer (e.g. code-audit-sweep) only has units when its fleet.yaml
# block is enabled — give a clear pointer rather than a bare cp failure.
if [[ ! -f "$TIMER_DIR/$NAME.service" ]] || [[ ! -f "$TIMER_DIR/$NAME.timer" ]]; then
    echo "Error: $NAME.{service,timer} not found in $TIMER_DIR — is the '$TIMER' timer enabled in fleet.yaml? Run 'claudlobby generate'." >&2
    exit 1
fi

mkdir -p "$HOME/.config/systemd/user"

# Ownership gate (#1152) — BEFORE the copy, so a refusal changes nothing on
# disk. The enrolling root is read from the unit being installed rather than
# derived from this script's path: both sides then answer "which root owns
# this" through the same property, and no path arithmetic can make them
# disagree.
UNIT_DEST="$HOME/.config/systemd/user/$NAME.service"
ENROLLING_ROOT="$(unit_owner_root "$TIMER_DIR/$NAME.service")"
PREV_OWNER="$(unit_owner_root "$UNIT_DEST")"
if [ "$ADOPT" = 1 ]; then
    if [ -f "$UNIT_DEST" ] && [ "$PREV_OWNER" != "$ENROLLING_ROOT" ]; then
        printf 'adopting %s: %s -> %s\n' \
            "$NAME" "${PREV_OWNER:-<no ownership marker>}" "${ENROLLING_ROOT:-<unknown>}"
    fi
else
    guard_unit_capture "$UNIT_DEST" "$ENROLLING_ROOT" "$NAME" || exit $?
fi

cp "$TIMER_DIR/$NAME.service" "$HOME/.config/systemd/user/"
cp "$TIMER_DIR/$NAME.timer" "$HOME/.config/systemd/user/"

systemctl --user daemon-reload
systemctl --user enable --now "$NAME.timer"

echo "installed + started: $NAME.timer"
echo "source:  $TIMER_DIR/$NAME.{service,timer}"
echo "status:  systemctl --user list-timers | grep $NAME"
echo "logs:    journalctl --user -u $NAME.service -f"
