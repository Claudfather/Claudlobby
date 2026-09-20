#!/usr/bin/env bash
# rolling-restart.sh — restart a fleet's bots ONE AT A TIME, gated per bot on a
# FRESH Telegram BRIDGE_READY before moving to the next (#689).
#
# WHY: restarting many authenticated bots in quick succession starves Telegram
# channel init past its readiness ceiling on every bot, and a failed bring-up
# never retries (#688) — so the whole fleet lands inbound-dead while looking
# healthy (sessions up, units green, reconcile clean, outbound fine). That was
# the 2026-07-23 fleet-wide outage: a 17-bot mass restart → 17× BRIDGE_MISSING.
# The safe recovery is serial + gated; this codifies the manual procedure that
# healed it.
#
# Per bot: [skip if already healthy] → pre-stop-handoff → spin-up-bot → WAIT for
# a fresh BRIDGE_READY in logs/startup.log (marker-fenced so a stale line cannot
# pass) up to --ceiling seconds → only THEN the next bot. Hard-stop on the first
# bot that never comes ready (proceed-anyway across the fleet is the bug itself).
#
# Usage:
#   rolling-restart.sh <fleet> [options]
#   rolling-restart.sh --all    [options]      # every fleet on the host, serially
# Options:
#   --skip-healthy       skip bots whose Telegram bridge is already up (turns this
#                        into a fleet-wide "make it so" after a partial outage)
#   --workers-only       exclude managers (MANAGER_TMUX == BOT_ID)
#   --managers-only      restart only managers (MANAGER_TMUX == BOT_ID); a
#                        coordinator is restarted too — one inert extra bot,
#                        not worked around. Mutually exclusive with
#                        --workers-only (that pair would skip every bot).
#   --ceiling <seconds>  per-bot BRIDGE_READY wait ceiling (default 180)
#   --continue-on-fail   alert + keep going past a stalled bot (default: hard-stop)
#
# Exits non-zero if any bot failed its gate (unless --continue-on-fail and only
# gate timeouts occurred, which still exits non-zero to flag the fleet).
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

# --- argument parse ----------------------------------------------------------
FLEET=""
ALL=0
SKIP_HEALTHY=0
WORKERS_ONLY=0
MANAGERS_ONLY=0
CONTINUE_ON_FAIL=0
CEILING=180

rr_parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --all)             ALL=1; shift ;;
            --skip-healthy)    SKIP_HEALTHY=1; shift ;;
            --workers-only)    WORKERS_ONLY=1; shift ;;
            --managers-only)   MANAGERS_ONLY=1; shift ;;
            --continue-on-fail) CONTINUE_ON_FAIL=1; shift ;;
            --ceiling)         CEILING="${2:?--ceiling needs a value}"; shift 2 ;;
            -h|--help)         sed -n '2,33p' "$LIB_DIR/rolling-restart.sh"; exit 0 ;;
            --*)               echo "rolling-restart: unknown option: $1" >&2; exit 2 ;;
            *)                 FLEET="$1"; shift ;;
        esac
    done
    case "$CEILING" in ''|*[!0-9]*) echo "rolling-restart: --ceiling must be an integer" >&2; exit 2 ;; esac
    if [ "$WORKERS_ONLY" -eq 1 ] && [ "$MANAGERS_ONLY" -eq 1 ]; then
        echo "rolling-restart: --workers-only and --managers-only are mutually exclusive (it would skip every bot)" >&2
        exit 2
    fi
}

# List every fleet on the host by name (the dir holding a fleet.yaml), flat OR
# nested — local/<fleet>/ and local/<system>/<fleet>/. Names are globally unique
# (#602 F5), so no de-dup needed.
rr_list_fleets() {
    local f
    for f in "$CLAUDLOBBY_ROOT"/local/*/fleet.yaml "$CLAUDLOBBY_ROOT"/local/*/*/fleet.yaml; do
        [ -f "$f" ] || continue
        basename "$(dirname "$f")"
    done
}

# Roll a single fleet. Sets global counters; returns 1 to signal a hard-stop.
rr_process_fleet() {
    local fleet="$1"
    local bots_dir fleet_dir declared bot_dir bot_id fence state auth_note why
    bots_dir="$(resolve_bots_dir "$fleet")"
    if [ ! -d "$bots_dir" ]; then
        echo "$(ts_iso) SKIP fleet: no bots dir for '$fleet' ($bots_dir)" >> "$LOG"
        return 0
    fi
    fleet_dir="$(resolve_fleet_dir "$fleet")" || fleet_dir="$CLAUDLOBBY_ROOT/local/$fleet"
    # #1146: an empty roster here does NOT mean do-nothing — bot_in_fleet reads
    # empty as "declared", so a manifest that drifts out of the documented
    # 2/4-space shape makes this act on EVERY bot dir on the host, other fleets
    # included. Classified as over-inclusive-action, not a delete; if this ever
    # grows a destructive leg, move it to declared_bots_strict (the loud door).
    declared="$(parse_fleet_bots "$fleet_dir/fleet.yaml")"

    echo "$(ts_iso) FLEET $fleet — rolling restart (ceiling ${CEILING}s, skip_healthy=$SKIP_HEALTHY workers_only=$WORKERS_ONLY managers_only=$MANAGERS_ONLY)" >> "$LOG"
    for bot_dir in "$bots_dir"/*/; do
        [ -d "$bot_dir" ] || continue
        bot_id="$(basename "$bot_dir")"
        bot_in_fleet "$bot_id" "$declared" || continue   # departed / cross-fleet residue → not ours

        if [ "$WORKERS_ONLY" -eq 1 ] && bot_is_manager "$bot_dir"; then
            echo "$(ts_iso) SKIP (manager): $bot_id" >> "$LOG"; SKIPPED=$((SKIPPED + 1)); continue
        fi
        if [ "$MANAGERS_ONLY" -eq 1 ] && ! bot_is_manager "$bot_dir"; then
            echo "$(ts_iso) SKIP (worker): $bot_id" >> "$LOG"; SKIPPED=$((SKIPPED + 1)); continue
        fi
        if [ "$SKIP_HEALTHY" -eq 1 ]; then
            state="$(bridge_state "$bot_dir" 2>/dev/null || true)"
            if [ "$state" = "up" ]; then
                echo "$(ts_iso) SKIP (bridge already up): $bot_id" >> "$LOG"; SKIPPED=$((SKIPPED + 1)); continue
            fi
        fi

        # Write a unique fence marker BEFORE the restart so only a BRIDGE_READY
        # after it counts (rotation-proof + fail-closed; see wait_bridge_ready).
        fence="$(bridge_fence_write "$bot_dir")"

        echo "$(ts_iso) RESTART: $bot_id" >> "$LOG"
        "$LIB_DIR/pre-stop-handoff.sh" "$bot_dir" >> "$LOG" 2>&1 || true
        if ! "$LIB_DIR/spin-up-bot.sh" "$bot_dir" >> "$LOG" 2>&1; then
            rr_fail "$fleet" "$bot_id" "$bots_dir" "spin-up-bot failed" || return 1
            continue
        fi
        if wait_bridge_ready "$bot_dir" "$CEILING" "$fence"; then
            echo "$(ts_iso) READY: $bot_id" >> "$LOG"; RESTARTED=$((RESTARTED + 1))
        else
            # A stalled gate used to report only its own ceiling, while the most
            # likely cause was already on disk and unnamed (#1358). Worse, the
            # advice an operator actually reads here comes from start-bot via
            # spin-up-bot (redirected into THIS log): "BRIDGE_MISSING ...
            # keepalive owns heal". For this one cause that is precisely wrong --
            # keepalive restarts the bot, the restart re-reads the same
            # host-global cache, the poller is skipped again, and the gate waits
            # out a BRIDGE_READY that can never arrive. That is what another
            # fleet reports stalled their fleet-wide restart on 2026-09-19 --
            # their report, relayed onto #1358, not verified on this host. It is
            # also the report that widened the trigger past a credential-less
            # bot, so it is carried as a report rather than flattened into fact.
            #
            # So name the signature instead. Same plain file read as start-bot,
            # scoped to THIS bot dir so a bot pinned to its own CLAUDE_CONFIG_DIR
            # is described by the cache its session actually consults.
            auth_note="$(mcp_auth_cache_note "$bot_dir" 2>/dev/null || true)"
            why="no BRIDGE_READY within ${CEILING}s"
            if [ -n "$auth_note" ]; then
                echo "$(ts_iso) $auth_note" >> "$LOG"
                # The alert is a Telegram-bound one-liner, so it carries the
                # signature and the address of the detail, never the detail.
                why="$why — host-global MCP auth cache is ARMED, so a restart re-reads it and skips the poller again; keepalive cannot heal this. Detail + remedy: $LOG"
            fi
            rr_fail "$fleet" "$bot_id" "$bots_dir" "$why" || return 1
        fi
    done
    return 0
}

# Record a failed bot: log + loud fleet alert. Returns 1 (hard-stop) unless
# --continue-on-fail, then 0 (keep rolling) — but FAILED is set either way, so
# the run still exits non-zero.
rr_fail() {
    local fleet="$1" bot_id="$2" bots_dir="$3" why="$4"
    echo "$(ts_iso) FAILED: $bot_id — $why" >> "$LOG"
    emit_failure_alert "$bots_dir" "rolling_restart_stalled" \
        "rolling-restart[$fleet]: $bot_id $why — halted before the rest of the fleet" || true
    FAILED=$((FAILED + 1))
    [ "$CONTINUE_ON_FAIL" -eq 1 ]
}

rr_main() {
    rr_parse_args "$@"

    LOG_DIR="${CLAUDLOBBY_ROOT}/state"
    mkdir -p "$LOG_DIR"
    LOG="$LOG_DIR/rolling-restart.log"
    install_error_trap ""

    RESTARTED=0; SKIPPED=0; FAILED=0

    local fleets=() f
    if [ "$ALL" -eq 1 ]; then
        while IFS= read -r f; do [ -n "$f" ] && fleets+=("$f"); done < <(rr_list_fleets)
    elif [ -n "$FLEET" ]; then
        fleets=("$FLEET")
    else
        echo "rolling-restart: give a <fleet> or --all" >&2; exit 2
    fi

    for f in "${fleets[@]}"; do
        if ! rr_process_fleet "$f"; then
            echo "$(ts_iso) HALT: hard-stop after a stalled bot in '$f' ($RESTARTED ok, $SKIPPED skipped, $FAILED failed)" >> "$LOG"
            echo "rolling-restart: HALTED in '$f' — $FAILED bot(s) failed the bridge gate (see $LOG)" >&2
            exit 1
        fi
    done

    echo "$(ts_iso) DONE: $RESTARTED restarted, $SKIPPED skipped, $FAILED failed" >> "$LOG"
    if [ "$FAILED" -gt 0 ]; then
        echo "rolling-restart: completed with $FAILED failure(s) (see $LOG)" >&2
        exit 1
    fi
    exit 0
}

# Source-guard: `. rolling-restart.sh` in a test defines the functions without
# running main (mirrors how the file-op tests source the migration tool).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    rr_main "$@"
fi
