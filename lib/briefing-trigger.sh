#!/bin/bash
# briefing-trigger.sh — fire a bot's scheduled briefing as a REAL slash command.
#
# Usage: briefing-trigger.sh <fleet> <bot> <slot>
#
# Run by the composer-generated per-(bot,slot) briefing timer
# (<prefix>.briefing-<bot>-<slot>, emitted from the bots.<bot>.briefing stanza).
# Generic and committed — it lives in lib/, so data-sweep can never purge it.
#
# Delivers "/briefing <slot>" to the bot's OWN session through the slash-aware
# dispatch.sh (P2/#629): the slash reaches the pane as its first characters so
# Claude Code fires the skill, instead of the old set +H; degraded prose. A busy
# or absent bot defers the slot (briefing_deferred) and gets a bounded retry,
# sent at its first idle check — briefings are time-sensitive, so a bounded wait
# beats a queue. A slot that is missed sends ONE FLEET NOTICE.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

FLEET="${1:?Usage: briefing-trigger.sh <fleet> <bot> <slot>}"
BOT="${2:?Usage: briefing-trigger.sh <fleet> <bot> <slot>}"
SLOT="${3:?Usage: briefing-trigger.sh <fleet> <bot> <slot>}"

BOTS_DIR="$(resolve_bots_dir "$FLEET")"
BOT_DIR="$BOTS_DIR/$BOT"
TS="$(ts_iso)"

# Event data payload — reason names why the run dispatched or deferred.
briefing_data() { printf '{"bot":"%s","slot":"%s","reason":"%s"}' "$BOT" "$SLOT" "$1"; }

# A missed slot pages ONCE through the shared FLEET NOTICE path (#1826). Each run
# owns one slot fire and calls this at most once, so it needs no dedup marker.
missed() { emit_fleet_notice "$BOTS_DIR" briefing_missed "$BOT $SLOT ($1)"; }

if [ ! -d "$BOT_DIR" ]; then
    echo "$TS SKIP $BOT/$SLOT — bot dir absent: $BOT_DIR" >&2
    # No bot dir to own the event — fleet-level ledger, attributed to the bot id.
    emit_fleet_event briefing_deferred briefing "$(briefing_data bot_dir_absent)" "" "$BOT"
    missed bot_dir_absent
    exit 0
fi
# The log lives in the bot dir, so it can only be made once the dir is known to exist.
LOG="${BRIEFING_TRIGGER_LOG:-$BOT_DIR/logs/briefing-trigger.log}"
setup_log_dir "$LOG"

# /briefing resolves only through the COMPOSED skill, which the briefing: stanza
# links. Without it (a hand-built timer, a deleted link) Claude Code rejects the
# command locally as "Unknown command", the input box still clears, and the send
# below reads OK with nothing run (#1819). /briefing is a library skill, never a
# native command, so only "available" sends; and a briefing that can never run
# is a defect, not a defer.
_skill_status="$(session_command_status /briefing "$BOT_DIR" || true)"
if [ "$_skill_status" != available ]; then
    echo "$TS FAIL $BOT/$SLOT — no briefing skill composed: declare bots.$BOT.briefing and regenerate" \
        | tee -a "$LOG" >&2
    emit_fleet_event briefing_failed briefing "$(briefing_data skill_absent)" "$BOT_DIR" "$BOT"
    exit 1
fi

# Session name is the bot name; tmux resolves it to the running session on the
# bot private socket (the dispatch.sh / tmux_socket_for_session convention).
SOCKET="$(tmux_socket_for_bot "$BOT_DIR" 2>/dev/null || true)"

# Why the bot cannot take the slash right now, in REASON; empty when it can.
# Never inject into an active turn (bot_is_busy, lib-common SSOT).
not_ready() {
    REASON=""
    if ! check_tmux_session "$BOT" "$SOCKET"; then REASON=session_absent
    elif bot_is_busy "$SOCKET" "$BOT" "$BOT_DIR"; then REASON=bot_busy
    fi
}

# A deferred slot is re-checked every RETRY_POLL_S for up to RETRY_WINDOW_S and
# sent at the first idle check (#1826). It waits here because the timer's next
# tick is the next day's briefing; the oneshot unit has no start timeout. The
# window is counted in polls, never read off a clock a boot-time step can move.
# The env overrides are a harness and test seam: the timer's env is closed.
RETRY_WINDOW_S="${BRIEFING_RETRY_WINDOW_S:-1800}"
RETRY_POLL_S="${BRIEFING_RETRY_POLL_S:-60}"
not_ready
if [ -n "$REASON" ]; then
    echo "$TS DEFER $BOT/$SLOT — $REASON; retrying for up to ${RETRY_WINDOW_S}s" >> "$LOG"
    emit_fleet_event briefing_deferred briefing "$(briefing_data "$REASON")" "$BOT_DIR" "$BOT"
    TRIES=$(( RETRY_WINDOW_S / RETRY_POLL_S ))
    while [ -n "$REASON" ] && [ "$TRIES" -gt 0 ]; do
        sleep "$RETRY_POLL_S"
        TRIES=$(( TRIES - 1 ))
        not_ready
    done
    TS="$(ts_iso)"
    if [ -n "$REASON" ]; then
        echo "$TS GIVEUP $BOT/$SLOT — still $REASON after ${RETRY_WINDOW_S}s" >> "$LOG"
        emit_fleet_event briefing_failed briefing "$(briefing_data "$REASON")" "$BOT_DIR" "$BOT"
        missed "$REASON"
        exit 0
    fi
fi

# --- observable-plane record (PR-B T6; the inventory's judgment row: a
# briefing-class communication carried as a raw slash injection — the door
# mints it a communication). Always on (PLANE_EMIT_DISABLED=1 is the one silencer); disclosed,
# never blocking. Intent before the send (F9); the busy defer above means a
# sent briefing lands in an idle pane, so a clean send is pane_submitted.
PLANE_ARMED=0
if plane_armed briefing-trigger; then
    PLANE_ARMED=1
fi
PLANE_MSG_ID=""
if [ "$PLANE_ARMED" = "1" ]; then
    PLANE_MSG_ID="$(plane_mint_id msg)"
    printf '{"events":[{"event_type":"communication","emitter":"briefing-trigger","fleet":"%s","payload":{"msg_id":"%s","sender":"system:briefing-trigger","recipient":"bot:%s/%s","recipient_raw":"%s","message_class":"briefing","body":"/briefing %s"}}]}' \
        "$(json_escape "$FLEET")" "$PLANE_MSG_ID" \
        "$(json_escape "$FLEET")" "$(json_escape "$BOT")" \
        "$(json_escape "$BOT")" "$(json_escape "$SLOT")" | plane_emit_events briefing-trigger || true
fi
_plane_transmission() {
    [ "$PLANE_ARMED" = "1" ] || return 0
    # fold F1: a submission-class state carries the delivery-JOIN wire proof,
    # read back from PLANE_WIRE_OUT across the dispatch.sh subprocess boundary.
    printf '{"events":[%s]}' \
        "$(plane_tx_event briefing-trigger "$FLEET" tmux "$PLANE_MSG_ID" "$BOT" "$1" "$(_wire_frag "$1")")" \
        | plane_emit_events briefing-trigger || true
}

# PLANE_MSG_ID across the process boundary (chunk P, #1501): the briefing is a
# tracked communication, so bot_tmux_send tags the send and the receiver records
# delivery. Empty when the plane is unarmed -> no trailer.
# fold F1: PLANE_WIRE_OUT scratch file so bot_tmux_send (inside dispatch.sh) can
# hand back the wire proof for the pane_submitted row. Auto-cleaned on EXIT via
# lib-common's tmpdir trap (the else branch exits before any explicit removal).
_plane_wire_out=""
[ "$PLANE_ARMED" = "1" ] && _plane_wire_out=$(safe_mktemp)
if PLANE_MSG_ID="$PLANE_MSG_ID" PLANE_WIRE_OUT="$_plane_wire_out" "$LIB_DIR/dispatch.sh" "$BOT" "/briefing $SLOT"; then
    _read_wire_out "$_plane_wire_out"
    echo "$TS DISPATCH $BOT/$SLOT — /briefing $SLOT sent" >> "$LOG"
    emit_fleet_event briefing_dispatched briefing "$(briefing_data ok)" "$BOT_DIR" "$BOT"
    _plane_transmission "pane_submitted"
else
    echo "$TS FAIL $BOT/$SLOT — dispatch failed" >> "$LOG"
    emit_fleet_event briefing_failed briefing "$(briefing_data dispatch_failed)" "$BOT_DIR" "$BOT"
    _plane_transmission "failed"
    missed dispatch_failed
    exit 1
fi
