#!/usr/bin/env bash
# weekly-worker-restart.sh — Weekly lossless restart of WORKER bots only.
#
# Mechanism 2 of the fleet update lifecycle. The Claude Code binary cannot
# hot-reload, so it is applied by restart. This bounces every WORKER bot once a
# week to pick up a staged binary (downloaded daily by update-claude-code.sh),
# each via a lossless intentional restart:
#
#   claudlobby bot restart (best-effort handoff, exact native restart,
#     fresh bridge and session readiness proof)
#
# MANAGERS are excluded: their long-horizon orchestration context is the least
# summarizable, so they are never auto-restarted — they pick up a new binary on
# a deliberate human restart (or any natural restart). A manager is identified
# by MANAGER_TMUX == BOT_ID (bot_is_manager). A worker that fails to restart
# raises a loud failure via the shared emit_failure_alert primitive (fleet event
# + manager tmux nudge + Telegram escalation) — the same alert path Mechanism 1
# uses, so the two mechanisms never fork it.
#
# Runs weekly via systemd timer (see system.yaml defaults.jobs). Also
# invocable on demand: weekly-worker-restart.sh <fleet>.
#
# Usage: weekly-worker-restart.sh [<fleet-name>]

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
# the doors this script runs anchor their fleet events on it (F18 R1)
[ -z "$FLEET" ] || export CLAUDLOBBY_FLEET="${CLAUDLOBBY_FLEET:-$FLEET}"
LOG_DIR="${CLAUDLOBBY_ROOT}/state"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/weekly-worker-restart.log"

log() { printf '%s %s\n' "$(ts_iso)" "$*" >> "$LOG"; }

if [ -z "$FLEET" ]; then
    log "RESTART abort: no fleet specified"
    exit 0
fi

BOTS_DIR=$(resolve_bots_dir "$FLEET")
if [ ! -d "$BOTS_DIR" ]; then
    log "RESTART warning: bots dir not found: $BOTS_DIR"
    exit 0
fi

# fleet.yaml is authoritative for which bots this fleet owns. Filter the runtime
# glob through it so a departed bot's leftover runtime dir is never bounced —
# a restart must not re-enroll a bot the fleet no longer
# declares, resurrecting cross-fleet orphan residue. Empty list (no/unreadable
# fleet.yaml) → bot_in_fleet treats every dir as declared, preserving prior behavior.
# #1146: that fallback is over-inclusive, not a no-op — a drifted manifest makes
# this weekly cron bounce every bot dir on the host, other fleets included.
_wr_fleet_dir=$(resolve_fleet_dir "$FLEET") || _wr_fleet_dir="$CLAUDLOBBY_ROOT/local/$FLEET"
declared_bots=$(parse_fleet_bots "$_wr_fleet_dir/fleet.yaml")

log "RESTART starting weekly worker-only bounce: $FLEET"
restarted=0; skipped=0; failed=0
for bot_dir in "$BOTS_DIR"/*/; do
    [ -d "$bot_dir" ] || continue
    bot_id=$(basename "$bot_dir")
    bot_in_fleet "$bot_id" "$declared_bots" || continue   # departed/cross-fleet residue → not ours to bounce

    # F5: managers are never auto-restarted.
    if bot_is_manager "$bot_dir"; then
        log "RESTART skip (manager): $bot_id"
        skipped=$((skipped + 1))
        continue
    fi

    # A declared worker can be deliberately de-enrolled by `bot stop`.
    # This scheduled maintenance tick must not turn that stop into a start.
    if ! svc_is_registered "$bot_dir"; then
        log "RESTART skip (de-enrolled): $bot_id"
        skipped=$((skipped + 1))
        continue
    fi

    log "RESTART worker: $bot_id"
    # The canonical restart owns the handoff, fresh fence and bridge/session
    # proof. Keep only this weekly driver's worker policy and alert behavior.
    _wr_rc_s="$(bot_conf_get "$bot_dir" RC_READY_TIMEOUT_S 90)"
    case "$_wr_rc_s" in ''|*[!0-9]*) _wr_rc_s=90 ;; esac
    # Decimal conversion preserves the composed timeout when zero-padded.
    # The explicit weekly override remains authoritative.
    _wr_ceiling="${WEEKLY_RESTART_CEILING:-$((10#$_wr_rc_s + 120))}"
    if claudlobby_cli --root "$CLAUDLOBBY_ROOT" --fleet "$FLEET" \
            bot restart "$bot_id" --ceiling "$_wr_ceiling" --json >> "$LOG" 2>&1; then
        log "RESTART ready: $bot_id"
        restarted=$((restarted + 1))
    else
        rc=$?
        log "RESTART FAILED: $bot_id (bot restart rc=$rc; bridge ceiling ${_wr_ceiling}s)"
        emit_failure_alert "$BOTS_DIR" "restart_failed" "worker $bot_id failed restart or bridge readiness on the weekly bounce (bot restart rc=$rc; bridge ceiling ${_wr_ceiling}s)"
        failed=$((failed + 1))
    fi
done
log "RESTART complete: $restarted restarted, $skipped manager(s) skipped, $failed failed"
exit 0
