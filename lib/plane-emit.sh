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
# EXIT 0 MEANS RECORDED — in the plane, queryable now. It does NOT mean
# "accepted" (#1711). A batch the db was unavailable for is SPOOLED: durable on
# disk (fsync'd file AND parent, atomic rename) and invisible to every reader
# until a drain ingests it. That is exit 6, its own code, because 0 asserted
# something false about it and a failure code asserts the opposite falsehood —
# nothing was lost and nothing needs retrying.
#
# Idempotent across rungs: lib/plane-socket-client.py mints event_ids into a
# finalized file BEFORE the first attempt; rung 2 replays that exact file, so
# a commit whose ack was lost classifies as duplicate, never a second row.
#
# Verdicts do not fall back: exits 2 (contract) and 3 (total failure) pass
# through — the CLI would only repeat them. THAT is the whole test, and
# `downgrade` fails it (#1485): a downgrade refusal says the db is newer than
# the DAEMON'S LOADED code, and the daemon is a long-lived process on an
# editable install, so a pull that carries a migration leaves its modules the
# only stale thing on the host. The cold rung is a fresh interpreter on the
# install's CURRENT code and commits, so plane-socket-client.py maps that
# refusal to 5 and it lands here, in the fallback.
#
# So the passthrough arm carries 2 and 3 and NOTHING ELSE: the client returns
# only 0, 2, 3 or 5, and a 4 in that arm was dead code describing a path that
# does not exist. A COLD-rung downgrade — where the INSTALL is behind the db
# and no rung can help — still exits 4, at the tail, where the shim returns
# the CLI rc verbatim.
#
# One consequence worth knowing: a downgrade's rc 5 also ARMS the wedge marker
# below, so the next PLANE_WEDGE_COOLDOWN_S (60s default) of emissions skip the
# socket and go straight to the cold rung, then the socket is retried. Nothing
# is dropped — it is the same records by a slower rung, which is the right
# trade while the supervisor is relaunching a stale daemon underneath.
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
    6) exit 6 ;;         # SPOOLED (#1711): accepted, durable on disk, NOT in
                         # the plane. A verdict for the same reason 2 and 3 are
                         # — the batch is already written, so the cold rung
                         # would only spool it a second time. Deliberately NOT
                         # 0: rc 0 is the one signal every door reads as
                         # "recorded", and no reader can see a spooled row.
    2|3) exit "$rc" ;;   # verdicts: the CLI would only repeat them. 4 is NOT
                         # listed because the client cannot return it (#1485
                         # maps a stale daemon to 5); a cold-rung downgrade
                         # exits 4 at the tail below, where the CLI rc rides
                         # out verbatim.
esac

# Only rc=5 reaches here, and it means two DISJOINT things depending on which
# rung produced it (#1657) — the client itself proves this (plane-socket-
# client.py): the finalize_only branch `return`s 5 unconditionally on a
# successful write, with no failure path at all; the transport branch
# returns 5 only from its own except block, printing "transport failed"
# first. So on skip_socket=1, rc=5 is the client's documented SUCCESS for
# --finalize-only — the daemon was deliberately never contacted, and calling
# that "daemon unavailable" is not an inflated failure, it is a wrong one.
# Measured, 24h journal, one host: 645 of every 719 "daemon unavailable"
# lines were this branch, not a transport failure (10.3% real, 9.7x
# inflation) — and the discriminator lived only in the DIFFERENT line
# printed above this one, invisible to a grep for the error string itself.
# The two branches below are self-sufficient on their own line for exactly
# that reason.
if [ "$skip_socket" = "1" ]; then
    printf 'plane-emit: cooldown finalize succeeded (rc=%s) — daemon not contacted, replaying cold as planned\n' "$rc" >&2
else
    # Deliberately NOT "daemon unavailable" here either (review residual,
    # #1657): plane-socket-client.py's transport-failed except block catches
    # connect refusal (genuinely unavailable), a deadline miss (reachable,
    # too slow — "unavailable" is false), and a garbled reply (reachable,
    # answered) under the SAME rc=5. Only the first sub-cause makes
    # "unavailable" true; the genuine-breach mechanism itself is unreproduced
    # (#1657's own stated bound), so naming a specific cause here would be
    # exactly the mistake this fix exists to remove, just relocated. State
    # only what is known: the transport failed and the client already said
    # why on the line above.
    printf 'plane-emit: transport failed (rc=%s) — falling back to cold CLI\n' "$rc" >&2
fi
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
    if [ "$rc" -eq 6 ]; then
        # NOT a failure and must not be worded as one (#1711): the batch is on
        # disk and lands at the next drain. A door that cries failure for a
        # non-failure teaches its reader to ignore the line that matters.
        printf 'plane-emit: SPOOLED by the cold rung — durable on disk, NOT in the plane until a drain\n' >&2
    elif [ "$rc" -ne 0 ]; then
        printf 'plane-emit: cold CLI rung failed rc=%s\n' "$rc" >&2
    fi
    exit "$rc"
fi
# The client died before finalizing (bad stdin never lands here — that is a
# rung-1 exit 2): nothing safe to replay.
printf 'plane-emit: no finalized batch to replay — total failure\n' >&2
exit 3
