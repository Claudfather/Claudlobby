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

# A leading slash command must reach the pane as its FIRST characters so Claude
# Code slash-command recognition fires; the set +H; history-expansion guard would
# swallow it (the live briefing-cron bug; cf. keepalive.sh send_reload_command,
# start-bot.sh STARTUP_PROMPT). Match a real command token — an alpha-led word
# terminated by whitespace-or-end — NOT any leading slash, so file-path prose
# (/home/crog, /etc/hosts) and leading-whitespace keep the guard. POSIX
# [[:space:]] class (not the GNU escape) for bash 3.2 / macOS /bin/bash.
# Also keep the guard when the payload contains a bang: prose that starts with a
# slash-word yet carries a bang (/reports is missing) matches the token but still
# needs the guard, and a rare genuine slash command with a bang in its args keeps
# it too (amended F6). The bang-free glob test is bash-3.2 safe.
# ROLLBACK: replace this if/else with the one line: PAYLOAD="set +H; $MESSAGE"
if [[ "$MESSAGE" =~ ^/[A-Za-z][A-Za-z0-9:_-]*([[:space:]]|$) ]] && [[ "$MESSAGE" != *"!"* ]]; then
    PAYLOAD="$MESSAGE"
else
    PAYLOAD="set +H; $MESSAGE"
fi

if ! bot_tmux_send "$WORKER_SOCKET" "$WORKER_SESSION" "$PAYLOAD"; then
    echo "dispatch: session '$WORKER_SESSION' could not be reached on socket '$WORKER_SOCKET'" >&2
    exit 1
fi
