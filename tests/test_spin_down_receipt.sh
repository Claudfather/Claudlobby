#!/usr/bin/env bash
# tests/test_spin_down_receipt.sh — spin-down teardown-receipt contract.
# Real spin-down-bot.sh + stub systemctl/tmux: asserts every teardown leaves a
# durable record of WHO tore the bot down and WHY, in a ledger that survives the
# bot directory it documents (the --purge case), with absence recorded
# explicitly rather than left ambiguous. Also pins the rollout contract -- the
# whole thing stays DORMANT until a fleet arms it, since lib/ is a shared
# install where a root-pull would otherwise make this live on a destructive
# door uncanaried -- and that a fault in the receipt can never cost the
# teardown. Runs hermetically under env -i so the real fleet's units, sockets
# and state can never be reached. Standalone bash (not pytest-collected); runs
# under macOS /bin/bash (3.2).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
PASS=0; FAIL=0; TOTAL=0
assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS + 1)); else echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1)); fi
}

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin"
# The receipt's record is the plane (F18 closure R1); the CLI rung is stood in
# for by tests/plane_capture_cli.sh, which renders each batch as the legacy row.
CAPTURE="$T/plane-capture.jsonl"; : > "$CAPTURE"
# Stubs: nothing may reach the host's real systemd or any tmux server.
printf '#!/bin/bash\nexit 0\n' > "$T/bin/systemctl"
printf '#!/bin/bash\nexit 0\n' > "$T/bin/tmux"
printf '#!/bin/bash\nexit 0\n' > "$T/bin/launchctl"
chmod +x "$T/bin/systemctl" "$T/bin/tmux" "$T/bin/launchctl"

ROOT="$T/root"

# spin_down <bot> [script flags...] → run a real teardown of a freshly made bot
# dir. Flags go to the SCRIPT; set SPINDOWN_ACTOR in the caller's environment to
# exercise the actor override. Each call rebuilds the bot so cases cannot leak.
spin_down() {
    local bot="$1"; shift
    local bdir="$ROOT/local/f1/runtime/bots/$bot"
    mkdir -p "$bdir"
    printf 'export BOT_NAME=%s\nexport BOT_SERVICE=t.p.%s\nexport TMUX_SOCKET=t.p.%s\nexport FLEET_STATE_PATH=%s\n' \
        "$bot" "$bot" "$bot" "$ROOT/state/fleet-state.json" > "$bdir/bot.conf"
    env -i PATH="$T/bin:/usr/bin:/bin" HOME="$T" CLAUDLOBBY_ROOT="$ROOT" USER=testuser \
        FLEET_NAME=f1 SPINDOWN_ACTOR="${SPINDOWN_ACTOR:-}" \
        SPINDOWN_RECEIPT_ENABLED="${SPINDOWN_RECEIPT_ENABLED-1}" \
        PLANE_EMIT_CLI="$SCRIPT_DIR/plane_capture_cli.sh" PLANE_CAPTURE="$CAPTURE" PLANE_SOCKET="$T/no.sock" \
        bash "$LIB_DIR/spin-down-bot.sh" "$bdir" "$@" 2>&1 || true
}

# The LAST teardown row the plane received. Deliberately NOT anything under
# the bot dir — the record surviving the bot is the property under test.
receipt_row() {
    grep -h '"type":"bot_teardown_started"' "$CAPTURE" 2>/dev/null | tail -1
}
# field <row> <key> — a real JSON parse, so key order in the emitted payload is
# never load-bearing and an escaped quote in free text cannot truncate a read.
field() { ROW="$1" K="$2" python3 -c 'import json,os;d=json.loads(os.environ["ROW"]);print(d.get(os.environ["K"], d.get("data",{}).get(os.environ["K"],"")))' 2>/dev/null || true; }
reset() { rm -rf "$ROOT"; : > "$CAPTURE"; }

echo "=== spin-down teardown-receipt contract ==="

# --- a plain teardown records actor, action, and explicit absences ------------
reset
spin_down bot1 >/dev/null
row="$(receipt_row)"
assert_eq "receipt records the door invoked" "spin-down"   "$(field "$row" action)"
assert_eq "actor falls back to user@host"    "testuser"    "$(field "$row" actor | cut -d@ -f1)"
assert_eq "receipt records the fleet"        "f1"          "$(field "$row" fleet)"
# Absence is recorded, never omitted: a missing field cannot be told apart from
# a field nobody set, and "none" is a positive decommission claim a default
# must not make on the operator's behalf.
assert_eq "unset reason is explicit"          "unspecified" "$(field "$row" reason)"
assert_eq "unset expected_return is explicit" "unspecified" "$(field "$row" expected_return)"

# --- supplied intent is carried verbatim -------------------------------------
reset
spin_down bot2 --reason "RAM pressure" --expected-return "2026-07-29T09:00:00-04:00" >/dev/null
row="$(receipt_row)"
assert_eq "reason recorded"          "RAM pressure"              "$(field "$row" reason)"
assert_eq "expected_return recorded" "2026-07-29T09:00:00-04:00" "$(field "$row" expected_return)"

# --- SPINDOWN_ACTOR lets a bot-driven teardown name itself -------------------
reset
SPINDOWN_ACTOR=clog spin_down bot3 >/dev/null
assert_eq "actor override wins over user@host" "clog" "$(field "$(receipt_row)" actor)"

# --- the door is the intent: --purge is a different door and says so ---------
reset
spin_down bot4 --purge >/dev/null
row="$(receipt_row)"
assert_eq "purge recorded as its own action" "spin-down --purge" "$(field "$row" action)"
# The whole point of the fleet ledger: --purge deletes the bot dir, so a receipt
# written under it would die with the transaction it documents.
assert_eq "bot dir is gone" "gone" \
    "$([ -d "$ROOT/local/f1/runtime/bots/bot4" ] && echo present || echo gone)"
assert_eq "receipt outlived the purged bot dir" "bot4" "$(field "$row" bot)"

# --- ordering: the receipt precedes the teardown legs ------------------------
# A crash mid-teardown must still leave a record. Asserted on the real stdout
# ordering, since the receipt and the legs are logged by the same script.
reset
out="$(spin_down bot5)"
# The script's OWN first line (the plane shim discloses its rung fallbacks on
# stderr ahead of it under 2>&1; those are not legs).
assert_eq "receipt is logged before any teardown leg" "yes" \
    "$(printf '%s\n' "$out" | grep 'spin-down\[' | head -1 | grep -q 'receipt:' && echo yes || echo no)"

# --- a bot with no bot.conf is already reaped: no phantom receipt ------------
reset
mkdir -p "$ROOT/local/f1/runtime/bots/ghost"
env -i PATH="$T/bin:/usr/bin:/bin" HOME="$T" CLAUDLOBBY_ROOT="$ROOT" USER=testuser \
    bash "$LIB_DIR/spin-down-bot.sh" "$ROOT/local/f1/runtime/bots/ghost" >/dev/null 2>&1 || true
assert_eq "no receipt for a bot that was never there" "" "$(receipt_row)"

# --- the rollout contract: dormant until a fleet arms it ---------------------
# lib/ is a SHARED install -- every bot on every fleet reads this same file, so
# this change cannot be staged per-bot. Default-on would mean a routine
# root-pull for something unrelated silently activates new behavior on the
# DESTRUCTIVE teardown door. It must do nothing until a fleet opts in.
reset
SPINDOWN_RECEIPT_ENABLED="" spin_down bot6 --reason "should not be recorded" >/dev/null
assert_eq "unarmed fleet writes NO receipt" "" "$(receipt_row)"
assert_eq "unarmed fleet does not even touch the plane" "no" \
    "$([ -s "$CAPTURE" ] && echo yes || echo no)"
# Dormant means dormant only for the RECORD -- the teardown itself is unchanged,
# so a dormant fleet is never left with a bot that failed to reap.
assert_eq "unarmed teardown still reaps supervision" "yes" \
    "$(SPINDOWN_RECEIPT_ENABLED="" spin_down bot7 | grep -q 'stopped + disabled + removed' && echo yes || echo no)"

# Only an explicit "1" arms it -- a stray truthy-looking value must not.
for v in 0 yes true ""; do
    reset
    SPINDOWN_RECEIPT_ENABLED="$v" spin_down bot8 >/dev/null
    assert_eq "SPINDOWN_RECEIPT_ENABLED='$v' stays dormant" "" "$(receipt_row)"
done
reset
SPINDOWN_RECEIPT_ENABLED=1 spin_down bot9 >/dev/null
assert_eq "SPINDOWN_RECEIPT_ENABLED=1 arms it" "bot9" "$(field "$(receipt_row)" bot)"

echo ""
# --- the record must never cost the teardown --------------------------------
# emit_teardown_receipt runs BEFORE the destructive legs, so under set -e any
# non-zero command inside it aborts the script and leaves standing a bot the
# operator asked to tear down. Fault-injected on the one surface with no
# fallback behind it: hostname, absent on a trimmed PATH or a minimal env.
reset
printf '#!/bin/bash\nexit 127\n' > "$T/bin/hostname"; chmod +x "$T/bin/hostname"
out="$(spin_down bot10)"
assert_eq "a broken hostname still tears the bot down" "yes" \
    "$(printf '%s\n' "$out" | grep -q 'stopped + disabled + removed' && echo yes || echo no)"
assert_eq "and the receipt degrades rather than failing" "unknown" \
    "$(field "$(receipt_row)" actor | cut -d@ -f2)"
rm -f "$T/bin/hostname"

echo ""
echo "=== $PASS/$TOTAL passed ==="
[ "$FAIL" -eq 0 ] || exit 1
