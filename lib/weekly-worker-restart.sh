#!/usr/bin/env bash
# weekly-worker-restart.sh — Weekly lossless restart of WORKER bots only.
#
# Mechanism 2 of the fleet update lifecycle. The Claude Code binary cannot
# hot-reload, so it is applied by restart. This bounces every WORKER bot once a
# week to pick up a staged binary (downloaded daily by update-claude-code.sh),
# each via a lossless intentional restart:
#
#   pre-stop-handoff.sh  (writes a session.md handoff, best-effort, never blocks)
#     → spin-up-bot.sh   (cross-platform idempotent restart)
#     → start-bot.sh resumes from the handoff (age-gated) on the new session
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
# spin-up-bot.sh would otherwise re-enroll + restart a bot the fleet no longer
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

    log "RESTART worker: $bot_id"
    # Write a unique fence marker BEFORE the bounce so the gate below only sees a
    # BRIDGE_READY after it (rotation-proof + fail-closed; see wait_bridge_ready).
    _wr_fence="$(bridge_fence_write "$bot_dir")"
    # Best-effort handoff first — pre-stop-handoff.sh self-bounds (≤30s, early
    # exits as soon as the handoff lands) and exits 0, so it never blocks the
    # restart. The restart proceeds regardless of the handoff outcome.
    "$LIB_DIR/pre-stop-handoff.sh" "$bot_dir" >> "$LOG" 2>&1 || true

    if "$LIB_DIR/spin-up-bot.sh" "$bot_dir" >> "$LOG" 2>&1; then
        # F4 coupling: bot.conf is the carrier for the launcher's OWN readiness
        # ceiling (RC_READY_TIMEOUT_S, composed from host.boot.mcp_timeout_ms),
        # and start-bot.sh writes BRIDGE_READY only AFTER that poll finishes —
        # so a fixed gate shorter than it alerts bridge_down on a bot that is
        # merely slow and healthy (the composed ceiling is 200 at this tip; the
        # old fixed 180 sat inside that band, and the band widens with every
        # raise of mcp_timeout_ms). Derive from the bot's own composed value plus
        # the margin for pre-stop-handoff + spin-up + the poller settle, so a
        # policy change moves this driver too. WEEKLY_RESTART_CEILING stays the
        # operator override and wins when set.
        _wr_rc_s="$(bot_conf_get "$bot_dir" RC_READY_TIMEOUT_S 90)"
        case "$_wr_rc_s" in ''|*[!0-9]*) _wr_rc_s=90 ;; esac
        # 10# forces base 10 -- the digits guard admits a ZERO-PADDED value and
        # bare $(( 090 )) is read as octal ("value too great for base"). What
        # that costs here is worse than an abort, and is MEASURED rather than
        # reasoned: bash treats an expansion error as a discard of the
        # enclosing command, so the rest of this iteration AND every remaining
        # iteration of the per-bot loop are skipped -- yet the script runs on
        # to its summary line and exits 0. One zero-padded bot would silently
        # truncate the weekly bounce, leaving every later worker un-restarted
        # under a "RESTART complete" line, with the shell's diagnostic going
        # to the journal rather than to this log.
        _wr_ceiling="${WEEKLY_RESTART_CEILING:-$((10#$_wr_rc_s + 120))}"
        # Serialize on the Telegram bridge: wait for THIS worker's poller to come
        # ready before bouncing the next, so an all-workers weekly bounce cannot
        # mass-starve channel init (#688/#689). A gate timeout is logged + alerted
        # but does NOT abort the maintenance run — the next worker still bounces.
        if wait_bridge_ready "$bot_dir" "$_wr_ceiling" "$_wr_fence"; then
            log "RESTART ready: $bot_id"
        else
            log "RESTART bridge-timeout: $bot_id (no BRIDGE_READY in ${_wr_ceiling}s)"
            emit_failure_alert "$BOTS_DIR" "bridge_down" "worker $bot_id restarted but its Telegram bridge did not come ready within ${_wr_ceiling}s (weekly bounce)"
        fi
        restarted=$((restarted + 1))
    else
        rc=$?
        log "RESTART FAILED: $bot_id (spin-up-bot rc=$rc)"
        emit_failure_alert "$BOTS_DIR" "restart_failed" "worker $bot_id failed to restart on the weekly bounce (spin-up rc=$rc)"
        failed=$((failed + 1))
    fi
done
log "RESTART complete: $restarted restarted, $skipped manager(s) skipped, $failed failed"
exit 0
