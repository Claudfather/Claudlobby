#!/usr/bin/env bash
# rehearse-held-push.sh — prove #2120 on real tmux sessions: a FLEET-PULSE push
# never types into a manager's box that holds text, a push the box did not take
# keeps its alert's window open, and a box that takes no input costs one wait per
# floor, not one per sweep or per alert.
#
# notify_manager discarded the send's status (`bot_tmux_send ... || true`), so a
# push typed and never submitted (rc 3, #1236) returned 0, and debounce_notify
# set the marker: the alert went quiet though nobody had read it (#900: a send
# that reached nobody must not buy the window). With the window kept open, an
# unbounded re-page would type a copy of the alert, and press its Enters, into
# the same box every sweep. So (dara's decision on vera's review): a box that
# holds text gets no push, the alert's record is left to the escalation, and
# after a push the box did not take, that manager gets no push until a floor
# (FLEET_PULSE_HELD_PUSH_FLOOR_S, 30 min) lapses. The floor is that manager
# instance's: a restarted manager is a new box and is not held back by it.
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

# Each sweep records its alerts on the plane whatever the push did; with no daemon
# in this throwaway root, plane-emit.sh stages them under it. That record is what
# the escalation reads, so an alert whose push is skipped is still carried there.
recorded ()      { cat "$ROOT"/state/plane/staged/* 2>/dev/null | grep -c "$1" || true; }
held_skips ()    { grep -c "box holds text" "$ROOT/pulse.log" || true; }
floor_skips ()   { grep -c "did not take a push less than" "$ROOT/pulse.log" || true; }
# The box's own line: the last line the stub draws with its prompt.
box_line ()      { mgr_pane | grep '^>' | tail -1; }

# One sweep. The short shown budget keeps a held push to about a second here: the
# property is how many pushes wait, not how long one wait is. The emits are on and
# can only stage in this root: PLANE_SOCKET is unset, so the socket is the root's
# own, which nothing serves. Extra VAR=value arguments go into the sweep's env.
run_pulse () {
    env -u PLANE_EMIT_DISABLED -u PLANE_SOCKET CLAUDLOBBY_ROOT="$ROOT" TMUX_TMPDIR="$TMUX_TMPDIR" \
        PANE_SEND_SHOWN_TICKS=5 FLEET_PULSE_ESCALATION_CHAT_ID="-100999" \
        TELEGRAM_GROUP_CHAT_ID="" FLEET_PULSE_ESCALATION_STATE_DIR="$ROOT/tg" "$@" \
        bash "$LIB_DIR/fleet-pulse.sh" "$FLEET" >"$ROOT/pulse.log" 2>&1 || true
}

echo "=== #2120 rehearsal: no push types into a held box, and a box that takes no input is bounded ==="

echo "--- 1. the manager's box already holds text; two alerts this sweep ---"
start_manager
check "the manager is up and shows its box" shown "$(manager_box)"
tmux -L "$MGR_SOCK" send-keys -t "$MGR" -l "a reply being typed"
sleep 0.5
check "the box holds the manager's own text" "> a reply being typed" "$(box_line)"
seen=$(recorded session_missing)
run_pulse
check "a held box gets no copy of an alert" 0 "$(push_count session_missing)"
check "a held box gets no Enter: its text is still in it" "> a reply being typed" "$(box_line)"
check "both alerts are skipped, not typed" 2 "$(held_skips)"
check "a skipped push leaves session_missing's window open" open "$(window session_alerted)"
check "the alert is still recorded for the escalation" 1 "$(( $(recorded session_missing) - seen ))"

echo "--- 2. the box takes no input: one wait, then the floor ---"
tmux -L "$MGR_SOCK" kill-session -t "$MGR" 2>/dev/null || true
start_manager --deaf
check "the deaf manager is up and shows its box" shown "$(manager_box)"
run_pulse
check "one push waits on the box that takes no input" 1 "$(waits)"
check "the other alert is held back by the floor, not typed" 1 "$(floor_skips)"
check "a held push leaves session_missing's window open" open "$(window session_alerted)"
check "a held push leaves service_down's window open"    open "$(window service_alerted)"

echo "--- 3. within the floor: no wait and no typing ---"
seen=$(recorded session_missing)
run_pulse
check "no push waits within the floor" 0 "$(waits)"
check "both alerts are held back by the floor" 2 "$(floor_skips)"
check "the alert is still recorded within the floor" 1 "$(( $(recorded session_missing) - seen ))"
check "session_missing's window stays open within the floor" open "$(window session_alerted)"

echo "--- 4. the floor lapses and the box takes input: the push goes out ---"
tmux -L "$MGR_SOCK" kill-session -t "$MGR" 2>/dev/null || true
start_manager
check "the restarted manager is up and shows its box" shown "$(manager_box)"
sleep 2
run_pulse FLEET_PULSE_HELD_PUSH_FLOOR_S=1
check "session_missing is pushed and submitted"          1 "$(push_count session_missing)"
check "service_down is pushed and submitted"             1 "$(push_count service_down)"
check "a submitted push closes session_missing's window" closed "$(window session_alerted)"
check "a submitted push closes service_down's window"    closed "$(window service_alerted)"
check "a submitted push clears the floor" absent "$(ls "$ROOT"/state/pulse/held-push.* >/dev/null 2>&1 && echo present || echo absent)"

echo "--- 5. control: a closed window debounces ---"
run_pulse
check "no second session_missing push" 1 "$(push_count session_missing)"
check "no second service_down push"    1 "$(push_count service_down)"

echo "--- 6. the floor belongs to the manager instance: a restarted manager is not held back ---"
# A restart is a new box: #831's recipient token (session_created, pane_pid)
# changes, so both alerts re-fire to it. A deaf one sets a floor; the next one,
# which takes input, must not inherit that floor.
tmux -L "$MGR_SOCK" kill-session -t "$MGR" 2>/dev/null || true
start_manager --deaf
check "a second deaf manager is up and shows its box" shown "$(manager_box)"
run_pulse
check "the alerts re-fire to it, and one push waits on its box" 1 "$(waits)"
check "its other alert is held back by its floor" 1 "$(floor_skips)"
tmux -L "$MGR_SOCK" kill-session -t "$MGR" 2>/dev/null || true
start_manager
check "a manager that takes input replaces it and shows its box" shown "$(manager_box)"
run_pulse
check "the new manager is not held back by its predecessor's floor" 0 "$(floor_skips)"
check "session_missing reaches the new manager" 1 "$(push_count session_missing)"
check "service_down reaches the new manager"    1 "$(push_count service_down)"

echo
echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ] || exit 1
