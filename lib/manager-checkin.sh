#!/bin/bash
# manager-checkin.sh -- the check-in beat. The composed `<prefix>.manager-checkin`
# FLEET timer execs this; the fleet arrives as $1 the way `task-recheck.sh` takes it.
#
# Usage: manager-checkin.sh <fleet> [--min-gap-s N]
#   rc 0 always, on every operating path (spec section 14) -- a manager it chose
#   not to wake is not a failure. rc 2 is the ONE refusal: a malformed call.
#
# ARMING is enrollment, not a flag: `defaults.jobs.manager-checkin.enroll: true`
# in fleet.yaml, then generate + lib/setup-fleet. The PER-BOT gate is the
# composed `checkin` skill symlink -- that is how a worker, a coordinator and an
# opted-out manager are all excluded, and how an operator un-equips one.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
[ $# -gt 0 ] && shift
MIN_GAP_S="${CHECKIN_MIN_GAP_S:-2700}"
while [ $# -gt 0 ]; do
    case "$1" in
        --min-gap-s)
            if [ -z "${2:-}" ]; then
                printf 'manager-checkin: --min-gap-s needs a value\n' >&2; exit 2
            fi
            MIN_GAP_S="$2"; shift 2 ;;
        *) printf 'manager-checkin: unknown argument: %s\n' "$1" >&2; exit 2 ;;
    esac
done
if [ -z "$FLEET" ]; then
    printf 'manager-checkin: no fleet named (usage: manager-checkin.sh <fleet> [--min-gap-s N])\n' >&2
    exit 2
fi
case "$MIN_GAP_S" in ''|*[!0-9]*)
    printf 'manager-checkin: --min-gap-s takes whole seconds, got: %s\n' "$MIN_GAP_S" >&2; exit 2 ;;
esac

ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
export CLAUDLOBBY_ROOT="$ROOT"
LOG="${MANAGER_CHECKIN_LOG:-$ROOT/logs/manager-checkin.log}"
setup_log_dir "$LOG"
TS="$(ts_iso)"
SINCE="$(epoch_to_iso_utc "$(( $(date +%s) - MIN_GAP_S ))")"

ck_skip() {   # <bot_dir> <bot_id> <reason> <log note>
    echo "$TS SKIP $2 -- $4" >> "$LOG"
    emit_fleet_event checkin_skipped manager-checkin \
        "$(printf '{"bot":"%s","reason":"%s"}' "$(json_escape "$2")" "$3")" "$1" "$2"
}

ROSTER="$(safe_mktemp)"; BAD="$(safe_mktemp)"; HITS="$(safe_mktemp)"
# Form D, both calls: rc 1 here means SOME manifest was bad, and the good rows
# still printed -- a disclosure, never a reason to stop. A substitution would
# fire the ERR trap and record a critical row for a normal outcome.
if declared_bots_strict "$BAD" > "$ROSTER"; then :; else
    [ -s "$BAD" ] && sed 's/^/manager-checkin: roster: /' "$BAD" >&2 || true
fi

while IFS="$(printf '\t')" read -r bot bot_fleet bot_dir; do
    [ -n "$bot" ] || continue
    [ "$bot_fleet" = "$FLEET" ] || continue
    [ -d "$bot_dir" ] || continue
    bot_is_manager "$bot_dir" || continue                      # silent: a worker
    [ -e "$bot_dir/.claude/skills/checkin" ] || continue       # silent: not equipped
    bot_id="$(bot_conf_get "$bot_dir" BOT_ID "$bot")"
    socket="$(tmux_socket_for_bot "$bot_dir" 2>/dev/null || true)"
    if ! check_tmux_session "$bot" "$socket"; then
        ck_skip "$bot_dir" "$bot_id" session_down "session not alive"; continue
    fi
    if bot_is_busy "$socket" "$bot" "$bot_dir"; then
        ck_skip "$bot_dir" "$bot_id" busy "mid-turn"; continue
    fi
    # THE RATE LIMIT IS A PLANE READ (task-recheck rule: no timer state file to
    # lose or to lie). Unreachable is NOT empty: a money-spending action must
    # not run blind, so rc 3 skips without firing and without a row -- the plane
    # is the thing that could not be reached.
    if python3 -S -E "$LIB_DIR/plane-lookup.py" --root "$ROOT" --events \
            --fleet "$FLEET" --bot "$bot_id" --type checkin_triggered \
            --since "$SINCE" > "$HITS" 2>> "$LOG"; then
        if [ -s "$HITS" ]; then
            echo "$TS SKIP $bot_id -- checked in within ${MIN_GAP_S}s" >> "$LOG"; continue
        fi
    else
        echo "$TS SKIP $bot_id -- plane unreachable, not firing" >> "$LOG"; continue
    fi
    if "$LIB_DIR/dispatch.sh" "$bot" "/checkin"; then
        echo "$TS DISPATCH $bot_id -- /checkin sent" >> "$LOG"
        emit_fleet_event checkin_triggered manager-checkin \
            "$(printf '{"bot":"%s","min_gap_s":%s}' "$(json_escape "$bot_id")" "$MIN_GAP_S")" \
            "$bot_dir" "$bot_id"
    else
        ck_skip "$bot_dir" "$bot_id" send_failed "dispatch failed"
    fi
done < "$ROSTER"
exit 0
