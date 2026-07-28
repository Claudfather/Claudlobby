#!/usr/bin/env bash
# tests/test_spin_down_receipt.sh — spin-down teardown-receipt contract.
# Real spin-down-bot.sh + stub systemctl/tmux: asserts every teardown leaves a
# durable record of WHO tore the bot down and WHY, in a ledger that survives the
# bot directory it documents (the --purge case), with absence recorded
# explicitly rather than left ambiguous. Runs hermetically under env -i so the
# real fleet's units, sockets and state can never be reached. Standalone bash
# (not pytest-collected); runs under macOS /bin/bash (3.2).
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
        bash "$LIB_DIR/spin-down-bot.sh" "$bdir" "$@" 2>&1 || true
}

# The LAST teardown row in the fleet ledger. Deliberately reads the fleet
# ledger (not the bot dir) — that is the property under test.
receipt_row() {
    grep -h '"type":"bot_teardown_started"' "$ROOT/state/events"/fleet-*.jsonl 2>/dev/null | tail -1
}
# field <row> <key> — a real JSON parse, so key order in the emitted payload is
# never load-bearing and an escaped quote in free text cannot truncate a read.
field() { ROW="$1" K="$2" python3 -c 'import json,os;d=json.loads(os.environ["ROW"]);print(d.get(os.environ["K"], d.get("data",{}).get(os.environ["K"],"")))' 2>/dev/null || true; }
reset() { rm -rf "$ROOT"; }

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
assert_eq "receipt is logged before any teardown leg" "yes" \
    "$(printf '%s\n' "$out" | head -1 | grep -q 'receipt:' && echo yes || echo no)"

# --- a bot with no bot.conf is already reaped: no phantom receipt ------------
reset
mkdir -p "$ROOT/local/f1/runtime/bots/ghost"
env -i PATH="$T/bin:/usr/bin:/bin" HOME="$T" CLAUDLOBBY_ROOT="$ROOT" USER=testuser \
    bash "$LIB_DIR/spin-down-bot.sh" "$ROOT/local/f1/runtime/bots/ghost" >/dev/null 2>&1 || true
assert_eq "no receipt for a bot that was never there" "" "$(receipt_row)"

echo ""
echo "=== $PASS/$TOTAL passed ==="
[ "$FAIL" -eq 0 ] || exit 1
