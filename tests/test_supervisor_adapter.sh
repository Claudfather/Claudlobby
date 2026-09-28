#!/usr/bin/env bash
# tests/test_supervisor_adapter.sh — contract tests for lib/supervisor.sh, the
# five-verb systemctl/launchctl adapter (#1573 boot admission, task 6).
#
# Standalone bash (not pytest-collected on its own); discovered by
# tests/test_sh_suites.py, which runs it hermetically (constructed env: PATH +
# LANG only, plus a throwaway HOME/TMPDIR -- see tests/conftest.py
# constructed_env). Runs under macOS /bin/bash (3.2) too.
#
# Hermetic: fake `systemctl`, `launchctl`, `uname` and `tmux` sit on a
# prepended PATH and log their argv to a shared file (tmux gets its own,
# TMUX_LOG, so a positive check there and a negative check on FAKE_LOG never
# have to untangle each other's lines); `uname` is faked (not just $_OS
# reassigned) because svc_enroll forks real child scripts
# (install-bot-systemd.sh / install-bot.sh) that call detect_os fresh in
# their own process -- an in-process $_OS override would not reach them, but
# a faked `uname` on the inherited PATH does, so re-running detect_os after
# flipping FAKE_UNAME keeps parent and child agreeing about the platform.
# `tmux` is faked via TMUX_BIN, exported before lib-common.sh is sourced,
# since lib-common.sh resolves $_TMUX_BIN once at source time and TMUX_BIN
# short-circuits that resolution directly rather than depending on PATH
# search order. HOME is redirected so systemd-user-unit / LaunchAgent paths
# never touch the real host. svc_enroll's own contract test additionally
# points $_SUPERVISOR_LIB_DIR (supervisor.sh's own sibling-script lookup,
# deliberately NOT $CLAUDLOBBY_ROOT -- see the comment beside it in
# lib/supervisor.sh) at a scratch tree holding FAKE install-bot-systemd.sh /
# install-bot.sh stand-ins, never the real ones: the real install-bot.sh
# shells out to the absolute, un-fakeable /bin/launchctl bootstrap, and
# actually bootstrapping a LaunchAgent on the machine running this suite is
# exactly the kind of real side effect a hermetic test must never risk.
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
TMUX_LOG="$T/fake-tmux.log"
export TMUX_LOG

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

# Own log, own file (see the header comment above for why): svc_disenroll's
# tmux teardown leg is OS-independent and unconditional, so every disenroll
# scenario below -- Linux, Darwin, and the unrecognized-OS one -- exercises
# this fake too, and a positive "kill-server was called" check must never be
# confused with a negative "no supervision binary was called" check on the
# unrelated FAKE_LOG.
cat > "$T/bin/tmux" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$TMUX_LOG"
exit 0
EOF
chmod +x "$T/bin/uname" "$T/bin/systemctl" "$T/bin/launchctl" "$T/bin/tmux"
export PATH="$T/bin:$PATH"
export TMUX_BIN="$T/bin/tmux"

# shellcheck source=../lib/lib-common.sh
. "$LIB_DIR/lib-common.sh"

# A syntax error inside the sourced lib/supervisor.sh DOES abort this whole
# suite under `set -euo pipefail` above (MEASURED: bash 3.2.57 on macOS) --
# but this suite's own `trap '...rm -rf "$T"...' EXIT` then runs, and its
# last command's exit status becomes the process's FINAL reported exit code,
# silently overwriting that abort's nonzero one back to 0 (final wave item 8,
# found by an invalid mutant) -- so a direct `bash` invocation of this suite
# can read as a clean pass despite never running a single assertion. This
# guard cannot fire for THAT exact failure (the abort happens inside the `.`
# above, before this line is ever reached); it is defense for the other
# shape of the same class -- a rename or a partial refactor that leaves
# sourcing itself clean but a verb undefined. Fail loudly, immediately,
# naming it, rather than silently running zero of the assertions below
# against functions that do not exist. tests/test_sh_suites.py's own
# stdout/stderr syntax-error check is what actually catches the abort case,
# since it inspects the suite's captured output from OUTSIDE this process.
type svc_kick svc_disenroll >/dev/null 2>&1 || { echo "FAIL: adapter verbs missing after source"; exit 1; }

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
    : > "$TMUX_LOG"
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

# No bot.conf at all (never written, not merely empty-valued): bot_conf_get's
# own [ -f ] guard means BOT_SERVICE and BOT_NAME both resolve to their ""
# default, so svc_unit_name has nothing to fall back to.
BOT_NOCONF="$T/bots/noconf"
mkdir -p "$BOT_NOCONF"
reset_fakes
set +e
out="$(svc_unit_name "$BOT_NOCONF")"; rc=$?
set -e
assert_eq "no bot.conf: prints nothing" "" "$out"
assert_eq "no bot.conf: rc 0 (an unresolved label is a valid answer)" "0" "$rc"

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

# Unrecognized OS: rc 1 regardless of a registered plist/unit sitting right
# there (svc-alpha.plist is still present from the assertion above) -- the
# case statement's `*) return 1 ;;` is unconditional on files, not merely on
# an unresolved label.
as_os SunOS
set +e
svc_is_registered "$BOT"; rc=$?
set -e
assert_eq "Other OS: svc_is_registered rc 1" "1" "$rc"
rm -f "$HOME/Library/LaunchAgents/svc-alpha.plist"

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

# Failure propagation (#1573 fix round 1 item 1): a FAILED restart must read
# as a failure to the caller, never a hardcoded 0 -- the exact bug that made
# PR B's planned `if svc_kick ...; then ... else start-bot.sh fallback; fi`
# unreachable on a real restart failure.
reset_fakes
: > "$HOME/.config/systemd/user/svc-charlie.service"
set +e
out="$(FAKE_EXIT=1 svc_kick "$BOT3")"; rc=$?
set -e
assert_eq "Linux kick (primary, systemctl fails): rc propagates (1), not 0" "1" "$rc"
assert_contains "Linux kick (primary, systemctl fails): still prints its one-line description" "svc-charlie" "$out"
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

echo "=== supervisor.sh contract -- selected actions and loaded identity ==="
reset_fakes
as_os Linux
: > "$HOME/.config/systemd/user/svc-charlie.service"
rc=0
FAKE_EXIT=2 svc_kick "$BOT3" > "$T/kick-description" || rc=$?
assert_eq "native rc 2 is retained" "2" "$rc"
assert_eq "native rc 2 still selected an action" "1" "$SVC_KICK_SELECTED"
reset_fakes
rc=0
svc_kick "$BOT3" > "$T/kick-description" || rc=$?
assert_eq "missing target returns rc 2" "2" "$rc"
assert_eq "missing target resets same-shell selection" "0" "$SVC_KICK_SELECTED"

before_kick() { printf 'callback:%s\n' "$1" >> "$FAKE_LOG"; }
reset_fakes
: > "$HOME/.config/systemd/user/loaded.service"
svc_kick "$BOT3" before_kick loaded charlie > "$T/kick-description"
assert_eq "loaded identity overrides a different bot.conf"     "callback:systemctl --user restart loaded
--user restart loaded.service" "$(cat "$FAKE_LOG")"
assert_eq "callback owns description output" "" "$(cat "$T/kick-description")"

before_kick_fails() { return 17; }
reset_fakes
: > "$HOME/.config/systemd/user/loaded.service"
rc=0
svc_kick "$BOT3" before_kick_fails loaded charlie || rc=$?
assert_eq "callback failure is retained" "17" "$rc"
assert_eq "callback failure is selected, never fallback" "1" "$SVC_KICK_SELECTED"
assert_eq "callback failure prevents native command" "" "$(cat "$FAKE_LOG")"

reset_fakes
: > "$HOME/.config/systemd/user/svc-charlie.service"
rc=0
svc_kick "$BOT3" before_kick "" "" || rc=$?
assert_eq "explicit empty identity never re-reads bot.conf" "0" "$SVC_KICK_SELECTED"
assert_eq "explicit empty identity leaves native command untouched" "" "$(cat "$FAKE_LOG")"

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
assert_contains "Linux disenroll: bot_tmux kill-server called on the resolved socket" "-L svc-echo kill-server" "$(cat "$TMUX_LOG")"

reset_fakes
as_os Darwin
: > "$HOME/Library/LaunchAgents/svc-echo.plist"
: > "$BOT5/.tmux-env"
svc_disenroll "$BOT5"
assert_eq "Darwin disenroll: plist removed" "false" "$([ -f "$HOME/Library/LaunchAgents/svc-echo.plist" ] && echo true || echo false)"
assert_contains "Darwin disenroll: bootout called" "bootout" "$(cat "$FAKE_LOG")"
assert_contains "Darwin disenroll: names svc-echo" "svc-echo" "$(cat "$FAKE_LOG")"
assert_eq "Darwin disenroll: .tmux-env removed (OS-independent teardown leg)" "false" "$([ -f "$BOT5/.tmux-env" ] && echo true || echo false)"
assert_contains "Darwin disenroll: bot_tmux kill-server called on the resolved socket" "-L svc-echo kill-server" "$(cat "$TMUX_LOG")"

# Unrecognized OS: skips the supervision leg entirely (no systemctl/launchctl
# call at all -- FAKE_LOG stays empty) but still runs the OS-independent tmux
# teardown (TMUX_LOG shows kill-server) and still removes .tmux-env, rc 0.
reset_fakes
as_os SunOS
: > "$BOT5/.tmux-env"
set +e
svc_disenroll "$BOT5"; rc=$?
set -e
assert_eq "Other OS: svc_disenroll rc 0" "0" "$rc"
assert_eq "Other OS: no systemctl/launchctl call (supervision leg skipped)" "" "$(cat "$FAKE_LOG")"
assert_contains "Other OS: tmux teardown still runs (OS-independent leg)" "-L svc-echo kill-server" "$(cat "$TMUX_LOG")"
assert_eq "Other OS: .tmux-env still removed" "false" "$([ -f "$BOT5/.tmux-env" ] && echo true || echo false)"

echo "=== supervisor.sh contract -- reaper callbacks and command ownership ==="
reset_fakes
as_os Darwin
: > "$HOME/Library/LaunchAgents/loaded.plist"
reaper_log() { printf 'callback:%s\n' "$*" >> "$FAKE_LOG"; }
svc_disenroll "$BOT5" reaper_log loaded "$T/bin/launchctl" > "$T/reaper-output"
assert_eq "reaper callback owns output" "" "$(cat "$T/reaper-output")"
assert_contains "explicit command and loaded label used" "bootout gui/$(id -u)/loaded" "$(cat "$FAKE_LOG")"
assert_contains "supervision log retains caller text" "callback:launchd agent loaded booted out + plist removed" "$(cat "$FAKE_LOG")"
assert_contains "tmux callback is present" "callback:tmux server -L svc-echo killed" "$(cat "$FAKE_LOG")"
reset_fakes
as_os SunOS
svc_disenroll "$BOT5" reaper_log "" "$T/bin/launchctl"
assert_contains "empty label wins before unsupported OS" "callback:BOT_SERVICE unset — no supervised unit to remove" "$(cat "$FAKE_LOG")"
assert_eq "unsupported empty label invokes no supervision action" "2" "$(wc -l < "$FAKE_LOG" | tr -d ' ')"

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
# A suite that ran zero assertions and never touched FAIL would otherwise
# read as a clean pass below (final wave item 8) -- the exact shape a
# swallowed syntax error upstream produces if the verbs-missing guard above
# were ever removed or bypassed.
[ "$TOTAL" -gt 0 ] || { echo "FAIL: zero assertions ran"; exit 1; }
[ "$FAIL" -eq 0 ] || exit 1
