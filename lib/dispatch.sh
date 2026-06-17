#!/bin/bash
# Manager-to-worker dispatch via tmux with race-safe two-step send.
# Usage: dispatch.sh <worker-session> <message...>
#
# Checks the target session exists, sanitizes the input for tmux safety,
# then sends text and Enter as separate steps with a brief pause to avoid
# the TUI swallowing keystrokes during render.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

WORKER_SESSION="${1:?Usage: dispatch.sh <worker-session> <message...>}"
shift
MESSAGE="$*"

# Resolve the worker's private tmux server socket from its session name, then
# send through the one safe cross-socket primitive (prechecks the session on
# that socket, sanitizes, two-step send, logs a send_miss on a miss). Tolerant:
# an unresolvable peer yields an empty socket → bot_tmux_send logs the miss and
# we exit 1 below, rather than the resolver's guard crashing the dispatcher.
WORKER_SOCKET="$(tmux_socket_for_session "$WORKER_SESSION" 2>/dev/null || true)"
if ! bot_tmux_send "$WORKER_SOCKET" "$WORKER_SESSION" "set +H; $MESSAGE"; then
    echo "dispatch: session '$WORKER_SESSION' could not be reached on socket '$WORKER_SOCKET'" >&2
    exit 1
fi
