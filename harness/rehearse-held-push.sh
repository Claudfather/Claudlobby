#!/usr/bin/env bash
# rehearse-held-push.sh — prove #2120 on real tmux sessions: a FLEET-PULSE push
# that the manager's box did not take keeps its alert's window open, and such a
# box costs one push wait per sweep, not one per alert.
#
# notify_manager discarded the send's status (`bot_tmux_send ... || true`), so a
# push typed and never submitted (rc 3, #1236) returned 0, and debounce_notify
# set the marker: the alert went quiet though nobody had read it (#900: a send
# that reached nobody must not buy the window). And every push to a box that
# never shows it waited the whole shown budget, ahead of the sweep's Telegram
# escalation.
#
# Red on the pre-fix code: step 1 finds both windows closed and two waits, and
# step 2 finds no push at all.
#
# Isolation as in rehearse-debounce-recipient.sh (#846): private tmux sockets
# under a throwaway TMUX_TMPDIR, a throwaway CLAUDLOBBY_ROOT, and a fake
# escalation chat id. Nothing here touches a live fleet, and no live socket is
# opened.
#
# Usage: harness/rehearse-held-push.sh

set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../claudlobby/_runtime_scripts" && pwd)"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not available"; exit 0; }
command -v python3 >/dev/null 2>&1 || { echo "SKIP: python3 not available"; exit 0; }
# The manager is the input-box stand-in: --deaf draws a box that takes no input,
# so a push never shows there; without it, a push shows and Enter submits it.
STUB="$(printf '%q %q' "$(type -P python3)" "$REPO/tests/fixtures/input-box-stub.py")"

ROOT="$(mktemp -d)"
FLEET="rhp$$"
MGR_SOCK="rhpm$$"
MGR="mgr$$"
BOTS="$ROOT/local/$FLEET/runtime/bots"
export TMUX_TMPDIR="$ROOT/tmx"
mkdir -p "$TMUX_TMPDIR" "$BOTS/w1"

pass=0; fail=0
check () { # <label> <expected> <actual>
    if [ "$2" = "$3" ]; then echo "  PASS: $1"; pass=$((pass+1))
    else echo "  FAIL: $1 (expected '$2', got '$3')"; fail=$((fail+1)); fi
}
cleanup () {
    tmux -L "$MGR_SOCK" kill-server 2>/dev/null || true
    rm -rf "$ROOT"
}
trap cleanup EXIT

# A worker with no tmux session and no real unit: every sweep raises two alerts
# for it, session_missing and service_down, and pushes both to the one manager.
cat > "$BOTS/w1/bot.conf" <<EOF
export MANAGER_TMUX=$MGR
export MANAGER_TMUX_SOCKET=$MGR_SOCK
export TMUX_SOCKET=rhpw$$
export BOT_SERVICE=rhpw$$
EOF

start_manager () { tmux -L "$MGR_SOCK" new-session -d -s "$MGR" "$STUB $*"; }
mgr_pane ()      { tmux -L "$MGR_SOCK" capture-pane -t "$MGR" -p 2>/dev/null || true; }
# The precondition every step rests on: a manager session that is up and shows
# its box. Without it, a push finds no session, returns 0 and closes the window,
# which reads as the very result a held push is tested for.
manager_box ()   {
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        # capture-pane trims trailing spaces, so the "> " prompt reads as ">".
        case "$(mgr_pane)" in *">"*) echo shown; return ;; esac
        sleep 0.5
    done
    echo missing
}
push_count ()    { printf '%s' "$(mgr_pane)" | grep -c "\\[FLEET-PULSE\\].*$1" || true; }
# The debounce marker is the window: present, the alert stays quiet until the
# re-notify age; absent, the next sweep fires it again.
window ()        { if [ -f "$ROOT/state/pulse/w1.$1" ]; then echo closed; else echo open; fi; }
# Each push that waited out the shown budget says so on the sweep's stderr.
waits ()         { grep -c "never showed the typed payload" "$ROOT/pulse.log" || true; }

# One sweep. The short shown budget keeps a held push to about a second here:
# the property is how many pushes wait, not how long one wait is.
run_pulse () {
    env CLAUDLOBBY_ROOT="$ROOT" TMUX_TMPDIR="$TMUX_TMPDIR" PANE_SEND_SHOWN_TICKS=5 \
        FLEET_PULSE_ESCALATION_CHAT_ID="-100999" \
        TELEGRAM_GROUP_CHAT_ID="" FLEET_PULSE_ESCALATION_STATE_DIR="$ROOT/tg" \
        bash "$LIB_DIR/fleet-pulse.sh" "$FLEET" >"$ROOT/pulse.log" 2>&1 || true
}

echo "=== #2120 rehearsal: is a FLEET-PULSE push the manager never took counted as delivered? ==="

echo "--- 1. the manager's box takes no input; two alerts this sweep ---"
start_manager --deaf
check "the manager is up and shows its box" shown "$(manager_box)"
run_pulse
check "a held push leaves session_missing's window open" open "$(window session_alerted)"
check "a held push leaves service_down's window open"    open "$(window service_alerted)"
check "one push waits on the held box, not one per alert" 1 "$(waits)"

echo "--- 2. still held: the next sweep pages again ---"
run_pulse
check "the next sweep pushes again"         1 "$(waits)"
check "the held manager is still up"         shown "$(manager_box)"
check "session_missing's window stays open" open "$(window session_alerted)"

echo "--- 3. the box takes input again: a submitted push closes the window ---"
tmux -L "$MGR_SOCK" kill-session -t "$MGR" 2>/dev/null || true
start_manager
check "the restarted manager is up and shows its box" shown "$(manager_box)"
run_pulse
check "session_missing is pushed and submitted"          1 "$(push_count session_missing)"
check "service_down is pushed and submitted"             1 "$(push_count service_down)"
check "a submitted push closes session_missing's window" closed "$(window session_alerted)"
check "a submitted push closes service_down's window"    closed "$(window service_alerted)"

echo "--- 4. control: a closed window debounces ---"
run_pulse
check "no second session_missing push" 1 "$(push_count session_missing)"
check "no second service_down push"    1 "$(push_count service_down)"

echo
echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ] || exit 1
