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
# Claude Code fires the skill, instead of the old set +H; degraded prose. Skips
# with a logged briefing_deferred event when the bot is busy or its session is
# absent — briefings are time-sensitive, so skip-and-log beats queue
# (sprint-trigger precedent).
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
LOG="${BRIEFING_TRIGGER_LOG:-$BOT_DIR/logs/briefing-trigger.log}"
setup_log_dir "$LOG"
TS="$(ts_iso)"

# Event data payload — reason names why the run dispatched or deferred.
briefing_data() { printf '{"bot":"%s","slot":"%s","reason":"%s"}' "$BOT" "$SLOT" "$1"; }

# Skip-with-log: emit a briefing_deferred event + log line, then exit 0. One home
# for the deferred-event shape shared by the session-absent and busy branches.
# $1 = reason (event data), $2 = human-readable log note.
defer() {
    echo "$TS DEFER $BOT/$SLOT — $2" >> "$LOG"
    emit_fleet_event briefing_deferred briefing "$(briefing_data "$1")" "$BOT_DIR" "$BOT"
    exit 0
}

if [ ! -d "$BOT_DIR" ]; then
    echo "$TS SKIP $BOT/$SLOT — bot dir absent: $BOT_DIR" >> "$LOG"
    # No bot dir to own the event — fleet-level ledger, attributed to the bot id.
    emit_fleet_event briefing_deferred briefing "$(briefing_data bot_dir_absent)" "" "$BOT"
    exit 0
fi

# Session name is the bot name; tmux resolves it to the running session on the
# bot private socket (the dispatch.sh / tmux_socket_for_session convention).
SOCKET="$(tmux_socket_for_bot "$BOT_DIR" 2>/dev/null || true)"

if ! check_tmux_session "$BOT" "$SOCKET"; then
    defer session_absent "session not alive"
fi

# Never inject into an active turn (bot_is_busy, lib-common SSOT).
if bot_is_busy "$SOCKET" "$BOT"; then
    defer bot_busy "bot busy"
fi

# --- observable-plane dual-write (PR-B T6; the inventory's judgment row: a
# briefing-class communication carried as a raw slash injection — the door
# mints it a communication). Dormant behind PLANE_EMIT_ENABLED=1; disclosed,
# never blocking. Intent before the send (F9); the busy defer above means a
# sent briefing lands in an idle pane, so a clean send is pane_submitted.
PLANE_ARMED=0
if [ "${PLANE_EMIT_ENABLED:-0}" = "1" ] && [ "${PLANE_EMIT_DISABLED:-0}" != "1" ]; then
    PLANE_ARMED=1
fi
PLANE_MSG_ID=""
_plane_emit() {
    # stderr passes through — the shim's fallback disclosure is the contract.
    "$LIB_DIR/plane-emit.sh" >/dev/null || \
        echo "briefing-trigger: plane record failed rc=$? (briefing unaffected)" >&2
}
if [ "$PLANE_ARMED" = "1" ]; then
    PLANE_MSG_ID="msg_$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"
    printf '{"events":[{"event_type":"communication","emitter":"briefing-trigger","fleet":"%s","payload":{"msg_id":"%s","sender":"system:briefing-trigger","recipient":"bot:%s/%s","recipient_raw":"%s","message_class":"briefing","body":"/briefing %s"}}]}' \
        "$(json_escape "$FLEET")" "$PLANE_MSG_ID" \
        "$(json_escape "$FLEET")" "$(json_escape "$BOT")" \
        "$(json_escape "$BOT")" "$(json_escape "$SLOT")" | _plane_emit || true
fi
_plane_transmission() {
    [ "$PLANE_ARMED" = "1" ] || return 0
    printf '{"events":[{"event_type":"transmission","emitter":"briefing-trigger","fleet":"%s","payload":{"msg_id":"%s","attempt_no":1,"carrier":"tmux","destination":"%s","state":"%s"}}]}' \
        "$(json_escape "$FLEET")" "$PLANE_MSG_ID" \
        "$(json_escape "$BOT")" "$1" | _plane_emit || true
}

if "$LIB_DIR/dispatch.sh" "$BOT" "/briefing $SLOT"; then
    echo "$TS DISPATCH $BOT/$SLOT — /briefing $SLOT sent" >> "$LOG"
    emit_fleet_event briefing_dispatched briefing "$(briefing_data ok)" "$BOT_DIR" "$BOT"
    _plane_transmission "pane_submitted"
else
    echo "$TS FAIL $BOT/$SLOT — dispatch failed" >> "$LOG"
    emit_fleet_event briefing_failed briefing "$(briefing_data dispatch_failed)" "$BOT_DIR" "$BOT"
    _plane_transmission "failed"
    exit 1
fi
