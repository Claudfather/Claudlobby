#!/usr/bin/env bash
# rehearse-debounce-recipient.sh — prove #831 on real tmux sessions: a debounced
# FLEET-PULSE alert must still reach a manager that restarted mid-episode.
#
# A marker changing shape is not the property. The property is a human-facing
# push arriving in the session that exists NOW, so this drives the real
# fleet-pulse.sh against a real (throwaway) bot whose real condition is still
# unresolved, restarts the real manager session, and counts what that session
# was submitted.
#
# Red on the pre-fix code: step 3 finds nothing submitted, because the marker says
# "already told someone" and cannot say the someone is gone.
#
# Isolation (#846): private tmux sockets under a throwaway TMUX_TMPDIR, a
# throwaway CLAUDLOBBY_ROOT, and a fake escalation chat id — the escalation leg
# posts to Telegram directly and would otherwise page the real operator.
# Nothing here touches a live fleet, and no live socket is opened.
#
# Usage: harness/rehearse-debounce-recipient.sh

set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../claudlobby/_runtime_scripts" && pwd)"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not available"; exit 0; }
command -v python3 >/dev/null 2>&1 || { echo "SKIP: python3 not available"; exit 0; }
# The manager is the input-box stand-in, not a bare `sleep` pane: a push presses
# Enter only once the box shows it (#1236), and a pane that draws no box is,
# correctly, never submitted to.
MGR_BOX="$(printf '%q %q' "$(type -P python3)" "$REPO/tests/fixtures/input-box-stub.py")"
# Test seam: start each manager's stand-in N seconds late, so a pulse sent at once
# would reach its pane before the box is drawn (#2136). Unset or empty, the
# command is unchanged.
if [ -n "${REHEARSE_MANAGER_START_DELAY:-}" ]; then
    MGR_BOX="sleep $(printf '%q' "$REHEARSE_MANAGER_START_DELAY"); exec $MGR_BOX"
    echo "seam: each manager's stand-in starts $REHEARSE_MANAGER_START_DELAY s late (#2136)"
fi

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
# every tick, for as long as we leave it that way. That is the "episode". A
# second worker names a service with no installed unit and no stop record, so
# it raises unit_missing every tick (#2243): the second alert type, from its own
# fault, pushed to the same manager.
cat > "$BOTS/w1/bot.conf" <<EOF
export MANAGER_TMUX=$MGR
export MANAGER_TMUX_SOCKET=$MGR_SOCK
export TMUX_SOCKET=rdrw$$
EOF
mkdir -p "$BOTS/w2"
cat > "$BOTS/w2/bot.conf" <<EOF
export MANAGER_TMUX=$MGR
export MANAGER_TMUX_SOCKET=$MGR_SOCK
export TMUX_SOCKET=rdrv$$
export BOT_SERVICE=rdrv$$
EOF

# The manager's stand-in logs each line it is submitted (--log), and each start
# empties the log, as a restarted manager is a new, empty session. The pane is
# no record of a submit: text there can sit unsubmitted in the box, or be the
# tty's echo of keys typed before the stand-in read them.
# A start waits for the box, through lib-common's own wait, because the stand-in
# draws it only once in raw mode, and entering raw mode discards keys typed
# before it. A pulse sent earlier is never submitted, and the debounce then
# marks it sent (#2136). lib-common is sourced in the substitution's subshell
# only, since sourcing it arms set -e and its own EXIT trap.
MGR_LOG="$ROOT/mgr.log"
start_manager () {
    : > "$MGR_LOG"
    tmux -L "$MGR_SOCK" new-session -d -s "$MGR" "$MGR_BOX --log $(printf '%q' "$MGR_LOG")"
    # shellcheck source=/dev/null
    check "the manager drew its box" drawn "$(. "$LIB_DIR/lib-common.sh" &&
        PANE_READY_TICKS=100 PANE_READY_POLL_S=0.2 pane_await_input_box "$MGR_SOCK" "$MGR")"
}
# Counted PER ALERT TYPE: one tick legitimately pushes two here (one from each
# worker), so a bare total would conflate them.
push_count ()    { grep -c "\\[FLEET-PULSE\\].*$1" "$MGR_LOG" || true; }

run_pulse () {
    env CLAUDLOBBY_ROOT="$ROOT" TMUX_TMPDIR="$TMUX_TMPDIR" \
        FLEET_PULSE_ESCALATION_CHAT_ID="-100999" \
        TELEGRAM_GROUP_CHAT_ID="" FLEET_PULSE_ESCALATION_STATE_DIR="$ROOT/tg" \
        bash "$LIB_DIR/fleet-pulse.sh" "$FLEET" >"$ROOT/pulse.log" 2>&1 || true
}

echo "=== #831 rehearsal: does a FLEET-PULSE alert survive a manager restart? ==="

echo "--- 1. episode opens: manager is up, condition fires ---"
start_manager
run_pulse
check "  session_missing pushed once" 1 "$(push_count session_missing)"
check "  unit_missing pushed once"    1 "$(push_count unit_missing)"

echo "--- 2. control: same manager, same episode -> debounce still debounces ---"
run_pulse
run_pulse
check "3 ticks, still one session_missing to the SAME instance" 1 "$(push_count session_missing)"
check "3 ticks, still one unit_missing to the SAME instance"    1 "$(push_count unit_missing)"

echo "--- 3. the bug: manager restarts, condition STILL unresolved ---"
tmux -L "$MGR_SOCK" kill-session -t "$MGR" 2>/dev/null || true
start_manager
run_pulse
check "restarted manager receives session_missing (THE PROPERTY)" 1 "$(push_count session_missing)"
check "restarted manager receives unit_missing too"              1 "$(push_count unit_missing)"

echo "--- 4. resolution still re-arms: close the condition, then reopen it ---"
tmux -L "rdrw$$" new-session -d -s "w1" "sleep 600" 2>/dev/null || true
run_pulse                                   # condition resolved -> debounce_clear
tmux -L "rdrw$$" kill-server 2>/dev/null || true
run_pulse                                   # condition reopens -> must fire again
check "a resolved-then-reopened condition fires again" 2 "$(push_count session_missing)"

echo
echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ] || exit 1
