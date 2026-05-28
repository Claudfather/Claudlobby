#!/bin/bash
# Inter-bot communication — manager dispatches a task to a worker
# Usage: dispatch.sh <worker-session> <task message...>
#
# Sibling of report-back.sh, but going the other direction (manager → worker).
# Encapsulates the two-step text → C-m send pattern required to avoid the
# Claude Code TUI Enter race: a single `send-keys "<text>" Enter` invocation
# sometimes lands the text in the input buffer but drops the trailing Enter,
# leaving the prompt unsubmitted. Sending the text and the literal CR keycode
# (`C-m`, kbd 0x0d) in separate send-keys calls is the fix — the `Enter` tmux
# alias is more vulnerable to the post-SIGWINCH race than the raw keycode.
# See lib/start-bot.sh for the same pattern on the boot path.
#
# Example:
#   dispatch.sh wrenn "Add rate-limit middleware to /api/login --repo backend"

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

WORKER="${1:?Usage: dispatch.sh <worker-session> <message...>}"
shift
MESSAGE="$*"

if [ -z "$MESSAGE" ]; then
    echo "dispatch: empty message" >&2
    exit 1
fi

if ! check_tmux_session "$WORKER"; then
    echo "dispatch: no tmux session named '$WORKER' — is the bot up?" >&2
    exit 1
fi

# Sanitize message for tmux safety (same helper report-back.sh uses)
MESSAGE="$(sanitize_tmux_input "$MESSAGE")"

# Two-step send: text, then literal C-m. See lib/start-bot.sh for rationale.
"$_TMUX_BIN" send-keys -t "$WORKER" "$MESSAGE"
"$_TMUX_BIN" send-keys -t "$WORKER" C-m

echo "dispatch: sent to $WORKER"
