#!/usr/bin/env bash
# rehearse-debounce-recipient.sh — prove #831 on real tmux sessions: a debounced
# FLEET-PULSE alert must still reach a manager that restarted mid-episode.
#
# A marker changing shape is not the property. The property is a human-facing
# push arriving in the session that exists NOW, so this drives the real
# fleet-pulse.sh against a real (throwaway) bot whose real condition is still
# unresolved, restarts the real manager session, and reads the real pane.
#
# Red on the pre-fix code: step 3 finds an empty pane, because the marker says
# "already told someone" and cannot say the someone is gone.
#
# Isolation (#846): private tmux sockets under a throwaway TMUX_TMPDIR, a
# throwaway CLAUDLOBBY_ROOT, and a fake escalation chat id — the escalation leg
# posts to Telegram directly and would otherwise page the real operator.
# Nothing here touches a live fleet, and no live socket is opened.
#
# Usage: lib/rehearse-debounce-recipient.sh

set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not available"; exit 0; }

ROOT="$(mktemp -d)"
FLEET="rdr$$"
MGR_SOCK="rdrm$$"
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

# A worker whose tmux session does not exist -> Check 1 fires session_missing
# every tick, for as long as we leave it that way. That is the "episode".
cat > "$BOTS/w1/bot.conf" <<EOF
export MANAGER_TMUX=$MGR
export MANAGER_TMUX_SOCKET=$MGR_SOCK
export TMUX_SOCKET=rdrw$$
export BOT_SERVICE=rdrw$$
EOF

start_manager () { tmux -L "$MGR_SOCK" new-session -d -s "$MGR" "sleep 600"; }
mgr_pane ()      { tmux -L "$MGR_SOCK" capture-pane -t "$MGR" -p 2>/dev/null || true; }
# Count pushes rather than clearing between steps: `clear-history` only drops
# scrollback and a `sleep` pane ignores C-l, so a "cleared" pane still shows the
# previous push and every later check would read as a false positive.
# Counted PER ALERT TYPE: one tick legitimately pushes two here (the worker has
# no tmux session AND no real service unit), so a bare total would conflate them.
push_count ()    { printf '%s' "$(mgr_pane)" | grep -c "\\[FLEET-PULSE\\].*$1" || true; }

run_pulse () {
    env CLAUDLOBBY_ROOT="$ROOT" TMUX_TMPDIR="$TMUX_TMPDIR" \
        FLEET_PULSE_ESCALATION_CHAT_ID="-100999" \
        TELEGRAM_GROUP_CHAT_ID="" TELEGRAM_STATE_DIR="$ROOT/tg" \
        bash "$LIB_DIR/fleet-pulse.sh" "$FLEET" >"$ROOT/pulse.log" 2>&1 || true
}

echo "=== #831 rehearsal: does a FLEET-PULSE alert survive a manager restart? ==="

echo "--- 1. episode opens: manager is up, condition fires ---"
start_manager
run_pulse
check "  session_missing pushed once" 1 "$(push_count session_missing)"
check "  service_down pushed once"    1 "$(push_count service_down)"

echo "--- 2. control: same manager, same episode -> debounce still debounces ---"
run_pulse
run_pulse
check "3 ticks, still one session_missing to the SAME instance" 1 "$(push_count session_missing)"
check "3 ticks, still one service_down to the SAME instance"    1 "$(push_count service_down)"

echo "--- 3. the bug: manager restarts, condition STILL unresolved ---"
tmux -L "$MGR_SOCK" kill-session -t "$MGR" 2>/dev/null || true
start_manager                               # a new session IS a new, empty pane
check "restarted manager starts with no push" 0 "$(push_count session_missing)"
run_pulse
check "restarted manager receives session_missing (THE PROPERTY)" 1 "$(push_count session_missing)"
check "restarted manager receives service_down too"              1 "$(push_count service_down)"

echo "--- 4. resolution still re-arms: close the condition, then reopen it ---"
tmux -L "rdrw$$" new-session -d -s "w1" "sleep 600" 2>/dev/null || true
run_pulse                                   # condition resolved -> debounce_clear
tmux -L "rdrw$$" kill-server 2>/dev/null || true
run_pulse                                   # condition reopens -> must fire again
check "a resolved-then-reopened condition fires again" 2 "$(push_count session_missing)"

echo
echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ] || exit 1
