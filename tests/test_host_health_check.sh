#!/usr/bin/env bash
# tests/test_host_health_check.sh — host-health-check detection + de-dup contract.
# Real cksum/grep/sed + stub vcgencmd/journalctl (canned probe output): asserts the
# alert de-dup fingerprints on a STABLE signature — it survives the volatile
# kworker/PID churn of a single ongoing incident, yet resets across a reboot so a
# post-reboot recurrence is never swallowed — and that the storage grep + throttle
# decode cover the real SD/MMC failure signatures without firing empty-label alerts.
# Runs hermetically under env -i so the real alert path (manager tmux + Telegram)
# cannot be reached and no real bot token leaks into the subprocess. Standalone
# bash (not pytest-collected); runs under macOS /bin/bash (3.2).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
PASS=0; FAIL=0; TOTAL=0
assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS + 1)); else echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1)); fi
}

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/root/lib"
# Stub vcgencmd: emit throttled=$THROTTLED (default 0x0 = clean, models a healthy Pi).
printf '#!/bin/bash\nprintf "throttled=%%s\\n" "${THROTTLED:-0x0}"\n' > "$T/bin/vcgencmd"
# Stub journalctl: emit $JOURNAL verbatim (default empty = clean storage); ignores args.
printf '#!/bin/bash\nprintf "%%s" "${JOURNAL:-}"\n' > "$T/bin/journalctl"
# Stub tmux: no-op — belt-and-suspenders so the alert path can never reach a real session.
printf '#!/bin/bash\nexit 0\n' > "$T/bin/tmux"
chmod +x "$T/bin/vcgencmd" "$T/bin/journalctl" "$T/bin/tmux"

ROOT="$T/root"; LOG="$ROOT/lib/host-health-check.log"

# Stub tg-post at the path lib-common actually invokes ($CLAUDLOBBY_ROOT/lib/tg-post.sh),
# exiting $TGPOST_RC. Before #977 this file was simply ABSENT, so every delivery
# failed by accident and nothing depended on it. It does now: the de-dup
# fingerprint is written only on a DELIVERED alert, so "does the alert go out"
# is a variable these tests must control rather than inherit. Default 0 keeps
# the de-dup contract below testing what it always tested.
printf '#!/bin/bash\nexit "${TGPOST_RC:-0}"\n' > "$ROOT/lib/tg-post.sh"
chmod +x "$ROOT/lib/tg-post.sh"

# run_check THROTTLED JOURNAL BOOT_ID → one check in an isolated env; the
# ALERT/REPEAT/OK verdict lands in $LOG. The alert-delivery leg is neutered:
# scratch CLAUDLOBBY_ROOT has no lib/tg-post.sh, env -i drops any real token, and
# tmux is a no-op — so nothing escapes to the real fleet.
run_check() {
    env -i PATH="$T/bin:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$T" \
        CLAUDLOBBY_ROOT="$ROOT" TGPOST_RC="${TGPOST_RC:-0}" \
        TELEGRAM_GROUP_CHAT_ID="-1001234567890" \
        THROTTLED="$1" JOURNAL="$2" HOST_HEALTH_BOOT_ID="$3" \
        bash "$LIB_DIR/host-health-check.sh" >/dev/null 2>&1 || true
}
# Read the verdict by ANCHOR, never by line position. Every log line is
# `<ts> <TOKEN> -- <msg>`, so the verdict is field 2 followed by " -- "; this
# takes the LAST line matching that grammar and ignores any other line type.
#
# It used to be `tail -1`, which broke the moment #977 appended a DELIVERY-FAILED
# line after the verdict. `tail -2` would have been the same defect with a new
# constant -- it re-breaks on the next line anyone adds. Anchoring on the line's
# own grammar is what makes it stable, and DELIVERY-FAILED is correctly excluded
# because it is not a verdict.
last_verdict() {
    grep -oE '^[^ ]+ (OK|ALERT|REPEAT) --' "$LOG" 2>/dev/null | tail -1 | awk '{print $2}'
}
log_has() { grep -qE "$1" "$LOG" 2>/dev/null; }
state_file() { printf '%s' "$ROOT/lib/host-health-check.state"; }
reset() { rm -f "$ROOT/lib/host-health-check.state" "$LOG"; }

echo "=== host-health-check detection + de-dup contract ==="

# --- baseline: clean host, and a real condition still alerts (no over-suppression) ---
reset
run_check "0x0" "" "bootA"
assert_eq "clean host -> OK, no alert" "OK" "$(last_verdict)"

reset
run_check "0x50005" "" "bootA"
assert_eq "under-voltage-NOW condition -> ALERT (real labels)" "ALERT" "$(last_verdict)"
run_check "0x50005" "" "bootA"
assert_eq "same condition, same boot -> REPEAT (base de-dup holds)" "REPEAT" "$(last_verdict)"

reset
run_check "0x50000" "" "bootA"
assert_eq "latched-only power condition -> ALERT (has labels)" "ALERT" "$(last_verdict)"

# --- de-dup fix (a): a recurrence AFTER a reboot must NOT be swallowed ---
reset
run_check "0x50005" "" "bootA"
assert_eq "boot A: first sighting -> ALERT" "ALERT" "$(last_verdict)"
run_check "0x50005" "" "bootB"
assert_eq "identical finding after reboot -> ALERT, not swallowed" "ALERT" "$(last_verdict)"

# --- de-dup fix (b): one ongoing stall with a churning kworker id -> ONE alert ---
reset
J1="Jul 24 10:00:00 host kernel: INFO: task kworker/u8:0:61 blocked for more than 122 seconds."
J2="Jul 24 10:05:00 host kernel: INFO: task kworker/u9:2:118 blocked for more than 245 seconds."
run_check "0x0" "$J1" "bootA"
assert_eq "storage stall -> ALERT" "ALERT" "$(last_verdict)"
run_check "0x0" "$J2" "bootA"
assert_eq "same stall, churned kworker/PID -> REPEAT (stable signature)" "REPEAT" "$(last_verdict)"

# --- grep fix: block-device-level MMC failures must be detected ---
reset
run_check "0x0" "Jul 24 10:00:00 host kernel: mmcblk0: error -110 sending status command, response 0x0" "bootA"
assert_eq "mmcblk0 block-device error -> detected (ALERT)" "ALERT" "$(last_verdict)"

reset
run_check "0x0" "Jul 24 10:00:00 host kernel: mmcblk0: Card stuck in programming state!" "bootA"
assert_eq "SD card 'stuck in programming state' -> detected (ALERT)" "ALERT" "$(last_verdict)"

# --- throttle-decode fix: values that decode to no known bit must NOT alert ---
reset
run_check "0x100000" "" "bootA"
assert_eq "undefined throttle bit (0x100000) -> no empty-label alert (OK)" "OK" "$(last_verdict)"

reset
run_check "0x00" "" "bootA"
assert_eq "0x00 (numeric zero, non-canonical string) -> no alert (OK)" "OK" "$(last_verdict)"

# --- #720 fast-follow: malformed hex must not CRASH the monitor (anchored guard) ---
# A 0x-prefix-then-non-hex value matches the old unanchored glob, reaches $(( )),
# and its arithmetic error is FATAL under set -e (aborts past `|| return 0`) — no
# verdict logged. The anchored guard must reject it first so the monitor stays up.
reset
run_check "0x1zzz" "" "bootA"
assert_eq "malformed hex 0x1zzz -> clean skip, monitor does not crash (OK)" "OK" "$(last_verdict)"

reset
run_check "0xAg" "" "bootA"
assert_eq "malformed hex 0xAg -> clean skip (OK)" "OK" "$(last_verdict)"

# --- #720 fast-follow: distinct mmc devices must not collapse to one fingerprint ---
# Device identity (mmcblk0 vs mmcblk1) must survive the volatile digit-collapse so a
# genuinely different device failing re-alerts instead of being deduped as a repeat.
reset
run_check "0x0" "Jul 24 10:00:00 host kernel: mmcblk0: error -110 sending status command" "bootA"
assert_eq "mmcblk0 failure -> ALERT" "ALERT" "$(last_verdict)"
run_check "0x0" "Jul 24 10:05:00 host kernel: mmcblk1: error -84 sending status command" "bootA"
assert_eq "distinct device mmcblk1 failing -> ALERT (not deduped against mmcblk0)" "ALERT" "$(last_verdict)"

# control: SAME device with a churning error code stays ONE incident (guard vs over-fix).
reset
run_check "0x0" "Jul 24 10:00:00 host kernel: mmcblk0: error -110 sending status command" "bootA"
assert_eq "mmcblk0 first failure -> ALERT" "ALERT" "$(last_verdict)"
run_check "0x0" "Jul 24 10:05:00 host kernel: mmcblk0: error -84 sending status command" "bootA"
assert_eq "same device mmcblk0, volatile error code -> REPEAT (one ongoing incident)" "REPEAT" "$(last_verdict)"

echo ""

# --- #977: a FAILED delivery must be loud and must not buy silence -----------
# The bug: the fingerprint was written unconditionally, so an alert that reached
# nobody put the check into REPEAT forever. Measured on the live Pi: the hardware
# alarm fired 2026-08-17 and has been logging REPEAT hourly ever since, having
# never been delivered once.

reset
TGPOST_RC=3 run_check "0x50005" "" "bootA"      # 3 = API rejected the send
assert_eq "#977 failed delivery still records the ALERT verdict" "ALERT" "$(last_verdict)"
log_has 'DELIVERY-FAILED --' && r=yes || r=no
assert_eq "#977 failed delivery is LOUD in the log" "yes" "$r"
[ -s "$(state_file)" ] && r=yes || r=no
assert_eq "#977 fingerprint WITHHELD on a failed delivery" "no" "$r"

TGPOST_RC=3 run_check "0x50005" "" "bootA"      # same condition, second run
assert_eq "#977 it RETRIES instead of going REPEAT-forever" "ALERT" "$(last_verdict)"

# ...and the moment delivery works, de-dup resumes. Both directions, because a
# fix that only ever retries would alert on every single pass forever.
TGPOST_RC=0 run_check "0x50005" "" "bootA"
assert_eq "#977 delivery succeeds -> ALERT (the retry lands)" "ALERT" "$(last_verdict)"
[ -s "$(state_file)" ] && r=yes || r=no
assert_eq "#977 fingerprint written once delivered" "yes" "$r"
TGPOST_RC=0 run_check "0x50005" "" "bootA"
assert_eq "#977 de-dup resumes after a delivered alert" "REPEAT" "$(last_verdict)"

# The verdict reader must survive a new trailing line type. This is the exact
# fragility that broke this file: tail -1 read the DELIVERY-FAILED line instead
# of the verdict, and tail -2 would break on the next line anyone appends.
reset
TGPOST_RC=3 run_check "0x50005" "" "bootA"
printf '%s SOMETHING-NEW -- a line type that does not exist yet\n' "2026-01-01T00:00:00Z" >> "$LOG"
assert_eq "#977 verdict is read by anchor, not position (survives trailing lines)" "ALERT" "$(last_verdict)"

echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
