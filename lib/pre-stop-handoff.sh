#!/bin/bash
# Pre-stop script: capture session context before systemd kills the bot
# Called by systemd ExecStop before the tmux session is terminated.
#
# Add to your .service file:
#   ExecStop=/path/to/claudlobby/lib/pre-stop-handoff.sh /path/to/bot/dir

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT_DIR="${1:?Usage: pre-stop-handoff.sh /path/to/bot/dir}"
MODE="${2:-}"
case "$MODE" in ''|--explicit) ;; *) echo "pre-stop-handoff.sh: unsupported mode" >&2; exit 2 ;; esac

# On the stop path, clean up .tmux-env on ALL exit paths (including early
# returns and the socket-resolution guard below). It holds resolved secrets
# written by start-bot.sh, so install the trap before anything can exit early.
# A standalone handoff leaves the running session in place; only ExecStop owns
# deletion of this launch-time secret file.
if [ -z "$MODE" ]; then trap 'rm -f "$BOT_DIR/.tmux-env"' EXIT; fi
install_error_trap "$BOT_DIR"
load_bot_conf "$BOT_DIR"
TMUX_SESSION="$(tmux_session_name "$BOT_DIR")"
# Per-bot tmux server socket (see start-bot.sh) — handoff is sent on the bot's
# OWN server. Fail fast (like start-bot.sh) if it can't be resolved rather than
# aborting silently on errexit.
TMUX_SOCKET="$(tmux_socket_for_bot "$BOT_DIR")" || {
    echo "pre-stop-handoff.sh: cannot resolve tmux socket for $BOT_DIR (check BOT_SERVICE in bot.conf)" >&2
    exit 1
}

# clauDNA's /claudna:session handoff writes to <cwd>/.claude/session.md,
# where cwd is the bot's runtime dir (start-bot.sh `cd "$BOT_DIR"` before tmux).
HANDOFF_FILE="$BOT_DIR/.claude/session.md"
if [ "$MODE" = --explicit ]; then
    [ ! -L "$HANDOFF_FILE" ] || { echo "handoff-unavailable"; exit 3; }
    _handoff_before=""
    if [ -e "$HANDOFF_FILE" ]; then
        _handoff_before=$(cksum < "$HANDOFF_FILE") || { echo "handoff-unavailable"; exit 3; }
    fi
fi

# If a fresh handoff was written in the last 5 minutes, skip
if [ -f "$HANDOFF_FILE" ]; then
    AGE=$(( $(date +%s) - $(stat_mtime "$HANDOFF_FILE" 2>/dev/null || echo 0) ))
    if [ "$AGE" -lt 300 ]; then
        if [ "$MODE" = --explicit ]; then echo "handoff-skipped:recent";
        else echo "Recent handoff exists ($AGE seconds old), skipping"; fi
        exit 0
    fi
fi

# Try to trigger a handoff via the running session
_session_was_present=0
if check_tmux_session "$TMUX_SESSION" "$TMUX_SOCKET"; then
    _session_was_present=1
    # Through pane_send_verified, not a bare send-keys: a swallowed Enter here
    # presents as the 30s timeout below with the session context silently lost,
    # which is the exact failure the verify-retry exists to catch. Verbatim send
    # (no sanitize, no 'set +H;') because the payload is a slash command.
    # Capability gate (#1163), the same predicate start-bot uses for resume —
    # one helper, not a second mechanism. This call site matters MORE than the
    # boot one: it runs on the systemd ExecStop path, so under
    # `plugins.include_defaults: false` the old hardcode fired an unresolvable
    # command at SHUTDOWN, the one moment the handoff is all that stands between
    # a restart and lost context.
    #
    # Fail OPEN, identically: only a positive finding of absence suppresses the
    # send. A wasted keystroke into a dying pane is visible and harmless; not
    # sending when we should have loses the session silently.
    _HANDOFF_CMD="${SESSION_HANDOFF_COMMAND-$_SESSION_HANDOFF_COMMAND_DEFAULT}"
    # `|| true` INSIDE the substitution and the status judged by VALUE (the
    # predicate's own table: rc 0 = available | unverifiable, rc 1 = no-command
    # | provider-absent): on bash 3.2 a function returning 1 as the last
    # command of a `$( )` fires the inherited ERR trap even under an `if` —
    # one phantom `script_error` per bot per restart, measured on the flip's
    # rolling restart (the #1460 class).
    _handoff_status="$(session_command_status "$_HANDOFF_CMD" "$BOT_DIR" || true)"
    case "$_handoff_status" in
        available|unverifiable) _handoff_inject=1 ;;
        *) _handoff_inject=0 ;;
    esac
    if [ "$_handoff_inject" -eq 1 ]; then
        if [ "$MODE" = --explicit ]; then
            pane_send_verified "$TMUX_SOCKET" "$TMUX_SESSION" "$_HANDOFF_CMD" || {
                echo "handoff-unavailable"; exit 3;
            }
        else
            pane_send_verified "$TMUX_SOCKET" "$TMUX_SESSION" "$_HANDOFF_CMD" || true
        fi
    else
        if [ "$MODE" = --explicit ]; then
            echo "HANDOFF SKIP — no session-handoff capability [$_handoff_status]" >&2
        else
            echo "HANDOFF SKIP — no session-handoff capability [$_handoff_status]; stopping without a fresh handoff, last one (if any) left at $HANDOFF_FILE" >&2
        fi
        if [ -z "$MODE" ]; then
            emit_fleet_event "handoff_skipped" "pre-stop" \
                "{\"reason\":\"$_handoff_status\",\"handoff\":\"$HANDOFF_FILE\"}" || true
        fi
        [ "$MODE" != --explicit ] || echo "handoff-skipped:capability"
        exit 0
    fi
    # Wait up to 30 seconds for handoff to complete
    for _ in $(seq 1 30); do
        if [ -f "$HANDOFF_FILE" ]; then
            AGE=$(( $(date +%s) - $(stat_mtime "$HANDOFF_FILE" 2>/dev/null || echo 0) ))
            if [ "$AGE" -lt 60 ]; then
                if [ "$MODE" = --explicit ]; then
                    [ ! -L "$HANDOFF_FILE" ] || { echo "handoff-unavailable"; exit 3; }
                    _handoff_after=$(cksum < "$HANDOFF_FILE") || { echo "handoff-unavailable"; exit 3; }
                    if [ "$_handoff_after" != "$_handoff_before" ]; then
                        echo "handoff-saved"
                        exit 0
                    fi
                else
                    echo "Handoff completed"
                    exit 0
                fi
            fi
        fi
        sleep 1
    done
    if [ -z "$MODE" ]; then echo "Handoff timed out after 30s"; fi
fi
if [ "$MODE" = --explicit ]; then
    if [ "$_session_was_present" -eq 0 ]; then
        echo "handoff-skipped:no-session"
        exit 0
    fi
    echo "handoff-unavailable"
    exit 3
fi

# Stop remains best-effort: timeout and absent-session fall-throughs return 0
# so a restart proceeds. Explicit mode above refuses an unverified save.
exit 0
