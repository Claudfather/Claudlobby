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
LOG_DIR="${CLAUDLOBBY_ROOT}/state"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/weekly-worker-restart.log"

ts=$(ts_iso)

if [ -z "$FLEET" ]; then
    echo "$ts RESTART abort: no fleet specified" >> "$LOG"
    exit 0
fi

BOTS_DIR=$(resolve_bots_dir "$FLEET")
if [ ! -d "$BOTS_DIR" ]; then
    echo "$ts RESTART warning: bots dir not found: $BOTS_DIR" >> "$LOG"
    exit 0
fi

# fleet.yaml is authoritative for which bots this fleet owns. Filter the runtime
# glob through it so a departed bot's leftover runtime dir is never bounced —
# spin-up-bot.sh would otherwise re-enroll + restart a bot the fleet no longer
# declares, resurrecting cross-fleet orphan residue. Empty list (no/unreadable
# fleet.yaml) → bot_in_fleet treats every dir as declared, preserving prior behavior.
_wr_fleet_dir=$(resolve_fleet_dir "$FLEET") || _wr_fleet_dir="$CLAUDLOBBY_ROOT/local/$FLEET"
declared_bots=$(parse_fleet_bots "$_wr_fleet_dir/fleet.yaml")

echo "$ts RESTART starting weekly worker-only bounce: $FLEET" >> "$LOG"
restarted=0; skipped=0; failed=0
for bot_dir in "$BOTS_DIR"/*/; do
    [ -d "$bot_dir" ] || continue
    bot_id=$(basename "$bot_dir")
    bot_in_fleet "$bot_id" "$declared_bots" || continue   # departed/cross-fleet residue → not ours to bounce

    # F5: managers are never auto-restarted.
    if bot_is_manager "$bot_dir"; then
        echo "$ts RESTART skip (manager): $bot_id" >> "$LOG"
        skipped=$((skipped + 1))
        continue
    fi

    echo "$ts RESTART worker: $bot_id" >> "$LOG"
    # Write a unique fence marker BEFORE the bounce so the gate below only sees a
    # BRIDGE_READY after it (rotation-proof + fail-closed; see wait_bridge_ready).
    _wr_fence="$(bridge_fence_write "$bot_dir")"
    # Best-effort handoff first — pre-stop-handoff.sh self-bounds (≤30s, early
    # exits as soon as the handoff lands) and exits 0, so it never blocks the
    # restart. The restart proceeds regardless of the handoff outcome.
    "$LIB_DIR/pre-stop-handoff.sh" "$bot_dir" >> "$LOG" 2>&1 || true

    if "$LIB_DIR/spin-up-bot.sh" "$bot_dir" >> "$LOG" 2>&1; then
        # Serialize on the Telegram bridge: wait for THIS worker's poller to come
        # ready before bouncing the next, so an all-workers weekly bounce cannot
        # mass-starve channel init (#688/#689). A gate timeout is logged + alerted
        # but does NOT abort the maintenance run — the next worker still bounces.
        if wait_bridge_ready "$bot_dir" "${WEEKLY_RESTART_CEILING:-180}" "$_wr_fence"; then
            echo "$ts RESTART ready: $bot_id" >> "$LOG"
        else
            echo "$ts RESTART bridge-timeout: $bot_id (no BRIDGE_READY in ${WEEKLY_RESTART_CEILING:-180}s)" >> "$LOG"
            emit_failure_alert "$BOTS_DIR" "bridge_down" "worker $bot_id restarted but its Telegram bridge did not come ready within ${WEEKLY_RESTART_CEILING:-180}s (weekly bounce)"
        fi
        restarted=$((restarted + 1))
    else
        rc=$?
        echo "$ts RESTART FAILED: $bot_id (spin-up-bot rc=$rc)" >> "$LOG"
        emit_failure_alert "$BOTS_DIR" "restart_failed" "worker $bot_id failed to restart on the weekly bounce (spin-up rc=$rc)"
        failed=$((failed + 1))
    fi
done
echo "$ts RESTART complete: $restarted restarted, $skipped manager(s) skipped, $failed failed" >> "$LOG"
exit 0
