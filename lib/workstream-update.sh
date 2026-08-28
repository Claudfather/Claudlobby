#!/bin/bash
# workstream-update.sh — single-writer mutator for the per-fleet workstream
# registry (workstreams.json). The fleet tracks a bounded portfolio of work
# across unrelated repos here; stalls will surface via fleet-pulse reading this
# file (the pulse consumer lands in a follow-up PR).
#
# This helper is the ONLY writer. Hand-editing is forbidden; it will be wrapped
# by the /workstream manager skill and dispatch-task.sh --workstream (both
# follow-up PRs). Reads go through the read-only `claudlobby workstreams` CLI.
#
# Usage:
#   workstream-update.sh open <title> [--project P] [--owner BOT] [--next TEXT] [--id ws-<slug>]
#       Mint an active workstream. Prints the id. Fails if the active count is
#       already at the cap (WORKSTREAM_MAX_ACTIVE).
#   workstream-update.sh progress <id> [--next TEXT]
#       Record real progress: advances last_progress_ts and extends the lease.
#   workstream-update.sh renew <id> --note TEXT
#       Extend the lease WITHOUT crediting progress (requires a note; logged to
#       renewals[]). Deliberately does not touch last_progress_ts so that
#       repeated renew-without-progress stays visible to the stall check.
#   workstream-update.sh block <id> [--note TEXT]
#       Mark blocked (drops out of the active cap).
#   workstream-update.sh close <id> [--status done|abandoned]
#       Terminal close (default done). Stamps closed_ts.
#   workstream-update.sh prune [--archive <path>]
#       Move terminal (done|abandoned) entries to the append-only archive
#       (workstreams-archive.jsonl). Rides the weekly data-sweep.
#
# Residence: per-fleet, resolved like report-back.jsonl — overlay
#   local/<fleet>/runtime/workstreams.json, root-mode runtime/fleet/workstreams.json.
#   Overridable via WORKSTREAMS_PATH (tests point it at a scratch file).
#
# Locking mirrors fleet-state-update.sh: with_lock (flock or mkdir spinlock) +
# safe_mktemp + temp-then-mv atomic write.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

# --- observable-plane dual-write (PR-B T6b; verb table per phase-2 plan §3) ----
# Dormant behind PLANE_EMIT_ENABLED=1 (fleet env); PLANE_EMIT_DISABLED=1 wins;
# disclosed, never blocking — workstreams.json stays the load-bearing registry.
# Verb map (F21): open -> the workstream CONSTRUCT (the row IS the opening;
# no `opened` token exists) · progress -> progressed · renew -> renewed +
# renewed_until · block -> blocked · close -> closed + disposition ·
# prune -> archived per pruned id. Emits run INSIDE the locked verb functions
# so they carry exactly the values the registry write applied.
PLANE_ARMED=0
if plane_armed workstream-update --require-fleet; then
    PLANE_ARMED=1
fi
_plane_actor() { printf 'bot:%s/%s' "$FLEET_NAME" "${BOT_NAME:-operator}"; }
# _plane_ws_event_obj <id> <event> [extra-json-fragment-with-leading-comma]
# One workstream event OBJECT on stdout — prune batches N of these into one
# atomic emission (gauntlet round: the per-id loop paid a shim spawn per
# pruned id, serialized inside the registry lock).
_plane_ws_event_obj() {
    printf '{"event_type":"workstream_event","emitter":"workstream-update","source_ref":"workstreams:%s","fleet":"%s","payload":{"workstream_id":"%s","event":"%s","actor":"%s"%s}}' \
        "$(json_escape "$1")" "$(json_escape "$FLEET_NAME")" \
        "$(json_escape "$1")" "$2" "$(json_escape "$(_plane_actor)")" "${3:-}"
}
# _plane_ws_event <id> <event> [extra-json-fragment-with-leading-comma]
_plane_ws_event() {
    [ "$PLANE_ARMED" = "1" ] || return 0
    printf '{"events":[%s]}' "$(_plane_ws_event_obj "$1" "$2" "${3:-}")" \
        | plane_emit_events workstream-update || true
}
install_error_trap ""

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"

# --- Registry path resolution (mirrors report-back.sh) ------------------------
_resolve_registry() {
    if [ -n "${WORKSTREAMS_PATH:-}" ]; then
        printf '%s' "$WORKSTREAMS_PATH"
        return 0
    fi
    printf '%s' "$(fleet_runtime_dir)/workstreams.json"
}

REGISTRY="$(_resolve_registry)"
mkdir -p "$(dirname "$REGISTRY")"

LEASE_DAYS="${WORKSTREAM_LEASE_DAYS:-14}"
MAX_ACTIVE="${WORKSTREAM_MAX_ACTIVE:-12}"

# Fail fast on a bad env: a non-numeric MAX_ACTIVE makes `[ n -ge $MAX ]` error
# and silently skip the cap (fails open, unbounded); a non-numeric/negative
# LEASE_DAYS aborts the lease-epoch arithmetic under set -u but set -e does not
# propagate out of the `$(...)` assignment, so an entry lands with an empty or
# past lease. The single writer must defend its own invariants, not trust the
# composed value blindly.
_require_pos_int() {
    # _require_pos_int <name> <value> — validated below, once _die is defined.
    case "$2" in
        ''|*[!0-9]*) _die "$1 must be a positive integer, got '$2'" ;;
    esac
    [ "$2" -ge 1 ] || _die "$1 must be >= 1, got '$2'"
}

_now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Lease expiry = now + LEASE_DAYS. GNU date and BSD date differ on relative-date
# syntax; compute the epoch arithmetically so the same code runs on Linux + macOS.
_lease_expiry_iso() {
    local now_epoch secs
    now_epoch=$(date -u +%s)
    secs=$(( LEASE_DAYS * 86400 ))
    epoch_to_iso_utc $(( now_epoch + secs ))
}

# epoch->ISO lives in lib-common (epoch_to_iso_utc) — the third private copy
# was promoted in the gauntlet round.

_slugify() {
    # Lowercase, non-alnum runs -> single dash, trim leading/trailing dashes.
    printf '%s' "$1" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -e 's/[^a-z0-9]\{1,\}/-/g' -e 's/^-*//' -e 's/-*$//'
}

_init_registry() {
    [ -f "$REGISTRY" ] || printf '%s\n' '{"updated":"1970-01-01T00:00:00Z","workstreams":{}}' > "$REGISTRY"
}

_registry_has() {
    # _registry_has <id> -> 0 if the id exists
    _init_registry
    [ "$(jq -r --arg id "$1" '.workstreams | has($id)' "$REGISTRY")" = "true" ]
}

# Atomic jq transform under the registry lock:
#   _apply <now-iso> <jq-program> [--arg k v ...]
# Runs the program, stamps .updated with the caller's timestamp (one instant per
# mutation — no second `date` fork, no skew vs the entry's own *_ts fields), then
# writes via temp-then-mv. Assumes the caller already holds the registry lock.
_apply() {
    local now="$1" program="$2"; shift 2
    local tmp
    tmp=$(safe_mktemp)
    jq "$@" --arg _ts "$now" "$program"' | .updated = $_ts' "$REGISTRY" > "$tmp" \
        && mv "$tmp" "$REGISTRY" \
        || { echo "workstream-update: failed to write $REGISTRY" >&2; rm -f "$tmp"; return 1; }
}

_die() { echo "workstream-update: $1" >&2; exit "${2:-2}"; }

# _require_exists <cmd> — inside a locked mutator, fail (return 1) if the
# ambient $ID is absent. Runs under the lock so the check-then-act is atomic:
# a pre-lock check would let a concurrent prune delete the entry before the
# write, and jq would then auto-vivify a partial zombie.
_require_exists() {
    _registry_has "$ID" && return 0
    echo "workstream-update: $1: no such workstream: $ID" >&2
    return 1
}

# Validate the bounds now that _die exists (fail fast, before any mutation).
_require_pos_int WORKSTREAM_MAX_ACTIVE "$MAX_ACTIVE"
_require_pos_int WORKSTREAM_LEASE_DAYS "$LEASE_DAYS"

# --- Subcommand dispatch ------------------------------------------------------
CMD="${1:-}"
[ -n "$CMD" ] || _die "usage: workstream-update.sh <open|progress|renew|block|close|prune> ..."
shift

case "$CMD" in
open)
    TITLE=""; PROJECT=""; OWNER=""; NEXT=""; ID_EXPLICIT=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --project) PROJECT="${2:-}"; shift 2 ;;
            --owner)   OWNER="${2:-}"; shift 2 ;;
            --next)    NEXT="${2:-}"; shift 2 ;;
            --id)      ID_EXPLICIT="${2:-}"; shift 2 ;;
            -*)        _die "open: unknown flag: $1" ;;
            *)         if [ -z "$TITLE" ]; then TITLE="$1"; else _die "open: unexpected arg: $1"; fi; shift ;;
        esac
    done
    [ -n "$TITLE" ] || _die "open: <title> is required"
    if [ -z "$ID_EXPLICIT" ]; then
        _slug_base="ws-$(_slugify "$TITLE")"
        [ "$_slug_base" != "ws-" ] || _die "open: title has no slug-able characters; pass --id"
    fi

    # De-dup, cap-check, and insert ALL run inside the lock: the id mint is the
    # single-writer guarantee this helper exists to provide — two concurrent
    # opens must not mint the same id or overrun the cap. Prints the chosen id
    # on success (with_lock inherits stdout), nothing on the error paths.
    _open_ws() {
        _init_registry
        local id
        if [ -n "$ID_EXPLICIT" ]; then
            _registry_has "$ID_EXPLICIT" && {
                echo "workstream-update: open: id '$ID_EXPLICIT' already exists" >&2
                return 2
            }
            id="$ID_EXPLICIT"
        else
            local n=2
            id="$_slug_base"
            while _registry_has "$id"; do id="$_slug_base-$n"; n=$(( n + 1 )); done
        fi

        local active_count
        active_count=$(jq -r '[.workstreams[] | select(.status=="active")] | length' "$REGISTRY")
        if [ "$active_count" -ge "$MAX_ACTIVE" ]; then
            {
                echo "workstream-update: active workstreams at cap ($MAX_ACTIVE)."
                echo "  Raise fleet.workstreams.max_active, or close one first. Oldest active:"
                jq -r '[.workstreams[] | select(.status=="active")]
                    | sort_by(.opened_ts) | .[0:3] | .[] | "    \(.id)  (\(.title))"' "$REGISTRY"
            } >&2
            return 3
        fi

        local now expiry
        now="$(_now_iso)"; expiry="$(_lease_expiry_iso)"
        _apply "$now" '.workstreams[$id] = {
              id: $id, fleet: $fleet, title: $title,
              project: (if $project == "" then null else $project end),
              status: "active",
              owner_bot: (if $owner == "" then null else $owner end),
              next: (if $next == "" then null else $next end),
              task_ids: [], refs: {issues: [], prs: []},
              opened_ts: $now, last_progress_ts: $now,
              lease_expires_ts: $expiry, renewals: []
            }' \
            --arg id "$id" --arg fleet "${FLEET_NAME:-}" --arg title "$TITLE" \
            --arg project "$PROJECT" --arg owner "$OWNER" --arg next "$NEXT" \
            --arg now "$now" --arg expiry "$expiry" || return 1
        if [ "$PLANE_ARMED" = "1" ]; then
            local owner_frag="" proj_frag=""
            [ -n "$OWNER" ] && owner_frag=",\"owner\":\"$(json_escape "bot:$FLEET_NAME/$OWNER")\""
            case "$PROJECT" in
                [a-z]*) proj_frag=",\"project_key\":\"$(json_escape "$PROJECT")\"" ;;
            esac
            printf '{"events":[{"event_type":"workstream","emitter":"workstream-update","source_ref":"workstreams:%s","fleet":"%s","payload":{"workstream_id":"%s","title":"%s","opened_by":"%s"%s%s}}]}' \
                "$(json_escape "$id")" "$(json_escape "$FLEET_NAME")" \
                "$(json_escape "$id")" "$(json_escape "$TITLE")" \
                "$(json_escape "$(_plane_actor)")" "$owner_frag" "$proj_frag" \
                | plane_emit_events workstream-update || true
        fi
        printf '%s\n' "$id"
    }
    with_lock "$REGISTRY.lock" _open_ws || exit $?
    ;;

progress)
    ID="${1:-}"; [ -n "$ID" ] || _die "progress: <id> is required"; shift
    NEXT=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --next) NEXT="${2:-}"; shift 2 ;;
            *) _die "progress: unknown arg: $1" ;;
        esac
    done
    # Existence check runs INSIDE the lock (as open does): a pre-lock check
    # would be check-then-act — a concurrent prune could delete the entry
    # between the check and the write, and jq's `.workstreams[$id].x = y`
    # would then auto-vivify a partial zombie entry (no id/status/title).
    _progress_ws() {
        _require_exists progress || return 1
        local now expiry
        now="$(_now_iso)"; expiry="$(_lease_expiry_iso)"
        _apply "$now" '.workstreams[$id].last_progress_ts = $now
                | .workstreams[$id].lease_expires_ts = $expiry
                | (if $next != "" then .workstreams[$id].next = $next else . end)' \
            --arg id "$ID" --arg now "$now" --arg expiry "$expiry" --arg next "$NEXT" \
            || return 1
        local frag=""
        [ -n "$NEXT" ] && frag=",\"next_step\":\"$(json_escape "$NEXT")\""
        _plane_ws_event "$ID" "progressed" "$frag"
    }
    with_lock "$REGISTRY.lock" _progress_ws || exit $?
    ;;

renew)
    ID="${1:-}"; [ -n "$ID" ] || _die "renew: <id> is required"; shift
    NOTE=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --note) NOTE="${2:-}"; shift 2 ;;
            *) _die "renew: unknown arg: $1" ;;
        esac
    done
    [ -n "$NOTE" ] || _die "renew: --note is required (renew without progress must be justified)"
    # Note: deliberately does NOT advance last_progress_ts — the stall check
    # keys on that, so serial renew-without-progress stays visible. Existence
    # checked inside the lock (see progress) to avoid the auto-vivify race.
    _renew_ws() {
        _require_exists renew || return 1
        local now expiry
        now="$(_now_iso)"; expiry="$(_lease_expiry_iso)"
        _apply "$now" '.workstreams[$id].lease_expires_ts = $expiry
                | .workstreams[$id].renewals += [{ts: $now, note: $note}]' \
            --arg id "$ID" --arg now "$now" --arg expiry "$expiry" --arg note "$NOTE" \
            || return 1
        _plane_ws_event "$ID" "renewed" \
            ",\"renewed_until\":\"$(json_escape "$expiry")\",\"note\":\"$(json_escape "$NOTE")\""
    }
    with_lock "$REGISTRY.lock" _renew_ws || exit $?
    ;;

block)
    ID="${1:-}"; [ -n "$ID" ] || _die "block: <id> is required"; shift
    NOTE=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --note) NOTE="${2:-}"; shift 2 ;;
            *) _die "block: unknown arg: $1" ;;
        esac
    done
    # Existence checked inside the lock (see progress) to avoid the auto-vivify race.
    _block_ws() {
        _require_exists block || return 1
        _apply "$(_now_iso)" '.workstreams[$id].status = "blocked"
                | (if $note != "" then .workstreams[$id].next = $note else . end)' \
            --arg id "$ID" --arg note "$NOTE" || return 1
        local frag=""
        [ -n "$NOTE" ] && frag=",\"note\":\"$(json_escape "$NOTE")\""
        _plane_ws_event "$ID" "blocked" "$frag"
    }
    with_lock "$REGISTRY.lock" _block_ws || exit $?
    ;;

close)
    ID="${1:-}"; [ -n "$ID" ] || _die "close: <id> is required"; shift
    STATUS="done"
    while [ $# -gt 0 ]; do
        case "$1" in
            --status) STATUS="${2:-}"; shift 2 ;;
            *) _die "close: unknown arg: $1" ;;
        esac
    done
    case "$STATUS" in done|abandoned) ;; *) _die "close: --status must be done|abandoned, got '$STATUS'" ;; esac
    # Existence checked inside the lock (see progress) to avoid the auto-vivify race.
    _close_ws() {
        _require_exists close || return 1
        local now; now="$(_now_iso)"
        _apply "$now" '.workstreams[$id].status = $status
                | .workstreams[$id].closed_ts = $now' \
            --arg id "$ID" --arg status "$STATUS" --arg now "$now" || return 1
        _plane_ws_event "$ID" "closed" ",\"disposition\":\"$STATUS\""
    }
    with_lock "$REGISTRY.lock" _close_ws || exit $?
    ;;

prune)
    ARCHIVE=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --archive) ARCHIVE="${2:-}"; shift 2 ;;
            *) _die "prune: unknown arg: $1" ;;
        esac
    done
    [ -n "$ARCHIVE" ] || ARCHIVE="$(dirname "$REGISTRY")/workstreams-archive.jsonl"
    _prune_ws() {
        _init_registry
        local terminal tmp
        terminal=$(jq -r '[.workstreams[] | select(.status=="done" or .status=="abandoned")] | length' "$REGISTRY")
        [ "$terminal" -gt 0 ] || return 0
        # Archive-then-drop, never the reverse: a crash between the two steps
        # must duplicate an audit row, never lose a terminal entry. Materialize
        # the terminal entries into a temp first so a crash mid-write cannot
        # leave a truncated line in the append-only archive; append the complete
        # temp in one shot, then drop from the live registry. A crash strictly
        # between the append and the drop re-archives on the next run — an
        # at-most-once-extra audit row, deduped by id at read time.
        tmp=$(safe_mktemp)
        jq -c '.workstreams[] | select(.status=="done" or .status=="abandoned")' "$REGISTRY" > "$tmp" \
            || { rm -f "$tmp"; echo "workstream-update: prune: failed to collect terminal entries" >&2; return 1; }
        cat "$tmp" >> "$ARCHIVE" \
            || { rm -f "$tmp"; echo "workstream-update: prune: failed to append $ARCHIVE" >&2; return 1; }
        # Retain the pruned ids; emit archived only AFTER the registry drop
        # succeeds (#1372 review F13): emitting first recorded `archived` for
        # a prune that then failed, and the retry double-emitted.
        local _pruned_ids
        # Disclosed, never swallowed (gauntlet round): this was the one
        # silent failure in the five door blocks — a jq failure dropped
        # every archived emit while the prune proceeded.
        if ! _pruned_ids="$(jq -r '.id // empty' "$tmp" 2>/dev/null)"; then
            _pruned_ids=""
            echo "workstream-update: prune: could not extract pruned ids — archived events not emitted (registry drop stands)" >&2
        fi
        rm -f "$tmp"
        _apply "$(_now_iso)" \
            '.workstreams |= with_entries(select(.value.status != "done" and .value.status != "abandoned"))' \
            || return 1
        # ONE atomic batch, emitted AFTER the lock releases (gauntlet round):
        # the per-id loop paid a shim invocation per pruned id INSIDE the
        # registry lock — daemon-down, each fallback is a full CLI spawn
        # (seconds on a Pi), so a large prune serialized every other
        # workstream writer behind plane fallbacks for minutes; and a crash
        # mid-loop left a permanently half-archived plane record. The batch
        # is built here (values captured under the lock) and handed out via
        # a FILE, never a shell variable: with_lock runs its command in a
        # SUBSHELL on flock-capable hosts (the Pi), so a global set in here
        # is lost there — a platform-split silent drop.
        if [ "$PLANE_ARMED" = "1" ] && [ -n "$PLANE_PRUNE_BATCH_FILE" ]; then
            local _pid _ev _batch=""
            while IFS= read -r _pid; do
                [ -n "$_pid" ] || continue
                _ev="$(_plane_ws_event_obj "$_pid" "archived")"
                _batch="${_batch:+$_batch,}$_ev"
            done <<< "$_pruned_ids"
            printf '%s' "$_batch" > "$PLANE_PRUNE_BATCH_FILE"
        fi
        echo "Pruned $terminal terminal workstream(s) to $ARCHIVE"
    }
    PLANE_PRUNE_BATCH_FILE=""
    [ "$PLANE_ARMED" = "1" ] && PLANE_PRUNE_BATCH_FILE="$(safe_mktemp)"
    with_lock "$REGISTRY.lock" _prune_ws || { _prc=$?; rm -f "$PLANE_PRUNE_BATCH_FILE"; exit "$_prc"; }
    if [ -n "$PLANE_PRUNE_BATCH_FILE" ] && [ -s "$PLANE_PRUNE_BATCH_FILE" ]; then
        printf '{"events":[%s]}' "$(cat "$PLANE_PRUNE_BATCH_FILE")" \
            | plane_emit_events workstream-update || true
    fi
    rm -f "$PLANE_PRUNE_BATCH_FILE"
    ;;

*)
    _die "unknown subcommand: $CMD (expected open|progress|renew|block|close|prune)"
    ;;
esac
