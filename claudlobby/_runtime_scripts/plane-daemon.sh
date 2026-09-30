#!/bin/bash
# plane-daemon.sh — launcher the composed host-service units exec (Phase-2 T1).
#
# Thin by rule: env resolution here, everything else in `claudlobby plane
# serve`. Ends in exec so supervision (systemd Restart=always / launchd
# KeepAlive) signals the daemon itself, with no bash intermediary to orphan.
# Composition selects the absolute executable and data root. Validation uses
# only shell builtins; no Python or host-tool probe precedes exec.

set -euo pipefail

case "${BASH_SOURCE[0]}" in
    */*) LIB_DIR="${BASH_SOURCE[0]%/*}" ;;
    *) LIB_DIR="." ;;
esac
# shellcheck source=cli-context.sh
. "$LIB_DIR/cli-context.sh"
_claudlobby_require_root
_claudlobby_require_cli

# --root is a GLOBAL flag: it precedes the subcommand (the smoke run caught
# the inverted order as an argparse usage error — stubs accept any argv, the
# real CLI does not).
ARGS=(--root "$CLAUDLOBBY_ROOT" plane serve)
[ -n "${PLANE_SOCKET:-}" ] && ARGS+=(--socket "$PLANE_SOCKET")
[ -n "${PLANE_DRAIN_INTERVAL:-}" ] && ARGS+=(--drain-interval "$PLANE_DRAIN_INTERVAL")

exec "$CLAUDLOBBY_CLI" "${ARGS[@]}"
