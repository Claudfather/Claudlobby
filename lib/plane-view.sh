#!/bin/bash
# plane-view.sh — launcher the composed claudlobby-plane-view host-service
# units exec (Phase-4 T4; plane-daemon.sh sibling, same rules).
#
# Thin by rule: env resolution here, everything else in `claudlobby plane
# view`. Ends in exec so supervision signals the daemon itself. The exec
# ladder is DELIBERATELY inline, not shared: launchers must work on a
# stripped PATH (a bare host boot), and sourcing lib-common would drag its
# environment probes into that path — the bare-env behavior is pinned by
# tests/test_plane_daemon_units.py, which is the drift guard for the copies.
# The view binds LOCALHOST by default — Tailscale Serve fronts it (design
# walk ruling); PLANE_VIEW_HOST is the raw-bind dev override. Dormant until
# a host arms plane-view.enroll in its system.yaml, like plane-daemon.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
export CLAUDLOBBY_ROOT="$ROOT"

# --root is a GLOBAL flag: it precedes the subcommand (plane-daemon.sh:19).
ARGS=(--root "$ROOT" plane view)
[ -n "${PLANE_VIEW_HOST:-}" ] && ARGS+=(--host "$PLANE_VIEW_HOST")
[ -n "${PLANE_VIEW_PORT:-}" ] && ARGS+=(--port "$PLANE_VIEW_PORT")

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
printf 'plane-view.sh: no claudlobby CLI resolvable from %s\n' "$ROOT" >&2
exit 127
