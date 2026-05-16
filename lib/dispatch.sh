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

WORKER_SESSION="${1:?Usage: dispatch.sh <worker-session> <message...>}"
shift
MESSAGE="$*"

if ! check_tmux_session "$WORKER_SESSION"; then
    echo "dispatch: session '$WORKER_SESSION' does not exist" >&2
    exit 1
fi

MESSAGE="$(sanitize_tmux_input "$MESSAGE")"

"$_TMUX_BIN" send-keys -t "$WORKER_SESSION" "set +H; $MESSAGE"
sleep 0.3
"$_TMUX_BIN" send-keys -t "$WORKER_SESSION" Enter
