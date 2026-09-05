#!/bin/bash
# workstream-update.sh — single-writer mutator for the per-fleet workstream
# registry, which lives on the PLANE (the workstreams / workstream_event
# families; F18 closure R1 — nothing on disk). The fleet tracks a bounded
# portfolio of work across unrelated repos here; stalls surface via fleet-pulse
# and brief reading the same registry from the plane.
#
# This helper is the ONLY writer. It is wrapped by the /workstream manager
# skill and dispatch-task.sh --workstream. Reads go through the read-only
# `claudlobby workstreams` CLI, which renders the same registry from the plane.
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
#   workstream-update.sh prune
#       Drop terminal (done|abandoned) entries from the live registry; their
#       `archived` events on the plane ARE the archive. Rides the weekly data-sweep.
#
# Residence: the plane, per fleet (FLEET_NAME). Every verb works on a registry
#   MATERIALIZED from the plane (plane-lookup.py --workstreams) into a temp
#   file — the same jq programs, one lock — and the verb's plane event IS the
#   write. A plane that cannot serve the registry is a refusal (rc 3), never a
#   stale file; an emission the shim could not record is a refusal too (rc 4):
#   the verb did not happen, and there is no file to fall back to.
#
# Locking mirrors fleet-state-update.sh: with_lock (flock or mkdir spinlock) on
# <fleet runtime>/workstreams.lock + safe_mktemp + temp-then-mv on the working copy.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

# --- the plane IS the registry (PR-B T6b; verb table per phase-2 plan §3) -----
# PLANE_EMIT_DISABLED=1 (the harness exemption) is the one thing that silences
# the plane — and with no file behind it, a silenced door has no registry and
# refuses (rc 3) rather than pretend.
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
# _plane_ws_event_obj <id> <event> [extra-json-fragment] [occurred-at]: the
# fourth argument is the VERB's instant — the same `now` the registry gets —
# so the plane row and the file agree to the second (the emit's own stamp can
# fall a second later; the A2 parity pin flaked 1 in 3 on exactly that).
_plane_ws_event_obj() {
    local _at_frag=""
    [ -n "${4:-}" ] && _at_frag=",\"occurred_at\":\"$(json_escape "$4")\""
    printf '{"event_type":"workstream_event","emitter":"workstream-update","source_ref":"workstreams:%s","fleet":"%s"%s,"payload":{"workstream_id":"%s","event":"%s","actor":"%s"%s}}' \
        "$(json_escape "$1")" "$(json_escape "$FLEET_NAME")" "$_at_frag" \
        "$(json_escape "$1")" "$2" "$(json_escape "$(_plane_actor)")" "${3:-}"
}
# _plane_ws_event <id> <event> [extra-json-fragment-with-leading-comma]
_plane_ws_event() {
    # a here-string, never a pipeline: the wrapper must run in THIS shell so its
    # result (PLANE_EMIT_LAST_RC) is visible to the refusal
    plane_emit_events workstream-update <<<"{\"events\":[$(_plane_ws_event_obj "$1" "$2" "${3:-}" "${4:-}")]}" || true
    _ws_require_recorded
}
install_error_trap ""

_die() { echo "workstream-update: $1" >&2; exit "${2:-2}"; }

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"

# --- the registry, materialized from the plane ---------------------------------
[ "$PLANE_ARMED" = "1" ] || _die "the plane is the only registry and it is silenced here (PLANE_EMIT_DISABLED=1, or no FLEET_NAME) -- nothing to work on" 3
# The root must EXIST before anything here creates a path under it (the lock
# dir does): a root that is not a directory is unreachable, never an empty
# registry minted at a wrong root.
[ -d "${CLAUDLOBBY_ROOT:-}" ] || _die "CLAUDLOBBY_ROOT '${CLAUDLOBBY_ROOT:-}' is not a directory -- the plane could not serve the registry for ${FLEET_NAME:-?}; nothing changed" 3
REGISTRY="$(safe_mktemp)"
_WS_MATERIALIZED=0
# Rendered INSIDE the verb's lock (through _init_registry, once per process),
# never at startup: two concurrent opens must dedup and cap-check against the
# plane as it is when their turn comes, not a snapshot from before the wait
# (found by the R1 gauntlet). --or-empty: a fleet the plane holds no identity
# for yet (its first open is this very call) starts from the empty registry;
# an unreachable plane refuses. The writer's render carries the archived ids.
_ws_materialize() {
    [ "$_WS_MATERIALIZED" = "1" ] && return 0
    if ! python3 -S -E "$LIB_DIR/plane-lookup.py" --root "${CLAUDLOBBY_ROOT:-}" --workstreams --or-empty \
            --fleet "$FLEET_NAME" --lease-days "$LEASE_DAYS" > "$REGISTRY" 2>/dev/null \
            || [ ! -s "$REGISTRY" ]; then
        _die "the plane could not serve the registry for $FLEET_NAME (plane-lookup.py --workstreams) -- nothing changed" 3
    fi
    _WS_MATERIALIZED=1
}
_ws_require_recorded() {
    # after a verb's emit: the shim's rc is the verdict, and there is no file
    [ "${PLANE_EMIT_LAST_RC:-1}" -eq 0 ] \
        || _die "the plane did not record this verb (rc=$PLANE_EMIT_LAST_RC) -- nothing changed" 4
}

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
    # the plane's registry, rendered on first use inside the caller's lock
    _ws_materialize
}
# One lock name per fleet regardless of the working copy: every local writer
# serializes on it (the directory is created on first use — nothing else
# writes there any more).
_ws_lock() { local _d; _d="$(fleet_runtime_dir)"; mkdir -p "$_d" 2>/dev/null || true; printf '%s' "$_d/workstreams.lock"; }

_registry_has() {
    # _registry_has <id> -> 0 if the id exists — live OR archived: a construct
    # id is unique per fleet on the plane, so a pruned id is never re-minted
    _init_registry
    [ "$(jq -r --arg id "$1" '(.workstreams | has($id)) or (((.archived // []) | index($id)) != null)' "$REGISTRY")" = "true" ]
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
            # the opening --next is the construct's goal: the stated next step at
            # opening, which the plane renders as `next` until a progress replaces it
            [ -n "$NEXT" ] && owner_frag="$owner_frag,\"goal\":\"$(json_escape "$NEXT")\""
            case "$PROJECT" in
                [a-z]*) proj_frag=",\"project_key\":\"$(json_escape "$PROJECT")\"" ;;
            esac
            local _open_batch
            printf -v _open_batch '{"events":[{"event_type":"workstream","emitter":"workstream-update","source_ref":"workstreams:%s","fleet":"%s","occurred_at":"%s","payload":{"workstream_id":"%s","title":"%s","opened_by":"%s"%s%s}}]}' \
                "$(json_escape "$id")" "$(json_escape "$FLEET_NAME")" "$now" \
                "$(json_escape "$id")" "$(json_escape "$TITLE")" \
                "$(json_escape "$(_plane_actor)")" "$owner_frag" "$proj_frag"
            plane_emit_events workstream-update <<<"$_open_batch" || true
            _ws_require_recorded
        fi
        printf '%s\n' "$id"
    }
    with_lock "$(_ws_lock)" _open_ws || exit $?
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
        _plane_ws_event "$ID" "progressed" "$frag" "$now"
    }
    with_lock "$(_ws_lock)" _progress_ws || exit $?
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
            ",\"renewed_until\":\"$(json_escape "$expiry")\",\"note\":\"$(json_escape "$NOTE")\"" "$now"
    }
    with_lock "$(_ws_lock)" _renew_ws || exit $?
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
        local now; now="$(_now_iso)"
        _apply "$now" '.workstreams[$id].status = "blocked"
                | (if $note != "" then .workstreams[$id].next = $note else . end)' \
            --arg id "$ID" --arg note "$NOTE" || return 1
        local frag=""
        [ -n "$NOTE" ] && frag=",\"note\":\"$(json_escape "$NOTE")\""
        _plane_ws_event "$ID" "blocked" "$frag" "$now"
    }
    with_lock "$(_ws_lock)" _block_ws || exit $?
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
        _plane_ws_event "$ID" "closed" ",\"disposition\":\"$STATUS\"" "$now"
    }
    with_lock "$(_ws_lock)" _close_ws || exit $?
    ;;

prune)
    [ $# -eq 0 ] || _die "prune: unknown arg: $1"
    _prune_ws() {
        _init_registry
        local terminal tmp
        terminal=$(jq -r '[.workstreams[] | select(.status=="done" or .status=="abandoned")] | length' "$REGISTRY")
        [ "$terminal" -gt 0 ] || return 0
        # The `archived` events on the plane ARE the archive (one per pruned id,
        # emitted after the drop succeeds, below). Collect the terminal entries
        # into a temp first: their ids are what the batch names.
        tmp=$(safe_mktemp)
        jq -c '.workstreams[] | select(.status=="done" or .status=="abandoned")' "$REGISTRY" > "$tmp" \
            || { rm -f "$tmp"; echo "workstream-update: prune: failed to collect terminal entries" >&2; return 1; }
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
        local _prune_now; _prune_now="$(_now_iso)"
        _apply "$_prune_now" \
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
                _ev="$(_plane_ws_event_obj "$_pid" "archived" "" "$_prune_now")"
                _batch="${_batch:+$_batch,}$_ev"
            done <<< "$_pruned_ids"
            printf '%s' "$_batch" > "$PLANE_PRUNE_BATCH_FILE"
        fi
        echo "Pruned $terminal terminal workstream(s) -- archived on the plane"
    }
    PLANE_PRUNE_BATCH_FILE=""
    [ "$PLANE_ARMED" = "1" ] && PLANE_PRUNE_BATCH_FILE="$(safe_mktemp)"
    with_lock "$(_ws_lock)" _prune_ws || { _prc=$?; rm -f "$PLANE_PRUNE_BATCH_FILE"; exit "$_prc"; }
    if [ -n "$PLANE_PRUNE_BATCH_FILE" ] && [ -s "$PLANE_PRUNE_BATCH_FILE" ]; then
        plane_emit_events workstream-update <<<"{\"events\":[$(cat "$PLANE_PRUNE_BATCH_FILE")]}" || true
        _ws_require_recorded
    fi
    rm -f "$PLANE_PRUNE_BATCH_FILE"
    ;;

*)
    _die "unknown subcommand: $CMD (expected open|progress|renew|block|close|prune)"
    ;;
esac
