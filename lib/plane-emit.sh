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
# During dual-write the legacy JSONL row is the load-bearing record.
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

# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
set +e  # lib-common re-arms set -e at source time; the ladder inspects rcs

ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
SOCK="${PLANE_SOCKET:-$ROOT/state/plane/ingest.sock}"

finalized="$(mktemp "${TMPDIR:-/tmp}/plane-emit.XXXXXX")"
trap 'rm -f "$finalized"' EXIT

python3 "$LIB_DIR/plane-socket-client.py" \
    --socket "$SOCK" --finalize-to "$finalized"
rc=$?
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
