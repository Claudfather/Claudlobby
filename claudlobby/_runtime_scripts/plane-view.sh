#!/bin/bash
# plane-view.sh — launcher the composed claudlobby-plane-view host-service
# units exec (Phase-4 T4; plane-daemon.sh sibling, same rules).
#
# Thin by rule: env resolution here, everything else in `claudlobby plane
# view`. Ends in exec so supervision signals the daemon itself. The selected
# executable and data root are composed explicitly; shared validation uses
# only shell builtins, so stripped-PATH boot requires no import/probe ladder.
# The view binds LOCALHOST by default — Tailscale Serve fronts it (design
# walk ruling); PLANE_VIEW_HOST is the raw-bind dev override. Dormant until
# a host arms plane-view.enroll in its system.yaml, like plane-daemon.

set -euo pipefail

case "${BASH_SOURCE[0]}" in
    */*) LIB_DIR="${BASH_SOURCE[0]%/*}" ;;
    *) LIB_DIR="." ;;
esac
# shellcheck source=cli-context.sh
. "$LIB_DIR/cli-context.sh"
_claudlobby_require_root
_claudlobby_require_cli

# --root is a GLOBAL flag: it precedes the subcommand (plane-daemon.sh:19).
ARGS=(--root "$CLAUDLOBBY_ROOT" plane view)
[ -n "${PLANE_VIEW_HOST:-}" ] && ARGS+=(--host "$PLANE_VIEW_HOST")
[ -n "${PLANE_VIEW_PORT:-}" ] && ARGS+=(--port "$PLANE_VIEW_PORT")

exec "$CLAUDLOBBY_CLI" "${ARGS[@]}"
