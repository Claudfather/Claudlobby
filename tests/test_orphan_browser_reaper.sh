#!/usr/bin/env bash
# tests/test_orphan_browser_reaper.sh — orphan-browser-reaper selection contract.
# Real awk/grep + a stubbed `ps` (canned process tables): asserts WHICH processes
# the reaper selects, which is the whole safety surface — a false positive kills
# someone's working browser, a false negative is the #807 leak coming back.
#
# Every case runs --dry-run, so nothing is killed and no FLEET NOTICE is emitted.
# Belt and braces on top of that: hermetic under env -i with a scratch
# CLAUDLOBBY_ROOT and stubbed tmux/tg-post, so even a regression that reached the
# alert path could not touch a real manager session or leak a bot token. A test
# must never push fleet-wide notices.
#
# Standalone bash (not pytest-collected); runs under macOS /bin/bash (3.2).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
REAPER="$LIB_DIR/orphan-browser-reaper.sh"
PASS=0; FAIL=0; TOTAL=0

assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then
        echo "  PASS: $d"; PASS=$((PASS + 1))
    else
        echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1))
    fi
}

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/root/lib"
ROOT="$T/root"
LOG="$ROOT/lib/orphan-browser-reaper.log"

# Stub ps. Two call shapes must be told apart:
#   ps -o ppid= -p <pid>                       (ancestor walk) -> empty, ends the walk
#   ps -u <user> -o pid=,ppid=,etime=,rss=,comm=  (the snapshot) -> $PSTABLE
cat > "$T/bin/ps" <<'STUB'
#!/bin/bash
for a in "$@"; do
    if [ "$a" = "-p" ]; then exit 0; fi
done
printf '%s\n' "${PSTABLE:-}"
STUB
# Stub the alert path so a regression can never reach a real session/token.
printf '#!/bin/bash\nexit 0\n' > "$T/bin/tmux"
printf '#!/bin/bash\nexit 0\n' > "$T/bin/tg-post.sh"
chmod +x "$T/bin/ps" "$T/bin/tmux" "$T/bin/tg-post.sh"

# run_case <ps-table> [extra args...] -> log text on stdout
run_case() {
    local table="$1"; shift
    : > "$LOG"
    env -i HOME="$T" PATH="$T/bin:/usr/bin:/bin" CLAUDLOBBY_ROOT="$ROOT" \
        PSTABLE="$table" \
        bash "$REAPER" --dry-run "$@" >/dev/null 2>&1 || true
    cat "$LOG"
}

# selected <log> <pid> -> "yes"/"no"
selected() {
    case "$1" in
        *"pid=$2 "*) printf 'yes' ;;
        *)           printf 'no' ;;
    esac
}

echo "orphan-browser-reaper: selection contract"

# --- 1. the #807 shapes: orphaned browser, both parent flavors ---------------
# 4001 is parented to init (pid 1). 4002 is parented to a subreaper: systemd
# --user adopts what a bot session orphans, so its ppid is NOT 1 -- the shape a
# naive `ppid == 1` test misses, which on a systemd host is the common one.
TABLE_ORPHANS=' 4001     1 10:00:00 512000 chromium
 4002   960 10:00:00 512000 chromium
  960     1 6-09:00:00   7952 systemd'
out=$(run_case "$TABLE_ORPHANS")
assert_eq "orphan parented to init is selected"      yes "$(selected "$out" 4001)"
assert_eq "orphan adopted by subreaper is selected"  yes "$(selected "$out" 4002)"

# --- 2. a browser with a live parent is someone's session --------------------
TABLE_LIVE=' 4010  4009 10:00:00 512000 chromium
 4009     1 10:00:00  20000 node'
out=$(run_case "$TABLE_LIVE")
assert_eq "browser with a live parent is NOT selected" no "$(selected "$out" 4010)"

# --- 3. age gate -------------------------------------------------------------
TABLE_YOUNG=' 4020     1 00:30 512000 chromium'
out=$(run_case "$TABLE_YOUNG")
assert_eq "orphan younger than the threshold is NOT selected" no "$(selected "$out" 4020)"
out=$(run_case "$TABLE_YOUNG" --max-age-hours 0)
assert_eq "same orphan IS selected at threshold 0"            yes "$(selected "$out" 4020)"

# --- 4. the pattern is anchored over the basename ----------------------------
# A substring match would reap all three of these; they are ordinary processes.
TABLE_NEARMISS=' 4030     1 10:00:00 1000 chrome-notes.sh
 4031     1 10:00:00 1000 playwright-tests
 4032     1 10:00:00 1000 not-chromium'
out=$(run_case "$TABLE_NEARMISS")
assert_eq "chrome-notes.sh is NOT selected"  no "$(selected "$out" 4030)"
assert_eq "playwright-tests is NOT selected" no "$(selected "$out" 4031)"
assert_eq "not-chromium is NOT selected"     no "$(selected "$out" 4032)"

# --- 5. macOS reports comm as a full path, sometimes with spaces -------------
TABLE_MACOS=' 4040     1 10:00:00 512000 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
out=$(run_case "$TABLE_MACOS")
assert_eq "macOS full-path comm matches on its basename" yes "$(selected "$out" 4040)"

# --- 6. descendants go with the root ----------------------------------------
# The #807 leak was 18 processes in one tree. Renderers/crashpad helpers point at
# the browser, not at init, so root-only reaping frees a fraction and leaves the
# rest to be adopted and reaped a run later. 4052 is a grandchild: the closure
# must be transitive, not one level.
TABLE_TREE=' 4050     1 10:00:00 100000 chromium
 4051  4050 10:00:00 200000 sleep
 4052  4051 10:00:00 200000 cat
 4060     1 10:00:00  10000 sleep'
out=$(run_case "$TABLE_TREE")
assert_eq "doomed root is selected"                    yes "$(selected "$out" 4050)"
assert_eq "child of a doomed root goes with it"        yes "$(selected "$out" 4051)"
assert_eq "grandchild goes too (transitive closure)"   yes "$(selected "$out" 4052)"
assert_eq "unrelated orphan sleep is NOT selected"     no  "$(selected "$out" 4060)"

# --- 7. etime parsing --------------------------------------------------------
# [[DD-]HH:]MM:SS. etimes(1) would be simpler but is procps-only (absent on macOS).
TABLE_ETIME=' 4070     1 2-03:04:05 1000 chromium
 4071     1     07:00 1000 chromium'
out=$(run_case "$TABLE_ETIME" --max-age-hours 24)
assert_eq "DD-HH:MM:SS parses as > 24h"        yes "$(selected "$out" 4070)"
assert_eq "MM:SS parses as minutes, not hours" no  "$(selected "$out" 4071)"

# --- 8. the reaper never selects itself -------------------------------------
# comm-matching (never a cmdline pattern) is what makes this structural, but the
# protected-pid guard is asserted too: a reaper that can kill its own process
# group is worse than the leak it cleans up.
TABLE_SELF=' 1     1 10:00:00 1000 chromium'
out=$(run_case "$TABLE_SELF" --max-age-hours 0)
assert_eq "pid 1 is never selected" no "$(selected "$out" 1)"

# --- 9. dry-run emits nothing beyond the log --------------------------------
out=$(run_case "$TABLE_ORPHANS")
case "$out" in
    *"DRY-RUN"*) assert_eq "dry-run says so and kills nothing" yes yes ;;
    *)           assert_eq "dry-run says so and kills nothing" yes no ;;
esac
case "$out" in
    *"REAPED"*) assert_eq "dry-run never logs a reap" yes no ;;
    *)          assert_eq "dry-run never logs a reap" yes yes ;;
esac

echo
echo "  $PASS/$TOTAL passed"
[ "$FAIL" -eq 0 ] || exit 1
