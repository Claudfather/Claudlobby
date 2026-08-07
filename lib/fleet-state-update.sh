#!/bin/bash
# fleet-state-update.sh — update a bot row in fleet-state.json.
#
# Called by start-bot.sh (boot → idle) and report-back.sh (completion → idle / blocked).
#
# Usage:
#   fleet-state-update.sh <bot> <status> [<current_task>] [<current_repo>] [<last_completed>]
#     status: idle | working | blocked | offline
#
#   fleet-state-update.sh prune <fleet-yaml-path> [--dry-run]
#     Remove bot entries not present in the given fleet.yaml.
#     --dry-run reports exactly what would go and writes nothing — this is what
#     the reconcile AUDIT path calls. fleet-state.json is host-shared, so a
#     prune deletes rows belonging to every OTHER fleet on the host too; that
#     is destructive and must never ride along on a report-only verb (#892).
#
#   fleet-state-update.sh delete <bot>...
#     Surgically remove one or more named bot rows (leaves all others). The
#     single-key inverse of prune — used by spin-down-bot.sh to reap a throwaway.
#
# Scaling note: the single-file + lock design works well for <50 bots.
# Beyond that, consider per-bot state files (state/<bot>.json) or a
# lightweight SQLite database to reduce lock contention.
#
# Locking is via lib-common's with_lock — flock(1) where available, otherwise
# an atomic mkdir-based spinlock (stock macOS has no flock).
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
STATE="${FLEET_STATE_PATH:-$CLAUDLOBBY_ROOT/state/fleet-state.json}"
mkdir -p "$(dirname "$STATE")"

# --- Prune subcommand ---------------------------------------------------------
if [ "${1:-}" = "prune" ]; then
    FLEET_YAML="${2:?Usage: fleet-state-update.sh prune <fleet-yaml-path> [--dry-run]}"
    DRY_RUN=""
    [ "${3:-}" = "--dry-run" ] && DRY_RUN=1
    if [ ! -f "$FLEET_YAML" ]; then
        echo "fleet-state-update: $FLEET_YAML not found" >&2
        exit 1
    fi
    [ -f "$STATE" ] || exit 0  # nothing to prune

    # ONE sanctioned parser (lib-common.sh). What used to sit here was a
    # byte-identical COPY of parse_fleet_bots, free to drift from it silently.
    DEFINED=$(parse_fleet_bots "$FLEET_YAML")

    # Zero extraction is never a legitimate prune input: a fleet declaring no
    # bots has nothing to reconcile. Without this bail the empty keep-set matches
    # no key at all, so every row on a file that EVERY fleet shares is deleted.
    # Refuse on both planes rather than fail open.
    if [ -z "$DEFINED" ]; then
        echo "fleet-state-update: refusing to prune — extracted ZERO bots from $FLEET_YAML (comment, indent or CRLF drift from the documented shape?). No rows touched." >&2
        emit_fleet_event "script_error" "fleet-state-update" \
            "$(printf '{"action":"prune_refused","fleet_yaml":"%s"}' "$(json_escape "$FLEET_YAML")")" "" || true
        exit 1
    fi

    # Build a JSON object of defined bot names for jq --argjson
    JQ_KEEP=$(printf '%s\n' "$DEFINED" | awk '{printf "\"%s\": 1, ", $0}' | sed 's/, $//')

    # The fleet doing the pruning, derived the same way discover_fleet_manifests
    # derives every other fleet name, so the two can never disagree.
    THIS_FLEET=$(basename "$(dirname "$FLEET_YAML")")

    # bot<TAB>fleet for every bot declared ANYWHERE on this host, built once.
    # fleet-state.json is host-shared, so a row this fleet does not declare
    # usually belongs to a sibling — naming that sibling is the whole point.
    ATTR=$(
        while IFS=$'\t' read -r _fn _fy; do
            [ -n "$_fn" ] || continue
            parse_fleet_bots "$_fy" | while read -r _b; do
                [ -n "$_b" ] && printf '%s\t%s\n' "$_b" "$_fn"
            done
        done < <(discover_fleet_manifests)
    )

    _fleet_of() { printf '%s\n' "$ATTR" | awk -F'\t' -v b="$1" '$1 == b { print $2; exit }'; }
    _count() { printf '%s\n' "$1" | grep -c . || true; }

    # Report what is MISSING host-wide, never only what THIS run removed. A run
    # that deletes 2 rows because 15 were already gone prints a small number,
    # which reads as reassuring and is the exact opposite of the truth. That
    # discrepancy is what made the first count on #892 wrong.
    _prune_report() {  # <verb> <rows-being-removed>
        local verb="$1" rows="$2" f b grp foreign=0 declared present surviving gone
        echo "$verb $(_count "$rows") row(s) from the HOST-SHARED fleet state ($STATE):"
        for f in $(printf '%s\n' "$ATTR" | cut -f2 | sort -u); do
            grp=""
            for b in $rows; do
                [ "$(_fleet_of "$b")" = "$f" ] && grp="$grp $b"
            done
            [ -n "$grp" ] || continue
            if [ "$f" = "$THIS_FLEET" ]; then
                echo "    $f (this fleet):$grp"
            else
                echo "    $f (NOT this fleet — these rows are not yours):$grp"
                for b in $grp; do foreign=$((foreign + 1)); done
            fi
        done
        grp=""
        for b in $rows; do
            [ -z "$(_fleet_of "$b")" ] && grp="$grp $b"
        done
        [ -n "$grp" ] && echo "    (declared by no fleet on this host):$grp"
        [ "$foreign" -gt 0 ] && \
            echo "  -> $foreign of $(_count "$rows") belong to OTHER fleets. Every fleet on this host shares this one file."
        # Absent = host-declared bots holding no row once this completes.
        declared=$(printf '%s\n' "$ATTR" | cut -f1 | sort -u)
        present=$(jq -r '.bots | keys[]' "$STATE" | sort -u)
        # Rows still present once this operation completes. On the real path the
        # file is already written so removing "$rows" again is a no-op; on the
        # --dry-run path the file is untouched so this is what WOULD remain.
        # One expression, correct on both.
        surviving=$(printf '%s\n' "$present" | grep -vxF -f <(printf '%s\n' "$rows") || true)
        gone=$(comm -23 <(printf '%s\n' "$declared") <(printf '%s\n' "$surviving" | sort -u))
        echo "  -> $(_count "$gone") of $(_count "$declared") host-declared bots have NO row after this."
    }

    _prune_state() {
        local tmp rows
        rows=$(jq -r --argjson keep "{${JQ_KEEP}}" '.bots | keys[] | select($keep[.] == null)' "$STATE")
        [ -z "$rows" ] && return 0
        if [ -n "$DRY_RUN" ]; then
            _prune_report "WOULD prune" "$rows"
            echo "  -> nothing was written. This is an audit; re-run with --enroll to apply."
            return 0
        fi
        tmp=$(safe_mktemp)
        jq --argjson keep "{${JQ_KEEP}}" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            '.updated = $ts | .bots |= with_entries(select($keep[.key] == 1))' "$STATE" > "$tmp" \
            && mv "$tmp" "$STATE" \
            || { echo "fleet-state-update: failed to write $STATE" >&2; rm -f "$tmp"; return 1; }
        _prune_report "Pruned" "$rows"
    }
    with_lock "$STATE.lock" _prune_state
    exit 0
fi

# --- Delete subcommand --------------------------------------------------------
# Surgically remove named bot rows only (never other bots) — the single-key
# inverse of prune. Idempotent: a missing key or absent state file is a no-op.
if [ "${1:-}" = "delete" ]; then
    shift
    [ "$#" -ge 1 ] || { echo "Usage: fleet-state-update.sh delete <bot>..." >&2; exit 2; }
    [ -f "$STATE" ] || exit 0  # nothing to delete
    _delete_state() {
        local tmp keys
        keys=$(printf '%s\n' "$@" | jq -Rnc '[inputs | select(length > 0)]')
        tmp=$(safe_mktemp)
        jq --argjson keys "$keys" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            'reduce $keys[] as $b (.; del(.bots[$b])) | .updated = $ts' "$STATE" > "$tmp" \
            && mv "$tmp" "$STATE" || { echo "fleet-state-update: failed to write $STATE" >&2; rm -f "$tmp"; return 1; }
    }
    with_lock "$STATE.lock" _delete_state "$@"
    exit 0
fi

# --- Normal update ------------------------------------------------------------
BOT="${1:?bot}"
STATUS="${2:?status}"
TASK="${3:-}"
REPO="${4:-}"
LAST="${5:-}"

[ -f "$STATE" ] || { echo '{"updated":"1970-01-01T00:00:00Z","bots":{},"queue":[]}' > "$STATE"; }

# Exclusive lock prevents concurrent bot updates from corrupting state
_update_state() {
    local tmp
    tmp=$(safe_mktemp)
    jq --arg bot "$BOT" --arg status "$STATUS" --arg task "$TASK" --arg repo "$REPO" \
       --arg last "$LAST" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
      .updated = $ts
      | .bots[$bot] //= {"status":"idle","current_task":null,"current_repo":null,"last_completed":null}
      | .bots[$bot].status = $status
      | (if $task != "" then .bots[$bot].current_task = $task else . end)
      | (if $repo != "" then .bots[$bot].current_repo = $repo else . end)
      | (if $last != "" then .bots[$bot].last_completed = $last else . end)
    ' "$STATE" > "$tmp" && mv "$tmp" "$STATE" || { echo "fleet-state-update: failed to write $STATE" >&2; rm -f "$tmp"; return 1; }
}
with_lock "$STATE.lock" _update_state
