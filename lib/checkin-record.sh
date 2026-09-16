#!/bin/bash
# checkin-record.sh -- THE write door for a manager check-in decision
# (manager check-in spec §7). The skill hands it the decision JSON on stdin;
# it mints the checkin_id (plane_mint_id ck -- the one mint every door uses),
# validates the schema-1 contract (checkin-contract.py), and lands ONE
# actor-anchored system event `checkin_decision`, stamped source_ref
# checkin:<checkin_id> -- the task-recheck stamp idiom: the ref is the address
# a reader joins on.
#
# Usage: checkin-record.sh [--dry-run] < decision.json
#   stdout: the checkin_id (one line); DRY-RUN <checkin_id> under --dry-run
#   exit:   0 recorded (committed or spooled), or validated under --dry-run
#           1 usage (unknown flag; no identity)
#           2 contract refused (every reason on stderr; nothing recorded)
#           3 the plane could not record (disclosed; nothing recorded)
#
# Identity is BOT_ID + FLEET_NAME from the environment the manager session
# sources from bot.conf -- never BOT_NAME (a display field), and no flags: no
# caller supplies them. RECORD BEFORE ACT is the skill's rule, so for THIS door
# the record IS the action: an unrecorded decision is a failure it says so
# about (rc 3), never a silent 0. The plane is always on; PLANE_EMIT_DISABLED=1
# (the harness exemption) is the one silencer, also rc 3.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        -h|--help) show_help "$0"; exit 0 ;;
        *) printf 'checkin-record: unknown flag %s (try --help; the decision rides stdin, never a flag)\n' "$1" >&2; exit 1 ;;
    esac
done
BOT="${BOT_ID:-}"
FLEET="${FLEET_NAME:-${CLAUDLOBBY_FLEET:-}}"
if [ -z "$BOT" ] || [ -z "$FLEET" ]; then
    printf 'checkin-record: no identity -- BOT_ID and FLEET_NAME must be set (a manager session sources them from bot.conf)\n' >&2
    exit 1
fi

checkin_id=$(plane_mint_id ck)
raw=$(cat)
tmp=$(safe_mktemp)
# The contract runs as a top-level pipeline into a file, never inside a
# command substitution: install_error_trap arms an ERR trap that bash fires
# INSIDE a substitution whatever the surrounding if/||/set +e (measured), and
# that trap lands a critical script_error row -- a refused decision must
# record NOTHING. A failing pipeline as an if-condition fires no trap.
if printf '%s' "$raw" | python3 "$LIB_DIR/checkin-contract.py" --checkin-id "$checkin_id" > "$tmp"; then
    normalized=$(cat "$tmp")
    rm -f "$tmp"
else
    rm -f "$tmp"
    printf 'checkin-record: decision refused (nothing recorded)\n' >&2
    exit 2
fi

if [ "$DRY" = "1" ]; then
    printf 'DRY-RUN %s\n' "$checkin_id"
    exit 0
fi
if ! plane_armed checkin-record; then
    printf 'checkin-record: plane silenced (PLANE_EMIT_DISABLED=1) -- decision %s NOT recorded\n' "$checkin_id" >&2
    exit 3
fi
utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
safe_fleet=$(json_escape "$FLEET")
printf -v batch '{"events":[{"event_type":"system","emitter":"checkin-record","source_ref":"checkin:%s","fleet":"%s","occurred_at":"%s","payload":{"event":"checkin_decision","subject_kind":"actor","subject":"bot:%s/%s","data":%s}}]}' \
    "$checkin_id" "$safe_fleet" "$utc" "$safe_fleet" "$(json_escape "$BOT")" "$normalized"
plane_emit_events checkin-record <<<"$batch"
if [ "${PLANE_EMIT_LAST_RC:-0}" -ne 0 ]; then
    printf 'checkin-record: plane record failed rc=%s -- decision %s NOT recorded (for THIS door the record is the action; the door-action-unaffected line above does not apply; claudlobby plane doctor says why, claudlobby plane spool drains what was spooled)\n' "$PLANE_EMIT_LAST_RC" "$checkin_id" >&2
    exit 3
fi
printf '%s\n' "$checkin_id"
