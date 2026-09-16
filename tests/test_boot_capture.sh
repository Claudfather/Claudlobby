#!/usr/bin/env bash
# tests/test_boot_capture.sh — the boot-capture recorder contract (#1265).
#
# What is under test is the property a single-pass recorder cannot have: that a
# bot is recorded at its FIRST OBSERVATION, so the perishable facts survive the
# keepalive restart that rewrites them ~60s later. Everything else here exists
# to stop that property being true by accident.
#
# Real tmux sessions on isolated sockets, real composed units carrying real
# rungs, a real scratch CLAUDLOBBY_ROOT. Only the boot instant is injected,
# because a reboot cannot be staged.
#
# Hermetic: scratch root, private tmux sockets, plane emission disabled — it can
# neither read the real estate nor be perturbed by one.
#
# Standalone bash (also driven by tests/test_boot_capture.py so CI collects it);
# runs under macOS /bin/bash (3.2).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BC="$SCRIPT_DIR/../lib/boot-capture.sh"
LIBC="$SCRIPT_DIR/../lib/lib-common.sh"
PASS=0; FAIL=0; TOTAL=0
SOCKETS=""

assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS + 1))
    else echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1)); fi
}
assert_contains() {
    TOTAL=$((TOTAL + 1)); local d="$1" n="$2" h="$3"
    case "$h" in *"$n"*) echo "  PASS: $d"; PASS=$((PASS + 1)) ;;
                 *) echo "  FAIL: $d (missing '$n')"; FAIL=$((FAIL + 1)) ;; esac
}
assert_absent() {
    TOTAL=$((TOTAL + 1)); local d="$1" n="$2" h="$3"
    case "$h" in *"$n"*) echo "  FAIL: $d (unexpected '$n')"; FAIL=$((FAIL + 1)) ;;
                 *) echo "  PASS: $d"; PASS=$((PASS + 1)) ;; esac
}

TMP="$(mktemp -d "${TMPDIR:-/tmp}/bccap.XXXXXX")" || exit 3
# Sockets get their OWN short-path dir, reaped in cleanup below. Two constraints
# that pull in opposite directions:
#
#   REAP — tmux defaults to /tmp/tmux-<uid>/, where kill-server leaves the file
#   behind, so repeated runs litter a directory several fleet tools enumerate to
#   find bots: a growing false-positive surface. Reap as a first-class verb, the
#   coldstart-harness.sh convention.
#
#   LENGTH — but this must NOT live under $TMP. An AF_UNIX path is capped near
#   104 bytes, and test_sh_suites.py runs every bash suite with TMPDIR set to a
#   pytest tmp_path; nesting the sockets under that blows the cap, every
#   new-session fails silently, and the harness reports bots that were never
#   captured. Measured: passes standalone (TMPDIR unset), fails under pytest.
#
# So: /tmp directly, which is also where tmux itself puts its sockets.
TMUX_TMPDIR="$(mktemp -d /tmp/bctmux.XXXXXX)" || exit 3
export TMUX_TMPDIR
cleanup() {
    for s in $SOCKETS; do tmux -L "$s" kill-server 2>/dev/null; done
    rm -rf "$TMP" "$TMUX_TMPDIR"
}
trap cleanup EXIT

# ── Fixture builders ────────────────────────────────────────────────────────
# A fleet whose bots carry a REAL composed unit with a REAL rung: the ladder end
# has to be read from these, never assumed, so the fixtures are what make a
# hardcoded stagger fail.
mkroot() {
    local root="$1"; shift
    mkdir -p "$root/local/testfleet/runtime/bots"
    { echo "fleet:"; echo "  name: testfleet"; echo "bots:"; } > "$root/local/testfleet/fleet.yaml"
    while [ $# -gt 0 ]; do
        local bot="${1%%:*}" rung="${1##*:}"; shift
        echo "  $bot:" >> "$root/local/testfleet/fleet.yaml"
        echo "    role: worker" >> "$root/local/testfleet/fleet.yaml"
        local d="$root/local/testfleet/runtime/bots/$bot"
        mkdir -p "$d/data"
        printf 'BOT_SERVICE=%s\nBOT_NAME=%s\nFLEET_NAME=testfleet\n' "bc-$$-$bot" "$bot" > "$d/bot.conf"
        if [ "$rung" != "none" ]; then
            { echo "[Service]"; echo "ExecStartPre=/bin/sleep $rung"; } > "$d/claudlobby.$bot.service"
        fi
    done
}
session_up() {   # session_up <root> <bot>
    local d="$1/local/testfleet/runtime/bots/$2" sock="bc-$$-$2"
    tmux -L "$sock" new-session -d -s "$2" 'sleep 300' 2>/dev/null
    SOCKETS="$SOCKETS $sock"
    touch "$d/data/.spawn"
}
tick() {         # tick <root> <boot_epoch> [bound_s]
    CLAUDLOBBY_ROOT="$1" CLAUDLOBBY_BOOT_EPOCH="$2" \
        BOOT_CAPTURE_BOUND_S="${3:-600}" PLANE_EMIT_DISABLED=1 \
        TMUX_TMPDIR="$TMUX_TMPDIR" \
        FLEET_NAME=ambient-should-not-leak bash "$BC" 2>&1
}
statedir() { printf '%s/runtime/_host/boot-capture/%s\n' "$1" "$2"; }

echo "== 1. first observation, and the overwrite it survives =="
R1="$TMP/r1"; mkroot "$R1" alpha:6 beta:21
B1=$(( $(date +%s) - 100 ))
session_up "$R1" alpha
tick "$R1" "$B1" > /dev/null
D1="$(statedir "$R1" "$B1")"
assert_eq "a bot with a session is captured" "alpha" "$(cut -f1 "$D1/observed.tsv" | tr '\n' ' ' | sed 's/ *$//')"
assert_eq "a bot with no session is NOT captured yet" "" "$(awk -F'\t' '$1=="beta"' "$D1/observed.tsv")"
assert_eq "the boot is not closed while a declared bot is unseen" "no" "$([ -f "$D1/closed" ] && echo yes || echo no)"
SPAWN_FIRST="$(awk -F'\t' '$1=="alpha"{print $4}' "$D1/observed.tsv")"
assert_eq "spawn_read_at is recorded beside the mtime" "1" \
    "$(awk -F'\t' '$1=="alpha" && $5 ~ /^[0-9]+$/ {print 1}' "$D1/observed.tsv")"

sleep 2
touch "$R1/local/testfleet/runtime/bots/alpha/data/.spawn"   # the keepalive restart
SPAWN_ONDISK="$(stat -c %Y "$R1/local/testfleet/runtime/bots/alpha/data/.spawn" 2>/dev/null \
                || stat -f %m "$R1/local/testfleet/runtime/bots/alpha/data/.spawn")"
session_up "$R1" beta
tick "$R1" "$B1" > /dev/null
SPAWN_AFTER="$(awk -F'\t' '$1=="alpha"{print $4}' "$D1/observed.tsv")"
# THE PROPERTY. A single-pass recorder reading at close would report
# SPAWN_ONDISK here, which describes the restart and not the boot.
assert_eq "the first observation survives a .spawn rewrite" "$SPAWN_FIRST" "$SPAWN_AFTER"
assert_absent "and it is not the post-restart value" "$SPAWN_ONDISK" "$SPAWN_AFTER"
assert_eq "the late bot is captured on the tick its session appears" "1" \
    "$(awk -F'\t' '$1=="beta"{print 1}' "$D1/observed.tsv")"
assert_contains "the boot closes complete once every declared bot is seen" "reason=complete" "$(cat "$D1/closed")"
assert_eq "no bot is recorded twice" "2" "$(wc -l < "$D1/observed.tsv" | tr -d ' ')"

echo "== 1b. a real injection stamp lands in the row =="
# Without this the stamp parser is only ever exercised on a MISSING file, and a
# reader that returns "-" for everything passes that test perfectly.
R1B="$TMP/r1b"; mkroot "$R1B" stamped:9
B1B=$(( $(date +%s) - 100 ))
printf 'state=done kind=startup at=2026-09-07T19:00:00-04:00 epoch=1788820000 boot=%s rc=0 dur=7\n' \
    "$B1B" > "$R1B/local/testfleet/runtime/bots/stamped/data/.inject"
session_up "$R1B" stamped
tick "$R1B" "$B1B" > /dev/null
D1B="$(statedir "$R1B" "$B1B")"
assert_eq "the send state is recorded"      "done"       "$(awk -F'\t' '$1=="stamped"{print $7}' "$D1B/observed.tsv")"
assert_eq "and which payload it was"        "startup"    "$(awk -F'\t' '$1=="stamped"{print $8}' "$D1B/observed.tsv")"
assert_eq "and the send instant"            "1788820000" "$(awk -F'\t' '$1=="stamped"{print $9}' "$D1B/observed.tsv")"
assert_eq "and the boot it belongs to"      "$B1B"       "$(awk -F'\t' '$1=="stamped"{print $10}' "$D1B/observed.tsv")"
assert_eq "and how long the send took"      "7"          "$(awk -F'\t' '$1=="stamped"{print $11}' "$D1B/observed.tsv")"

echo "== 2. the ladder end is DERIVED from the composed rungs =="
assert_contains "ladder end is the max composed rung, not a constant" "ladder_end=21" "$(cat "$D1/closed")"
assert_contains "and it says so" "ladder_derived=true" "$(cat "$D1/closed")"
R2="$TMP/r2"; mkroot "$R2" solo:45
B2=$(( $(date +%s) - 100 )); session_up "$R2" solo
tick "$R2" "$B2" > /dev/null
assert_contains "a different rung moves the ladder end" "ladder_end=45" "$(cat "$(statedir "$R2" "$B2")/closed")"

echo "== 3. an unreadable rung is not a zero-length ladder =="
R3="$TMP/r3"; mkroot "$R3" nounit:none
B3=$(( $(date +%s) - 100 )); session_up "$R3" nounit
tick "$R3" "$B3" > /dev/null
D3="$(statedir "$R3" "$B3")"
assert_contains "no readable rung is DISCLOSED, never asserted as 0" "ladder_derived=false" "$(cat "$D3/closed")"
assert_eq "and the per-bot rung reads -1, not 0" "-1" "$(awk -F'\t' '$1=="nounit"{print $6}' "$D3/observed.tsv")"

echo "== 4. the bound closes the boot and NAMES the shortfall =="
R4="$TMP/r4"; mkroot "$R4" up:3 down:6
B4=$(( $(date +%s) - 500 )); session_up "$R4" up
tick "$R4" "$B4" 1 > /dev/null
D4="$(statedir "$R4" "$B4")"
assert_contains "closed at the bound" "reason=bound" "$(cat "$D4/closed")"
assert_contains "the unaccounted bot is named, not merely counted" "unaccounted=down" "$(cat "$D4/closed")"
assert_contains "the denominator is stated" "declared=2 observed=1" "$(cat "$D4/closed")"

echo "== 5. an empty roster loses a boot, so it refuses rather than closing =="
R5="$TMP/r5"; mkdir -p "$R5/local"
B5=$(( $(date +%s) - 100 ))
OUT5="$(tick "$R5" "$B5")"; RC5=$?
assert_eq "exit 2 on an untrustworthy denominator" "2" "$RC5"
assert_contains "and it says why" "no bots declared" "$OUT5"
assert_eq "the boot is NOT closed, so a later tick can still capture it" "no" \
    "$([ -f "$(statedir "$R5" "$B5")/closed" ] && echo yes || echo no)"

echo "== 6. a closed boot is emitted once, and a later tick is a no-op =="
assert_eq "the closed boot was emitted" "yes" "$([ -f "$D4/emitted" ] && echo yes || echo no)"
EM_BEFORE="$(cat "$D4/emitted")"; sleep 1
tick "$R4" "$B4" 1 > /dev/null
assert_eq "a tick after close does not re-emit" "$EM_BEFORE" "$(cat "$D4/emitted")"
assert_eq "and does not duplicate rows" "1" "$(wc -l < "$D4/observed.tsv" | tr -d ' ')"

echo "== 7. a boot that closed but never emitted is flushed later =="
rm -f "$D4/emitted"
R4B=$(( B4 + 1 )); session_up "$R4" down
tick "$R4" "$R4B" 1 > /dev/null
assert_eq "the stranded prior boot is picked up on a later tick" "yes" \
    "$([ -f "$D4/emitted" ] && echo yes || echo no)"

echo "== 8. the injection stamp =="
IB="$R1/local/testfleet/runtime/bots/alpha"
rm -f "$IB/data/.inject"
bash -c ". '$LIBC'; t0=\$(inject_stamp '$IB' startup sending); inject_stamp '$IB' startup done 0 \"\$t0\" >/dev/null" 2>/dev/null
assert_eq "unarmed, the stamp writes nothing" "no" "$([ -f "$IB/data/.inject" ] && echo yes || echo no)"
bash -c "export BOOT_CAPTURE_ENABLED=1; . '$LIBC'; inject_stamp '$IB' startup sending >/dev/null" 2>/dev/null
assert_contains "armed, a send in flight is on disk as sending" "state=sending" "$(cat "$IB/data/.inject" 2>/dev/null)"
assert_contains "carrying the boot it belongs to" "boot=" "$(cat "$IB/data/.inject" 2>/dev/null)"
bash -c "export BOOT_CAPTURE_ENABLED=1; . '$LIBC'; t0=\$(inject_stamp '$IB' startup sending); sleep 1; inject_stamp '$IB' startup done 0 \"\$t0\" >/dev/null" 2>/dev/null
assert_contains "a completed send is stamped done" "state=done" "$(cat "$IB/data/.inject" 2>/dev/null)"
assert_contains "with its duration" "dur=1" "$(cat "$IB/data/.inject" 2>/dev/null)"
assert_eq "the epoch is returned even when unarmed, so callers do not branch" "1" \
    "$(bash -c ". '$LIBC'; v=\$(inject_stamp '$IB' startup sending); case \"\$v\" in ''|*[!0-9]*) echo 0;; *) echo 1;; esac" 2>/dev/null)"

echo "== 8b. a failing stamp must never cost a boot =="
# start-bot.sh reaches inject_stamp through `_inject_t0="$(inject_stamp ...)"`
# under `set -euo pipefail`, with no guard at the call site, so any nonzero
# return from this function aborts the boot (#1496 review).
#
# What is asserted is the RETURN, not a downstream symptom. The review predicted
# an abort from the epoch printf; measured, that does not reproduce — bash does
# not exit on a failing command inside a function when further statements follow
# it, so the printf was never the reachable hazard. The reachable one is the
# function returning nonzero at all, which is what this pins.
#
# Positive control included: the write must genuinely fail, or a green result
# here means only that nothing was tested.
RO="$TMP/rodir"; mkdir -p "$RO/data"; chmod a-w "$RO/data"
_stamp_rc() {
    bash -c "
        . '$LIBC'
        export BOOT_CAPTURE_ENABLED=1
        inject_stamp '$RO' startup $1 >/dev/null 2>&1
        echo \$?
    " 2>/dev/null
}
assert_eq "control: the stamp really could not be written" "no" \
    "$([ -f "$RO/data/.inject" ] && echo yes || echo no)"
assert_eq "an unwritable stamp still returns 0"      "0" "$(_stamp_rc sending)"
assert_eq "and so does the second stamp of the pair" "0" "$(_stamp_rc done)"
chmod u+w "$RO/data"

echo "== 9. a host-scoped sweep carries no fleet identity of its own =="
# tick() exports a misleading FLEET_NAME on purpose: run by hand from inside a
# bot session, an ambient fleet would stamp every row on every fleet with it.
assert_eq "the row keeps the bot own fleet" "testfleet" "$(awk -F'\t' '$1=="alpha"{print $2}' "$D1/observed.tsv")"

echo
echo "---- $PASS/$TOTAL passed, $FAIL failed ----"
[ "$FAIL" -eq 0 ]
