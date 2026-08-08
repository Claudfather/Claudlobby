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
#     Remove the PRUNING FLEET's own departed bot rows — never another fleet's.
#     fleet-state.json is host-shared: every fleet on the box writes this one
#     file, so scope is not a nicety. A row is removed only when this fleet no
#     longer declares it, NO fleet on the host declares it, and it is stamped as
#     this fleet's. Everything else is reported and left in place (#892).
#     --dry-run reports exactly what would go and writes nothing — this is what
#     the reconcile AUDIT path calls; a destructive write must never ride along
#     on a report-only verb.
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

    # Every bot declared by ANY fleet on this host. A row named here is live
    # somewhere, so it is never this fleet's to reap -- and this is the guard
    # that survives `claudlobby move-bot`, where a row's stamped .fleet still
    # says the OLD fleet while the manifest already says the new one. Attribution
    # alone would delete a bot that had just moved away.
    JQ_HOST=$(printf '%s\n' "$ATTR" | cut -f1 | sort -u | awk 'NF{printf "\"%s\": 1, ", $0}' | sed 's/, $//')

    # A row is this fleet's to remove ONLY when all three hold:
    #   1. this fleet no longer declares it   (the reason to prune at all)
    #   2. NO fleet on this host declares it  (not a sibling's live bot)
    #   3. it is stamped as ours              (a sibling's departed bot is theirs)
    # Anything else is reported and left alone. The file is host-global, so the
    # default has to be "not mine" -- a row with no .fleet stamp predates the
    # stamping and is kept, which makes the transition safe by construction
    # rather than by migration.
    _removable_rows() {
        jq -r --argjson keep "{${JQ_KEEP}}" --argjson host "{${JQ_HOST}}" --arg fleet "$THIS_FLEET" '
            .bots | to_entries[]
            | select(($keep[.key]  // 0) != 1)
            | select(($host[.key]  // 0) != 1)
            | select((.value.fleet // "")  == $fleet)
            | .key' "$STATE"
    }

    # Candidates this fleet is NOT allowed to remove, with the reason. Printed
    # whenever it is non-empty: "prune touched nothing" and "prune declined to
    # touch a sibling" are different facts and an operator needs to tell them
    # apart.
    _protected_rows() {
        jq -r --argjson keep "{${JQ_KEEP}}" --argjson host "{${JQ_HOST}}" --arg fleet "$THIS_FLEET" '
            .bots | to_entries[]
            | select(($keep[.key] // 0) != 1)
            | select(($host[.key] // 0) == 1 or (.value.fleet // "") != $fleet)
            | "      \(.key) — \(
                 if ($host[.key] // 0) == 1 then "declared by another fleet on this host"
                 elif (.value.fleet // "") == "" then "no fleet attribution (predates stamping)"
                 else "belongs to fleet \(.value.fleet)" end)"' "$STATE"
    }

    # Declared rows whose attribution is missing or stale. Backfilling these is
    # its OWN reason to write: a fleet with nothing to reap is the normal steady
    # state, so folding the backfill into the delete path would mean attribution
    # never converges for a healthy fleet -- and an unattributed row is one prune
    # can never act on later. Scoping that only converges on damaged fleets is
    # not converging.
    _unstamped_rows() {
        jq -r --argjson keep "{${JQ_KEEP}}" --arg fleet "$THIS_FLEET" '
            .bots | to_entries[]
            | select(($keep[.key] // 0) == 1)
            | select((.value.fleet // "") != $fleet)
            | .key' "$STATE"
    }

    _prune_state() {
        local tmp rows protected unstamped
        rows=$(_removable_rows)
        protected=$(_protected_rows)
        unstamped=$(_unstamped_rows)
        if [ -n "$protected" ]; then
            echo "prune: left $(_count "$protected") row(s) in place — this fleet does not own them:"
            printf '%s\n' "$protected"
        fi
        [ -z "$rows" ] && [ -z "$unstamped" ] && return 0
        if [ -n "$DRY_RUN" ]; then
            [ -n "$rows" ] && _prune_report "WOULD prune" "$rows"
            [ -n "$unstamped" ] && \
                echo "WOULD stamp $(_count "$unstamped") of this fleet's row(s) with its name:$(printf ' %s' $unstamped)"
            echo "  -> nothing was written. This is an audit; re-run with --enroll to apply."
            return 0
        fi
        tmp=$(safe_mktemp)
        # Delete BY THE EXPLICIT KEY LIST just reported, never by re-deriving the
        # predicate here. Two copies of a delete rule are two rules, and the one
        # that runs is not the one the operator read. Also backfill .fleet on the
        # rows this fleet declares, so attribution converges after one reconcile
        # per fleet instead of waiting on each bot's next write.
        jq --argjson keep "{${JQ_KEEP}}" \
           --argjson drop "$(printf '%s\n' "$rows" | jq -Rnc '[inputs | select(length > 0)]')" \
           --arg fleet "$THIS_FLEET" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
            .updated = $ts
            | .bots |= with_entries(if ($keep[.key] // 0) == 1 then .value.fleet = $fleet else . end)
            | reduce $drop[] as $b (.; del(.bots[$b]))
          ' "$STATE" > "$tmp" \
            && mv "$tmp" "$STATE" \
            || { echo "fleet-state-update: failed to write $STATE" >&2; rm -f "$tmp"; return 1; }
        [ -n "$rows" ] && _prune_report "Pruned" "$rows"
        [ -n "$unstamped" ] && \
            echo "Stamped $(_count "$unstamped") of this fleet's row(s) with its name:$(printf ' %s' $unstamped)"
        return 0
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
    # FLEET_NAME is composed into every bot.conf, so start-bot and report-back
    # carry it; an operator shell without it leaves the field untouched rather
    # than blanking it, because an unattributed row is protected from prune and
    # a WRONGLY attributed one is not.
    jq --arg bot "$BOT" --arg status "$STATUS" --arg task "$TASK" --arg repo "$REPO" \
       --arg last "$LAST" --arg fleet "${FLEET_NAME:-}" \
       --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
      .updated = $ts
      | .bots[$bot] //= {"status":"idle","current_task":null,"current_repo":null,"last_completed":null}
      | (if $fleet != "" then .bots[$bot].fleet = $fleet else . end)
      | .bots[$bot].status = $status
      | (if $task != "" then .bots[$bot].current_task = $task else . end)
      | (if $repo != "" then .bots[$bot].current_repo = $repo else . end)
      | (if $last != "" then .bots[$bot].last_completed = $last else . end)
    ' "$STATE" > "$tmp" && mv "$tmp" "$STATE" || { echo "fleet-state-update: failed to write $STATE" >&2; rm -f "$tmp"; return 1; }
}
with_lock "$STATE.lock" _update_state
