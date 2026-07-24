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

# run_check THROTTLED JOURNAL BOOT_ID → one check in an isolated env; the
# ALERT/REPEAT/OK verdict lands in $LOG. The alert-delivery leg is neutered:
# scratch CLAUDLOBBY_ROOT has no lib/tg-post.sh, env -i drops any real token, and
# tmux is a no-op — so nothing escapes to the real fleet.
run_check() {
    env -i PATH="$T/bin:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$T" \
        CLAUDLOBBY_ROOT="$ROOT" \
        THROTTLED="$1" JOURNAL="$2" HOST_HEALTH_BOOT_ID="$3" \
        bash "$LIB_DIR/host-health-check.sh" >/dev/null 2>&1 || true
}
last_verdict() { tail -1 "$LOG" | grep -oE 'ALERT|REPEAT|OK' | head -1; }
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

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
