#!/bin/bash
# install_fleet_timer_launchd.sh — Shared launchd enrollment for fleet timers.
#
# launchd sibling of install_fleet_timer.sh: copies the composer-generated
# <prefix>.<name>.plist from runtime/fleet/timers/ into ~/Library/LaunchAgents/
# and (re)loads it. One enroll implementation for every fleet timer — the
# composer already emits the plists, so nothing is regenerated here.
#
# Run `claudlobby generate` first to produce the units.
#
# Usage: install_fleet_timer_launchd.sh <timer-name> [<fleet-name>]
#
# Env overrides (the setup backbone uses these; defaults preserve the
# fleet-timer behavior above):
#   TIMER_DIR       — source dir of composed units
#                     (default: local/<fleet>/runtime/fleet/timers)
#   UNIT_NAME       — full plist basename / launchd Label
#                     (default: <service_prefix>.<timer-name>)
#   SERVICE_PREFIX  — prefix for the default UNIT_NAME
#                     (default: derived from the fleet's first bot.conf)
#
# Exit: 0 installed + loaded, or already current | 1 failed | 2 usage |
#   3 refused: another root owns the installed plist (see --adopt) |
#   4 deferred: the job is running this enrollment and its composed plist
#     changed, so nothing was touched (svc_enroll_agent, lib/supervisor.sh)
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

TIMER="${1:?Usage: install_fleet_timer_launchd.sh <timer-name> [<fleet-name>]}"
shift

# --adopt — see the systemd sibling. Parsed identically on both platforms so a
# runbook step does not become platform-specific.
ADOPT=0
_args=()
for _a in "$@"; do
    case "$_a" in
        --adopt) ADOPT=1 ;;
        *) _args+=("$_a") ;;
    esac
done
set -- ${_args[@]+"${_args[@]}"}

if [ "$_OS" != "Darwin" ]; then
    echo "install_fleet_timer_launchd.sh: macOS only (launchd). On Linux, use install_fleet_timer.sh." >&2
    exit 1
fi
resolve_timer_unit "$(basename "$0")" "$TIMER" "${1:-}" || exit $?
LABEL="$UNIT_BASENAME"

SRC_PLIST="$TIMER_DIR/$LABEL.plist"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# An opt-in timer (e.g. code-audit-sweep) only has units when its fleet.yaml
# block is enabled — give a clear pointer rather than a bare cp failure.
if [ ! -f "$SRC_PLIST" ]; then
    echo "Error: $SRC_PLIST not found — is the '$TIMER' timer enabled in fleet.yaml? Run 'claudlobby generate'." >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"

# Ownership gate (#1152) — BEFORE the copy, so a refusal changes nothing on
# disk. Same predicate as the systemd sibling: a host job captured on macOS is
# the same defect, and a guard on one platform only would leave the other
# silently capturing.
ENROLLING_ROOT="$(unit_owner_root "$SRC_PLIST")"
PREV_OWNER="$(unit_owner_root "$PLIST")"
if [ "$ADOPT" = 1 ]; then
    if [ -f "$PLIST" ] && [ "$PREV_OWNER" != "$ENROLLING_ROOT" ]; then
        printf 'adopting %s: %s -> %s\n' \
            "$LABEL" "${PREV_OWNER:-<no ownership marker>}" "${ENROLLING_ROOT:-<unknown>}"
    fi
else
    guard_unit_capture "$PLIST" "$ENROLLING_ROOT" "$LABEL" || exit $?
fi

# Copy + (re)load through the adapter, which never stops a job that is
# running this very enrollment: the nightly reload-fleet enrolls every fleet
# job, its own included, and the bootout of its own label used to kill the run
# before the bootstrap after it (#1924). Exit 4 then, with nothing changed.
svc_enroll_agent "$LABEL" "$SRC_PLIST" || exit $?
echo "plist:  $PLIST (source: $SRC_PLIST)"
echo "status: launchctl print gui/$(id -u)/$LABEL | grep state"
