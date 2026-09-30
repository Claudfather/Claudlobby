#!/bin/bash
# Private Plane telemetry shim. A socket acknowledgement is the only committed
# outcome (exit 0). When the daemon cannot answer, the stdlib-only client writes
# the existing durable raw staged queue and exits pending (6); the daemon replays it
# later. No full claudlobby CLI is spawned for a telemetry event.
#
# Exit 2: bad input or contract verdict. Exit 3: total failure, including an
# unwriteable or full staged queue and an untrusted capture policy (counted in
# state/plane/.emit-losses). Exit 6: durable pending in staged or daemon spool,
# never queryable yet. Callers that need an immediate receipt must require 0 or
# use the commit-required Plane API; they cannot treat 6 as success.
#
# PLANE_EMIT_DISABLED=1 is a no-op for isolated harnesses. The socket deadline
# follows PLANE_EMIT_CLASS (hook/background/door), with optional class-specific
# PLANE_SOCKET_DEADLINE_*_S settings. On socket failure the client applies the
# capture policy before it stages, within the staged queue's bound; only the
# daemon replays that queue. All paths use explicit
# CLAUDLOBBY_ROOT, never ambient fleet discovery.

set -euo pipefail

[ "${PLANE_EMIT_DISABLED:-0}" = "1" ] && exit 0

# The invoked adapter supplies its package location, never its data root.
# Validation is shell-only. Do not source lib-common or import the package.
case "${BASH_SOURCE[0]}" in
    */*) LIB_DIR="${BASH_SOURCE[0]%/*}" ;;
    *) LIB_DIR="." ;;
esac
case "${CLAUDLOBBY_ROOT:-}" in
    /*) ROOT="$CLAUDLOBBY_ROOT" ;;
    *)
        printf 'plane-emit: CLAUDLOBBY_ROOT must be an explicit absolute data directory (got %s)\n' \
            "${CLAUDLOBBY_ROOT:-<unset>}" >&2
        exit 127
        ;;
esac

set +e  # native client verdicts are inspected below

SOCK="${PLANE_SOCKET:-$ROOT/state/plane/ingest.sock}"

# A transport miss arms a bounded cooldown so subsequent hot-path events stage
# without repeating the socket deadline. A later successful socket ACK clears
# it; expiry retries the socket automatically.
WEDGE_MARK="$ROOT/state/plane/.socket-wedged"
COOLDOWN="${PLANE_WEDGE_COOLDOWN_S:-60}"
skip_socket=0
if [ -f "$WEDGE_MARK" ]; then
    _now=$(date +%s); _mark=$(cat "$WEDGE_MARK" 2>/dev/null || echo 0)
    case "$_mark" in ''|*[!0-9]*) _mark=0 ;; esac
    _delta=$(( _now - _mark ))
    # A marker AHEAD of the clock is a skew artifact, never a live wedge: this
    # estate's RTC-less host boots up to ~1h behind real time, so a pre-crash
    # marker outruns the clock and a plain `delta < cooldown` pins the socket
    # rung off for the whole skew — during the boot emit storm, exactly when
    # rung 1 matters most. Negative delta = expired.
    if [ "$_delta" -ge 0 ] && [ "$_delta" -lt "$COOLDOWN" ]; then
        skip_socket=1
        # The policy-applied batch will be durably staged for daemon replay.
        printf 'plane-emit: socket in wedge cooldown (%ss) — skipping the socket\n' "$COOLDOWN" >&2
    else
        rm -f "$WEDGE_MARK"
    fi
fi

if [ "$skip_socket" = "1" ]; then
    # Finalize and persist without contacting the socket. Invalid input still
    # returns 2; a successful stage returns pending (6).
    python3 -S -E "$LIB_DIR/plane-socket-client.py" \
        --socket "$SOCK" --finalize-only \
        --stage-to "$ROOT/state/plane/staged"
    rc=$?
else
    # The deadline follows WHO WAITS (#1693). On the Pi's SD card an ordinary
    # commit can outlast 1.0 s, and each miss arms the marker for every door on
    # the host, so a caller that can afford to wait longer should. A knob the
    # client would refuse is ignored OUT LOUD, never passed on: the client's
    # refusal is rc 2, a verdict, so a typo must not drop every emission.
    _knob=""; deadline=""
    case "${PLANE_EMIT_CLASS:-}" in
        hook)       _knob=PLANE_SOCKET_DEADLINE_HOOK_S; deadline="${PLANE_SOCKET_DEADLINE_HOOK_S:-}" ;;
        background) _knob=PLANE_SOCKET_DEADLINE_BACKGROUND_S; deadline="${PLANE_SOCKET_DEADLINE_BACKGROUND_S:-}" ;;
        door)       _knob=PLANE_SOCKET_DEADLINE_DOOR_S; deadline="${PLANE_SOCKET_DEADLINE_DOOR_S:-}" ;;
        '')         ;;
        *)          printf 'plane-emit: unknown PLANE_EMIT_CLASS=%s (hook, background or door) — using the default deadline\n' "$PLANE_EMIT_CLASS" >&2 ;;
    esac
    if [ -n "$deadline" ]; then
        _num='^([0-9]+(\.[0-9]*)?|\.[0-9]+)$'; _zero='^[0.]+$'; _int="${deadline%%.*}"
        if ! [[ $deadline =~ $_num ]] || [[ $deadline =~ $_zero ]] \
           || [ "${#_int}" -gt 4 ] || [ "${_int:-0}" -ge 3600 ]; then
            printf 'plane-emit: ignoring %s=%s (want seconds, above 0 and below 3600) — using the default deadline\n' "$_knob" "$deadline" >&2
            deadline=""
        fi
    fi
    # -S -E: skip site/pyvenv machinery — the client is minimal-stdlib by
    # contract (measured: 45ms -> 12ms interpreter spawn on the Pi).
    # --arm-log names WHO missed and why; rc 7 means that the missed batch is
    # already in the durable staged queue, so the shim arms its cooldown.
    python3 -S -E "$LIB_DIR/plane-socket-client.py" \
        --socket "$SOCK" \
        --stage-to "$ROOT/state/plane/staged" \
        --arm-log "$ROOT/state/plane/.socket-arms" ${deadline:+--timeout "$deadline"}
    rc=$?
    if [ "$rc" -eq 7 ]; then
        { date +%s > "$WEDGE_MARK"; } 2>/dev/null || true
    elif [ "$rc" -eq 0 ]; then
        rm -f "$WEDGE_MARK" 2>/dev/null
    fi
fi
case "$rc" in
    0) exit 0 ;;  # daemon committed or classified duplicate
    6) exit 6 ;;  # staged or daemon-spooled: pending, never queryable yet
    7)
        # The socket failed and the native client durably staged this batch.
        # Arm the cooldown for the next hot-path event, but expose only the
        # public pending result (6), never a false committed receipt (0).
        exit 6 ;;
    2|3) exit "$rc" ;;
esac
printf 'plane-emit: unexpected native client rc=%s — commit unconfirmed\n' "$rc" >&2
exit 3
