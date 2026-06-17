#!/usr/bin/env bash
# code-audit-sweep.sh — No-LLM rolling code-audit selector.
#
# Runs as a fleet timer (no LLM). For each configured repo it asks GitHub for
# the most recent `auto-audit`-labelled issue and picks the stalest repo (the
# one whose newest audit issue is oldest, or which has never been audited).
# It then hands the audit to the owner bot's live Claude session via the
# existing bot-sweep-cron.sh dispatcher.
#
# Staleness is read live from GitHub every run — the labelled issues ARE the
# ledger. There is no local tracker, so there is no select->run->log race and
# no drift: an audit's own filed issues make its repo "fresh" for the next run.
#
# Config is read from the owner bot's bot.conf (emitted by `claudlobby
# generate` from the fleet.yaml `sweep:` block):
#   SWEEP_OWNER_BOT   the bot whose session runs the audit (also marks the owner)
#   SWEEP_REPOS       space-separated org/repo list (compose-time resolved,
#                     defaulting to the owner's scope.repos)
#   SWEEP_LABEL       staleness label (default: auto-audit)
#   SWEEP_AUDIT_TYPES space-separated clauDNA audit types, rotated per run
#
# Usage: code-audit-sweep.sh <fleet-name>

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

fleet="${1:?Usage: code-audit-sweep.sh <fleet-name>}"

BOTS_DIR=$(resolve_bots_dir "$fleet")
if [ ! -d "$BOTS_DIR" ]; then
    echo "code-audit-sweep: bots directory not found: $BOTS_DIR" >&2
    exit 1
fi

install_error_trap ""

today=$(date +%Y-%m-%d)
ts=$(ts_iso)

# --- Helper: emit one audit event to the owner bot's event log ---
# Mirrors fleet-pulse.sh's emit_event but stamps source:"audit". data_json is a
# raw JSON object fragment, interpolated unescaped.
emit_event() {
    local bot_dir="$1" bot_id="$2" event_type="$3" data_json="$4"
    local events_dir="$bot_dir/data/events"
    mkdir -p "$events_dir"
    printf '{"ts":"%s","bot":"%s","type":"%s","source":"audit","data":%s}\n' \
        "$ts" "$bot_id" "$event_type" "$data_json" \
        >> "$events_dir/fleet-${today}.jsonl"
}

# --- Helper: read a bot.conf value the way it was written ---
# The compositor emits shell-quoted values (e.g. single-quoted SWEEP_REPOS
# lists), but bot_conf_get strips only double quotes. Sourcing in a subshell is
# the faithful reader for multi-word values; bot_conf_get stays fine for the
# single-word owner scan below.
_conf_get() {
    local dir="$1" var="$2" default="$3"
    ( . "$dir/bot.conf" >/dev/null 2>&1 || true; printf '%s' "${!var:-$default}" )
}

# --- Resolve the owner bot ---
# The compositor writes SWEEP_OWNER_BOT only into the owner bot's bot.conf, so
# the single bot declaring it is the owner.
OWNER_BOT=""
OWNER_DIR=""
for _dir in "$BOTS_DIR"/*/; do
    [ -d "$_dir" ] || continue
    _owner=$(bot_conf_get "${_dir%/}" SWEEP_OWNER_BOT "")
    if [ -n "$_owner" ]; then
        OWNER_BOT="$_owner"
        OWNER_DIR="${_dir%/}"
        break
    fi
done
if [ -z "$OWNER_BOT" ] || [ ! -d "$OWNER_DIR" ]; then
    echo "code-audit-sweep: no sweep owner found (no bot.conf declares SWEEP_OWNER_BOT) — is the fleet.yaml sweep: block enabled and generated?" >&2
    exit 1
fi

LABEL=$(_conf_get "$OWNER_DIR" SWEEP_LABEL "auto-audit")
REPOS=$(_conf_get "$OWNER_DIR" SWEEP_REPOS "")
AUDIT_TYPES=$(_conf_get "$OWNER_DIR" SWEEP_AUDIT_TYPES "tech-debt")

if [ -z "$REPOS" ]; then
    echo "code-audit-sweep: SWEEP_REPOS empty for owner '$OWNER_BOT' and no scope fallback resolved at compose time" >&2
    exit 1
fi

# Audit type for this run: rotate deterministically over the configured types
# by day-of-year, so every repo eventually sees every type without any stored
# counter.
# shellcheck disable=SC2206
_types=($AUDIT_TYPES)
_doy=$(date +%j)
# Strip a leading zero so day 045 doesn't get read as octal in arithmetic.
_doy=$((10#$_doy))
AUDIT_TYPE="${_types[$(( _doy % ${#_types[@]} ))]}"

# --- Staleness query (live GitHub read) ---
# ISO 8601 timestamps sort lexicographically in chronological order, so the
# stalest repo is the one with the smallest "newest audit issue" timestamp.
# Never-audited repos return NONE, which we map to the empty string — sorting
# before any real timestamp, i.e. maximally stale. Ties break on config order
# (first wins) because we only replace the leader on a strictly-smaller key.
STALEST_REPO=""
STALEST_KEY=""        # ranking key; "" = never audited = oldest (sorts first)
REACHABLE=0

for repo in $REPOS; do
    if last=$(with_timeout 60 gh issue list --repo "$repo" --label "$LABEL" \
            --state all --limit 200 --json createdAt \
            -q 'if length>0 then max_by(.createdAt).createdAt else "NONE" end' 2>/dev/null); then
        REACHABLE=$((REACHABLE + 1))
    else
        # Auth/network/timeout: never guess "fresh" or "stale" (no-fabrication).
        emit_event "$OWNER_DIR" "$OWNER_BOT" "sweep_repo_unreachable" \
            '{"repo":"'"$repo"'","label":"'"$LABEL"'"}'
        continue
    fi

    if [ "$last" = "NONE" ]; then
        key=""           # never audited → maximally stale
    else
        key="$last"
    fi

    # Stalest = smallest key. "" (never-audited) sorts before any ISO timestamp,
    # and an equal key keeps the incumbent — preserving the config-order tiebreak.
    if [ -z "$STALEST_REPO" ] || [[ "$key" < "$STALEST_KEY" ]]; then
        STALEST_REPO="$repo"
        STALEST_KEY="$key"
    fi
done

if [ "$REACHABLE" -eq 0 ]; then
    echo "code-audit-sweep: all ${REPOS} repos unreachable via gh — aborting (error trap emits script_error)" >&2
    exit 1
fi

# Human-facing last-audit value for the event payload: the ISO timestamp, or
# NONE when the selected repo has never been audited.
STALEST_LAST="${STALEST_KEY:-NONE}"

# Staleness in days for the event payload (informational; ranking above does
# not depend on it). null when the timestamp can't be parsed or never audited.
staleness_days() {
    local iso="$1" then_epoch now
    if [ -z "$iso" ] || [ "$iso" = "NONE" ]; then printf 'null'; return; fi
    then_epoch=$(date -d "$iso" +%s 2>/dev/null || echo "")
    [ -z "$then_epoch" ] && { printf 'null'; return; }
    now=$(date +%s)
    printf '%d' $(( (now - then_epoch) / 86400 ))
}
DAYS=$(staleness_days "$STALEST_LAST")

emit_event "$OWNER_DIR" "$OWNER_BOT" "audit_selected" \
    '{"repo":"'"$STALEST_REPO"'","last_audit":"'"$STALEST_LAST"'","staleness_days":'"$DAYS"',"audit_type":"'"$AUDIT_TYPE"'"}'

# --- Dispatch to the owner's session via the shared dispatcher ---
# bot-sweep-cron.sh owns sanitize + the busy-pane double-dispatch guard +
# send-keys + bare Enter + logging. We do NOT re-implement any of that.
DISPATCH="/code-audit-sweep $STALEST_REPO $AUDIT_TYPE"
SWEEP_LOG="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}/lib/bot-sweep-cron.log"

# bot-sweep-cron.sh exits 0 on BOTH a real dispatch and a busy-skip, so we
# diff its log to report the true outcome instead of assuming success.
_log_before=0
[ -f "$SWEEP_LOG" ] && _log_before=$(wc -l < "$SWEEP_LOG")

dispatch_rc=0
# Pass the owner's resolved socket explicitly (we have OWNER_DIR here); the
# child falls back to a name-based reverse-lookup only if we hand it nothing.
"$LIB_DIR/bot-sweep-cron.sh" "$OWNER_BOT" "$DISPATCH" "$(tmux_socket_for_bot "$OWNER_DIR" 2>/dev/null || true)" || dispatch_rc=$?

_new_log=""
[ -f "$SWEEP_LOG" ] && _new_log=$(tail -n +"$((_log_before + 1))" "$SWEEP_LOG" || true)

if [ "$dispatch_rc" -ne 0 ] || printf '%s' "$_new_log" | grep -q 'ERROR'; then
    emit_event "$OWNER_DIR" "$OWNER_BOT" "audit_failed" \
        '{"repo":"'"$STALEST_REPO"'","audit_type":"'"$AUDIT_TYPE"'","reason":"dispatch_failed"}'
    exit 1
elif printf '%s' "$_new_log" | grep -q 'busy'; then
    # Owner mid-task: the guard skipped this tick. Next run retries naturally.
    emit_event "$OWNER_DIR" "$OWNER_BOT" "audit_deferred" \
        '{"repo":"'"$STALEST_REPO"'","audit_type":"'"$AUDIT_TYPE"'","reason":"owner_busy"}'
else
    emit_event "$OWNER_DIR" "$OWNER_BOT" "audit_dispatched" \
        '{"repo":"'"$STALEST_REPO"'","audit_type":"'"$AUDIT_TYPE"'"}'
fi
