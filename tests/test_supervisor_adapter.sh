#!/usr/bin/env bash
# tests/test_supervisor_adapter.sh — contract tests for lib/supervisor.sh, the
# five-verb systemctl/launchctl adapter (#1573 boot admission, task 6).
#
# Standalone bash (not pytest-collected on its own); discovered by
# tests/test_sh_suites.py, which runs it hermetically (constructed env: PATH +
# LANG only, plus a throwaway HOME/TMPDIR -- see tests/conftest.py
# constructed_env). Runs under macOS /bin/bash (3.2) too.
#
# Hermetic: fake `systemctl`, `launchctl` and `uname` sit on a prepended PATH
# and log their argv to a shared file; `uname` is faked (not just $_OS
# reassigned) because svc_enroll forks real child scripts
# (install-bot-systemd.sh / install-bot.sh) that call detect_os fresh in
# their own process -- an in-process $_OS override would not reach them, but
# a faked `uname` on the inherited PATH does, so re-running detect_os after
# flipping FAKE_UNAME keeps parent and child agreeing about the platform.
# HOME is redirected so systemd-user-unit / LaunchAgent paths never touch the
# real host. svc_enroll's own contract test additionally points
# $_SUPERVISOR_LIB_DIR (supervisor.sh's own sibling-script lookup, deliberately
# NOT $CLAUDLOBBY_ROOT -- see the comment beside it in lib/supervisor.sh) at a
# scratch tree holding FAKE install-bot-systemd.sh / install-bot.sh
# stand-ins, never the real ones: the real install-bot.sh shells out to the
# absolute, un-fakeable /bin/launchctl bootstrap, and actually bootstrapping
# a LaunchAgent on the machine running this suite is exactly the kind of
# real side effect a hermetic test must never risk.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
PASS=0; FAIL=0; TOTAL=0
assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS + 1)); else echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1)); fi
}
assert_contains() {
    TOTAL=$((TOTAL + 1)); local d="$1" needle="$2" haystack="$3"
    case "$haystack" in
        *"$needle"*) echo "  PASS: $d"; PASS=$((PASS + 1)) ;;
        *) echo "  FAIL: $d (expected to find '$needle' in: $haystack)"; FAIL=$((FAIL + 1)) ;;
    esac
}

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export HOME="$T/home"
mkdir -p "$HOME/.config/systemd/user" "$HOME/Library/LaunchAgents" "$T/bin"

FAKE_LOG="$T/fake-argv.log"
export FAKE_LOG

cat > "$T/bin/uname" <<'EOF'
#!/bin/bash
printf '%s\n' "${FAKE_UNAME:-Darwin}"
EOF

cat > "$T/bin/systemctl" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_LOG"
if [ "$1" = "--user" ] && [ "$2" = "show" ]; then
    case "${FAKE_STATE:-active}" in
        active)   printf 'ActiveState=active\nLoadState=loaded\n' ;;
        inactive) printf 'ActiveState=inactive\nLoadState=loaded\n' ;;
        *)        printf 'ActiveState=inactive\nLoadState=not-found\n' ;;
    esac
fi
exit "${FAKE_EXIT:-0}"
EOF

cat > "$T/bin/launchctl" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_LOG"
if [ "$1" = "print" ]; then
    case "${FAKE_STATE:-active}" in
        active)   printf 'state = running\n'; exit 0 ;;
        inactive) printf 'state = not running\n'; exit 0 ;;
        *)        exit 1 ;;
    esac
fi
exit "${FAKE_EXIT:-0}"
EOF
chmod +x "$T/bin/uname" "$T/bin/systemctl" "$T/bin/launchctl"
export PATH="$T/bin:$PATH"

# shellcheck source=../lib/lib-common.sh
. "$LIB_DIR/lib-common.sh"

as_os() {  # as_os <Linux|Darwin|SunOS> -- flips the fake uname AND re-derives
           # $_OS via the real detect_os, so parent process and any forked
           # child (install-bot*.sh) agree on the platform.
    export FAKE_UNAME="$1"
    detect_os
}

write_bot_conf() {  # write_bot_conf <bot_dir> <bot_service> <bot_name>
    mkdir -p "$1"
    printf 'BOT_SERVICE=%s\nBOT_NAME=%s\n' "$2" "$3" > "$1/bot.conf"
}

reset_fakes() {
    : > "$FAKE_LOG"
    unset FAKE_STATE FAKE_EXIT || true
    rm -f "$HOME/.config/systemd/user"/*.service "$HOME/Library/LaunchAgents"/*.plist 2>/dev/null || true
}

echo "=== supervisor.sh contract -- svc_unit_name ==="

BOT="$T/bots/alpha"
write_bot_conf "$BOT" "svc-alpha" "alpha"
reset_fakes
assert_eq "BOT_SERVICE set: printed verbatim" "svc-alpha" "$(svc_unit_name "$BOT")"

# Pre-rename fallback: BOT_SERVICE empty, BOT_NAME set, and a unit/plist for
# BOT_NAME actually exists -- svc_unit_name must fall back to it.
BOT2="$T/bots/bravo"
write_bot_conf "$BOT2" "" "bravo"
reset_fakes
as_os Linux
assert_eq "no fallback file yet: prints empty" "" "$(svc_unit_name "$BOT2")"
: > "$HOME/.config/systemd/user/bravo.service"
assert_eq "Linux fallback: BOT_NAME used once its unit file exists" "bravo" "$(svc_unit_name "$BOT2")"
rm -f "$HOME/.config/systemd/user/bravo.service"

as_os Darwin
reset_fakes
assert_eq "no fallback plist yet: prints empty" "" "$(svc_unit_name "$BOT2")"
: > "$HOME/Library/LaunchAgents/bravo.plist"
assert_eq "Darwin fallback: BOT_NAME used once its plist exists" "bravo" "$(svc_unit_name "$BOT2")"
rm -f "$HOME/Library/LaunchAgents/bravo.plist"

echo "=== supervisor.sh contract -- svc_is_registered ==="

reset_fakes
as_os Linux
if svc_is_registered "$BOT"; then echo "  FAIL: not yet registered read as registered"; FAIL=$((FAIL+1)); else echo "  PASS: unregistered reads false"; PASS=$((PASS+1)); fi
TOTAL=$((TOTAL+1))
: > "$HOME/.config/systemd/user/svc-alpha.service"
if svc_is_registered "$BOT"; then echo "  PASS: registered unit reads true"; PASS=$((PASS+1)); else echo "  FAIL: registered unit read as false"; FAIL=$((FAIL+1)); fi
TOTAL=$((TOTAL+1))

as_os Darwin
if svc_is_registered "$BOT"; then echo "  FAIL: no plist yet read as registered"; FAIL=$((FAIL+1)); else echo "  PASS: no plist reads false"; PASS=$((PASS+1)); fi
TOTAL=$((TOTAL+1))
: > "$HOME/Library/LaunchAgents/svc-alpha.plist"
if svc_is_registered "$BOT"; then echo "  PASS: registered plist reads true"; PASS=$((PASS+1)); else echo "  FAIL: registered plist read as false"; FAIL=$((FAIL+1)); fi
TOTAL=$((TOTAL+1))

echo "=== supervisor.sh contract -- svc_state ==="

for os in Linux Darwin; do
    as_os "$os"
    for st in active inactive absent; do
        reset_fakes
        FAKE_STATE="$st" out="$(FAKE_STATE="$st" svc_state "$BOT")"
        case "$st" in
            active)   want=loaded-active ;;
            inactive) want=loaded-inactive ;;
            absent)   want=not-loaded ;;
        esac
        assert_eq "$os/$st: svc_state prints $want" "$want" "$out"
    done
done

reset_fakes
as_os Linux
FAKE_STATE=active svc_state "$BOT" >/dev/null
assert_contains "Linux svc_state calls systemctl --user show with the label" \
    "--user show" "$(cat "$FAKE_LOG")"
assert_contains "Linux svc_state names the unit" "svc-alpha.service" "$(cat "$FAKE_LOG")"

reset_fakes
as_os Darwin
FAKE_STATE=active svc_state "$BOT" >/dev/null
assert_contains "Darwin svc_state calls launchctl print" "print" "$(cat "$FAKE_LOG")"
assert_contains "Darwin svc_state names the label" "svc-alpha" "$(cat "$FAKE_LOG")"

as_os SunOS
assert_eq "unrecognized OS: svc_state prints unknown" "unknown" "$(svc_state "$BOT")"

echo "=== supervisor.sh contract -- svc_kick ==="

# Primary branch: BOT_SERVICE-named unit/plist exists.
BOT3="$T/bots/charlie"
write_bot_conf "$BOT3" "svc-charlie" "charlie"
reset_fakes
as_os Linux
: > "$HOME/.config/systemd/user/svc-charlie.service"
out="$(svc_kick "$BOT3")"; rc=$?
assert_eq "Linux kick (primary): rc 0" "0" "$rc"
assert_contains "Linux kick (primary): restarts the BOT_SERVICE unit" "restart svc-charlie.service" "$(cat "$FAKE_LOG")"
assert_contains "Linux kick (primary): description printed for the caller's log" "svc-charlie" "$out"
rm -f "$HOME/.config/systemd/user/svc-charlie.service"

# Pre-rename branch: BOT_SERVICE unit absent, but a BOT_NAME unit exists.
reset_fakes
: > "$HOME/.config/systemd/user/charlie.service"
out="$(svc_kick "$BOT3")"; rc=$?
assert_eq "Linux kick (pre-rename): rc 0" "0" "$rc"
assert_contains "Linux kick (pre-rename): restarts the BOT_NAME unit" "restart charlie.service" "$(cat "$FAKE_LOG")"
assert_contains "Linux kick (pre-rename): description says so" "pre-rename" "$out"
rm -f "$HOME/.config/systemd/user/charlie.service"

# Neither unit installed: no branch applies -- rc 2, nothing invoked.
reset_fakes
set +e
svc_kick "$BOT3" >/dev/null 2>/dev/null; rc=$?
set -e
assert_eq "Linux kick (no unit installed): rc 2" "2" "$rc"
assert_eq "Linux kick (no unit installed): fake untouched" "" "$(cat "$FAKE_LOG")"

# Darwin branch.
as_os Darwin
reset_fakes
: > "$HOME/Library/LaunchAgents/svc-charlie.plist"
out="$(svc_kick "$BOT3")"; rc=$?
assert_eq "Darwin kick: rc 0" "0" "$rc"
assert_contains "Darwin kick: kickstarts the label" "kickstart" "$(cat "$FAKE_LOG")"
assert_contains "Darwin kick: names svc-charlie" "svc-charlie" "$(cat "$FAKE_LOG")"
rm -f "$HOME/Library/LaunchAgents/svc-charlie.plist"

# _OS=Other: rc 2, empty fake log, no branch invoked at all (brief's named case).
as_os SunOS
reset_fakes
set +e
svc_kick "$BOT3" >/dev/null 2>/dev/null; rc=$?
set -e
assert_eq "Other OS: svc_kick rc 2" "2" "$rc"
assert_eq "Other OS: fake untouched" "" "$(cat "$FAKE_LOG")"

echo "=== supervisor.sh contract -- svc_enroll (dispatch only, stubbed installers) ==="

# The REAL install-bot-systemd.sh / install-bot.sh are deliberately never
# exercised here (see file header) -- only that svc_enroll dispatches to the
# right one, by name, with the bot_dir argument.
SCRATCH_LIB="$T/scratch-lib"
mkdir -p "$SCRATCH_LIB"
cat > "$SCRATCH_LIB/install-bot-systemd.sh" <<EOF
#!/bin/bash
printf 'install-bot-systemd.sh %s\n' "\$*" >> "$FAKE_LOG"
exit 0
EOF
cat > "$SCRATCH_LIB/install-bot.sh" <<EOF
#!/bin/bash
printf 'install-bot.sh %s\n' "\$*" >> "$FAKE_LOG"
exit 0
EOF
chmod +x "$SCRATCH_LIB/install-bot-systemd.sh" "$SCRATCH_LIB/install-bot.sh"

BOT4="$T/bots/delta"
write_bot_conf "$BOT4" "svc-delta" "delta"

reset_fakes
as_os Linux
( _SUPERVISOR_LIB_DIR="$SCRATCH_LIB" svc_enroll "$BOT4" )
assert_contains "Linux enroll: calls install-bot-systemd.sh with the bot dir" \
    "install-bot-systemd.sh $BOT4" "$(cat "$FAKE_LOG")"

reset_fakes
as_os Darwin
( _SUPERVISOR_LIB_DIR="$SCRATCH_LIB" svc_enroll "$BOT4" )
assert_contains "Darwin enroll: calls install-bot.sh with the bot dir" \
    "install-bot.sh $BOT4" "$(cat "$FAKE_LOG")"

reset_fakes
as_os SunOS
set +e
( _SUPERVISOR_LIB_DIR="$SCRATCH_LIB" svc_enroll "$BOT4" ) 2>/dev/null; rc=$?
set -e
assert_eq "Other OS: svc_enroll rc 2" "2" "$rc"
assert_eq "Other OS: neither installer invoked" "" "$(cat "$FAKE_LOG")"

echo "=== supervisor.sh contract -- svc_disenroll ==="

BOT5="$T/bots/echo"
write_bot_conf "$BOT5" "svc-echo" "echo"

reset_fakes
as_os Linux
: > "$HOME/.config/systemd/user/svc-echo.service"
: > "$BOT5/.tmux-env"
svc_disenroll "$BOT5"
assert_eq "Linux disenroll: unit file removed" "false" "$([ -f "$HOME/.config/systemd/user/svc-echo.service" ] && echo true || echo false)"
assert_contains "Linux disenroll: disable --now called" "disable --now svc-echo.service" "$(cat "$FAKE_LOG")"
assert_contains "Linux disenroll: daemon-reload called" "daemon-reload" "$(cat "$FAKE_LOG")"
assert_contains "Linux disenroll: reset-failed called" "reset-failed svc-echo.service" "$(cat "$FAKE_LOG")"
assert_eq "Linux disenroll: .tmux-env removed (OS-independent teardown leg)" "false" "$([ -f "$BOT5/.tmux-env" ] && echo true || echo false)"

reset_fakes
as_os Darwin
: > "$HOME/Library/LaunchAgents/svc-echo.plist"
: > "$BOT5/.tmux-env"
svc_disenroll "$BOT5"
assert_eq "Darwin disenroll: plist removed" "false" "$([ -f "$HOME/Library/LaunchAgents/svc-echo.plist" ] && echo true || echo false)"
assert_contains "Darwin disenroll: bootout called" "bootout" "$(cat "$FAKE_LOG")"
assert_contains "Darwin disenroll: names svc-echo" "svc-echo" "$(cat "$FAKE_LOG")"
assert_eq "Darwin disenroll: .tmux-env removed (OS-independent teardown leg)" "false" "$([ -f "$BOT5/.tmux-env" ] && echo true || echo false)"

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
