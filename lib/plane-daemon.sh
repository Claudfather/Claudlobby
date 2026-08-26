#!/bin/bash
# plane-daemon.sh — launcher the composed host-service units exec (Phase-2 T1).
#
# Thin by rule: env resolution here, everything else in `claudlobby plane
# serve`. Ends in exec so supervision (systemd Restart=always / launchd
# KeepAlive) signals the daemon itself, with no bash intermediary to orphan.
# The exec ladder mirrors lib-common claudlobby_cli (console script from the
# venv, then PATH, then python3 -m from the root) in an exec-shaped form —
# claudlobby_cli itself RUNS the CLI, and a launcher must replace itself.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
export CLAUDLOBBY_ROOT="$ROOT"

# --root is a GLOBAL flag: it precedes the subcommand (the smoke run caught
# the inverted order as an argparse usage error — stubs accept any argv, the
# real CLI does not).
ARGS=(--root "$ROOT" plane serve)
[ -n "${PLANE_SOCKET:-}" ] && ARGS+=(--socket "$PLANE_SOCKET")
[ -n "${PLANE_DRAIN_INTERVAL:-}" ] && ARGS+=(--drain-interval "$PLANE_DRAIN_INTERVAL")

if [ -x "$ROOT/.venv/bin/claudlobby" ]; then
    exec "$ROOT/.venv/bin/claudlobby" "${ARGS[@]}"
fi
if command -v claudlobby >/dev/null 2>&1; then
    exec claudlobby "${ARGS[@]}"
fi
# python3 existing is not python3 being USABLE: on a bare host (GitHub Linux,
# a fresh box) /bin/python3 exists and cannot import claudlobby, and
# `python3 -m claudlobby` then exits 1 — indistinguishable from a daemon
# failure. Probe the import first; an unusable interpreter falls through to
# the honest 127. One extra spawn at daemon START only, never per event.
if command -v python3 >/dev/null 2>&1 \
    && (cd "$ROOT" && python3 -c "import claudlobby" >/dev/null 2>&1); then
    cd "$ROOT"
    exec python3 -m claudlobby "${ARGS[@]}"
fi
printf 'plane-daemon.sh: no claudlobby CLI resolvable from %s\n' "$ROOT" >&2
exit 127
