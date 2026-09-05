#!/bin/bash
# plane-emit.sh — THE recording shim every door routes through (Phase-2 T2).
#
# Reads one batch ({"events": [...]} or bare array) on stdin and lands it in
# the plane by the first rung that answers, each fallback DISCLOSED on stderr,
# never silent:
#
#   rung 1  ingest daemon (unix socket; ~ms — no Python-package spawn)
#   rung 2  cold `claudlobby emit-batch` (the CLI spools on db failure, which
#           is rung 3 by construction)
#
# Idempotent across rungs: lib/plane-socket-client.py mints event_ids into a
# finalized file BEFORE the first attempt; rung 2 replays that exact file, so
# a commit whose ack was lost classifies as duplicate, never a second row.
#
# Verdicts do not fall back: exits 2 (contract), 3 (total failure), 4
# (downgrade) pass through — the CLI would only repeat them.
#
# THE SHIM NEVER BLOCKS A DOOR'S REAL ACTION. Doors call it as
#   plane_emit <<<"$batch" || log "plane record failed rc=$? (acted, unrecorded)"
# — the send/report/restart itself must already have happened or still happen.
#
# Env:
#   CLAUDLOBBY_ROOT      install root (default: this script's parent)
#   PLANE_SOCKET         socket override (default: $ROOT/state/plane/ingest.sock)
#   PLANE_EMIT_DISABLED  =1 -> no-op exit 0 (the ruled harness exemption:
#                        byte-identical legacy behavior, nothing spawned)
#   PLANE_EMIT_CLI       fallback command override (tests stub it; default
#                        resolves through lib-common's claudlobby_cli).
#                        CONTRACT: a command LINE, whitespace-split — the
#                        systemd ExecStart convention (#969). An executable
#                        whose PATH contains spaces is not expressible;
#                        wrap it in a script.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ "${PLANE_EMIT_DISABLED:-0}" = "1" ] && exit 0

# lib-common is NOT sourced on the hot path (T10 budget lever, measured on the
# Pi): rung 1 needs nothing from it, and sourcing ~3600 lines per emit is a
# door-felt tax. The fallback rung sources it lazily for claudlobby_cli.
set +e  # the ladder inspects rcs

ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
SOCK="${PLANE_SOCKET:-$ROOT/state/plane/ingest.sock}"

finalized="$(mktemp "${TMPDIR:-/tmp}/plane-emit.XXXXXX")"
trap 'rm -f "$finalized"' EXIT

# Wedge circuit-breaker (#1372 re-verify blocking residual on F5): the client
# deadline bounds ONE emission, but doors emit twice (intent + outcome), so a
# wedged listener still compounded past a door's own latency alarm. On a
# transport-wedge (rc 5) a cooldown marker is set and every emission — this
# door's second, and every other door's — skips the socket rung for
# PLANE_WEDGE_COOLDOWN_S (default 60s, disclosed), going straight to the cold
# CLI. Cleared by the next successful socket emit; self-heals by expiry.
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
        printf 'plane-emit: socket in wedge cooldown (%ss) — straight to cold CLI\n' "$COOLDOWN" >&2
    else
        rm -f "$WEDGE_MARK"
    fi
fi

if [ "$skip_socket" = "1" ]; then
    # Finalize without a send so the CLI rung has its idempotent batch. The
    # client's own rc carries verdicts (2 = bad stdin, before any finalize)
    # and 5 on finalize-only success — pass it through, never overwrite: a
    # hardcoded 5 here turned a contract violation into "daemon unavailable"
    # + a doomed CLI replay + exit 3, precisely during incident windows.
    python3 -S -E "$LIB_DIR/plane-socket-client.py" \
        --socket "$SOCK" --finalize-to "$finalized" --finalize-only
    rc=$?
else
    # -S -E: skip site/pyvenv machinery — the client is minimal-stdlib by
    # contract (measured: 45ms -> 12ms interpreter spawn on the Pi).
    python3 -S -E "$LIB_DIR/plane-socket-client.py" \
        --socket "$SOCK" --finalize-to "$finalized"
    rc=$?
    if [ "$rc" -eq 5 ]; then
        { date +%s > "$WEDGE_MARK"; } 2>/dev/null || true
    elif [ "$rc" -eq 0 ]; then
        rm -f "$WEDGE_MARK" 2>/dev/null
    fi
fi
case "$rc" in
    0) exit 0 ;;
    2|3|4) exit "$rc" ;;   # verdicts: the CLI would only repeat them
esac

printf 'plane-emit: daemon unavailable (rc=%s) — falling back to cold CLI\n' "$rc" >&2
if [ -s "$finalized" ]; then
    # --root is global: before the subcommand.
    if [ -n "${PLANE_EMIT_CLI:-}" ]; then
        $PLANE_EMIT_CLI --root "$ROOT" emit-batch --json "$finalized"
    else
        # shellcheck source=lib-common.sh
        . "$LIB_DIR/lib-common.sh"   # lazy: only this rung needs claudlobby_cli
        set +e                        # lib-common re-arms set -e at source time
        claudlobby_cli --root "$ROOT" emit-batch --json "$finalized"
    fi
    rc=$?
    [ "$rc" -ne 0 ] && printf 'plane-emit: cold CLI rung failed rc=%s\n' "$rc" >&2
    exit "$rc"
fi
# The client died before finalizing (bad stdin never lands here — that is a
# rung-1 exit 2): nothing safe to replay.
printf 'plane-emit: no finalized batch to replay — total failure\n' >&2
exit 3
