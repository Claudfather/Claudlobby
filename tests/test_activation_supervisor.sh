#!/bin/bash
# Exact-unit activation controls. Native commands are shell recording functions,
# so neither a PATH mistake nor an absolute executable can reach a supervisor.
set -euo pipefail
TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$TEST_DIR/../claudlobby/_runtime_scripts/supervisor.sh"
T="$(mktemp -d "${TMPDIR:?}/activation-supervisor.XXXXXX")"; trap 'rm -rf "$T"' EXIT
TRACE="$T/trace"; : > "$TRACE"
DELAY_UNLOAD="$T/delay-unload"; DELAY_ON_BOOTOUT=0
file="$T/worker.service"; target=worker.service; : > "$file"
CALLER_RC=0; QUERY_FAIL=0; FAIL_ACTION=""; SHADOW=0; TEST_MANAGER=Aqua; JOB_PID=600
enabled=enabled; load=loaded; active=active; group=/user.slice/worker.service
STOP_SETTLE=0; SETTLE_FILE="$T/settle-count"
old_enabled=enabled; launched=1
systemctl() {
    [ "$1" = --user ]; shift
    if [ "$1" = show ]; then
        [ "$QUERY_FAIL" = 0 ] || return 7
        local fragment="$file"
        [ "$load" != masked ] || fragment="${MASK_FRAGMENT:-/dev/null}"
        if [ "$active" = deactivating ] && [ -f "$SETTLE_FILE" ]; then
            pending=$(cat "$SETTLE_FILE")
            pending=$((pending - 1)); printf '%s' "$pending" > "$SETTLE_FILE"
            [ "$pending" -gt 0 ] || active=inactive
        fi
        printf 'Id=%s\nLoadState=%s\nActiveState=%s\nSubState=%s\nUnitFileState=%s\nFragmentPath=%s\nControlGroup=%s\nMainPID=%s\nControlPID=%s\n' \
            "$target" "$load" "$active" "${sub:-running}" "$enabled" "$fragment" "$group" "${main_pid:-0}" "${control_pid:-0}"
        return
    fi
    printf '%s\n' "$*" >> "$TRACE"
    [ "$1" != "$FAIL_ACTION" ] || return 9
    case "$1" in
        mask) if [ "$SHADOW" = 0 ]; then load=masked; enabled=masked-runtime; fi ;;
        stop) if [ "$STOP_SETTLE" -gt 0 ]; then active=deactivating; else active="${STOP_RESULT:-inactive}"; fi ;;
        unmask) load=loaded; enabled="$old_enabled"; rm -f "${XDG_RUNTIME_DIR:-$T/no-runtime}/systemd/user/$target" ;;
        start) active=active ;;
        disable) active=inactive ;;
        enable) active=active ;;
        reset-failed) [ "$active" != failed ] || active=inactive ;;
        daemon-reload) ;;
        *) return 98 ;;
    esac
}
launchctl() {
    case "$1" in
        manageruid) printf '501\n' ;;
        managername) printf '%s\n' "$TEST_MANAGER" ;;
        list)
            [ "$QUERY_FAIL" = 0 ] || return 7
            printf 'PID\tStatus\tLabel\n799\t0\tcom.apple.Terminal\n'
            if [ -f "$DELAY_UNLOAD" ]; then
                local pending
                pending=$(cat "$DELAY_UNLOAD")
                if [ "$pending" -gt 0 ]; then
                    printf '%s' "$((pending - 1))" > "$DELAY_UNLOAD"
                    printf '%s\t0\t%s\n' "$JOB_PID" "${target##*/}"
                    return
                fi
            fi
            [ "$launched" = 0 ] || printf '%s\t0\t%s\n' "$JOB_PID" "${target##*/}"
            ;;
        bootout|bootstrap)
            printf '%s\n' "$*" >> "$TRACE"
            [ "$1" != "$FAIL_ACTION" ] || return 9
            if [ "$1" = bootout ]; then
                launched=0
                if [ "$DELAY_ON_BOOTOUT" -gt 0 ]; then
                    printf '%s' "$DELAY_ON_BOOTOUT" > "$DELAY_UNLOAD"
                fi
            else launched=1; fi
            ;;
        *) return 98 ;;
    esac
}
python3() { return "$CALLER_RC"; }
expect() {
    local want="$1" rc=0; shift
    "$@" || rc=$?
    [ "$rc" = "$want" ] || { printf 'FAIL: expected rc %s, got %s: %s\n' "$want" "$rc" "$*"; exit 1; }
}
unchanged() { [ ! -s "$TRACE" ] || { cat "$TRACE"; exit 1; }; }
_OS=Linux
saved=$(svc_activation_snapshot "$file" "$target")
[ "$saved" = 'enabled loaded active' ]
CALLER_RC=1; expect 1 svc_activation_pause "$file" "$target" "$saved"; unchanged
CALLER_RC=3; expect 3 svc_activation_pause "$file" "$target" "$saved"; unchanged
CALLER_RC=0; QUERY_FAIL=1; expect 3 svc_activation_pause "$file" "$target" "$saved"; unchanged
QUERY_FAIL=0; SHADOW=1
expect 3 svc_activation_pause "$file" "$target" "$saved"
[ "$(cat "$TRACE")" = 'mask --runtime worker.service' ]  # no stop after an ineffective mask
: > "$TRACE"; SHADOW=0; FAIL_ACTION=mask
expect 9 svc_activation_pause "$file" "$target" "$saved"
[ "$(cat "$TRACE")" = 'mask --runtime worker.service' ]
: > "$TRACE"; FAIL_ACTION=""
mv "$file" "$file.parked"  # enrollment owner, not the adapter, parks the file
expect 0 svc_activation_pause "$file" "$target" "$saved"
expect 3 svc_activation_resume "$file" "$target" "$saved"
mv "$file.parked" "$file"
expect 0 svc_activation_resume "$file" "$target" "$saved"
[ "$(cat "$TRACE")" = "$(printf 'mask --runtime worker.service\nstop worker.service\nunmask --runtime worker.service\nstart worker.service')" ]
[ -f "$file" ]
# The exact timer may remain deactivating briefly after a successful stop.
file="$T/settling.timer"; target=settling.timer; : > "$file"; : > "$TRACE"
enabled=enabled; old_enabled=enabled; load=loaded; active=active; group=""
saved=$(svc_activation_snapshot "$file" "$target")
STOP_SETTLE=3
printf 3 > "$SETTLE_FILE"
expect 0 svc_activation_pause "$file" "$target" "$saved"
[ "$(cat "$SETTLE_FILE")" -le 0 ]
rm "$SETTLE_FILE"
STOP_SETTLE=0
# Parking its service first leaves the stopped, masked timer failed ("Unit to
# trigger vanished"). Only that exact timer's failure is reset, then verified.
file="$T/vanished.timer"; target=vanished.timer; : > "$file"; : > "$TRACE"
enabled=enabled; old_enabled=enabled; load=loaded; active=active; group=""
saved=$(svc_activation_snapshot "$file" "$target")
STOP_RESULT=failed
expect 0 svc_activation_pause "$file" "$target" "$saved"
[ "$(cat "$TRACE")" = "$(printf 'mask --runtime vanished.timer\nstop vanished.timer\nreset-failed vanished.timer')" ]
[ "$load:$active" = masked:inactive ]
load=loaded; enabled=enabled; active=active; : > "$TRACE"; FAIL_ACTION=reset-failed
expect 9 svc_activation_pause "$file" "$target" "$saved"  # a failed reset still refuses
FAIL_ACTION=""
# A service left failed after stop is never reset; pause refuses.
file="$T/vanished.service"; target=vanished.service; : > "$file"; : > "$TRACE"
load=loaded; enabled=enabled; active=active; group=/user.slice/vanished.service
saved=$(svc_activation_snapshot "$file" "$target")
expect 3 svc_activation_pause "$file" "$target" "$saved" 2>/dev/null
if grep -q reset-failed "$TRACE"; then echo 'FAIL: service failure was reset' >&2; exit 1; fi
# Only owner-declared timer services may clear a terminal failed marker.
load=loaded; enabled=enabled; old_enabled=enabled; active=failed; main_pid=0; control_pid=0; : > "$TRACE"
expect 0 svc_activation_pause "$file" "$target" 'enabled loaded inactive' "$$" timer-owned
[ "$(cat "$TRACE")" = "$(printf 'mask --runtime vanished.service\nstop vanished.service\nreset-failed vanished.service')" ]
[ "$load:$active" = masked:inactive ]
expect 0 svc_activation_resume "$file" "$target" 'enabled loaded inactive'
if grep -q '^start ' "$TRACE"; then echo 'FAIL: failed timer job replayed' >&2; exit 1; fi
load=loaded; enabled=enabled; active=failed; main_pid=42; : > "$TRACE"
expect 3 svc_activation_pause "$file" "$target" 'enabled loaded inactive' "$$" timer-owned 2>/dev/null
if grep -q reset-failed "$TRACE"; then echo 'FAIL: live producer failure reset' >&2; exit 1; fi
main_pid=0; control_pid=42; load=loaded; enabled=enabled; : > "$TRACE"
expect 3 svc_activation_pause "$file" "$target" 'enabled loaded inactive' "$$" timer-owned 2>/dev/null
if grep -q reset-failed "$TRACE"; then echo 'FAIL: live control failure reset' >&2; exit 1; fi
control_pid=0; load=loaded; enabled=enabled; active=failed; FAIL_ACTION=reset-failed; : > "$TRACE"
expect 9 svc_activation_pause "$file" "$target" 'enabled loaded inactive' "$$" timer-owned
FAIL_ACTION=""
STOP_RESULT=""
# An inactive, disabled timer stays inactive and disabled after restoration.
file="$T/worker.timer"; target=worker.timer; : > "$file"; : > "$TRACE"
enabled=disabled; old_enabled=disabled; load=loaded; active=inactive; group=""
saved=$(svc_activation_snapshot "$file" "$target")
expect 0 svc_activation_pause "$file" "$target" "$saved"
expect 0 svc_activation_resume "$file" "$target" "$saved"
[ "$(cat "$TRACE")" = "$(printf 'mask --runtime worker.timer\nstop worker.timer\nunmask --runtime worker.timer')" ]

# A queued auto-restart has no current process, but caller ancestry still
# requires a kernel cgroup proof against this exact unit name.
file="$T/worker.service"; target=worker.service; load=loaded; enabled=enabled
active=activating; sub=auto-restart; group=""; main_pid=0; control_pid=0
CALLER_RC=0; expect 0 svc_activation_assert_external "$file" "$target"
CALLER_RC=1; expect 1 svc_activation_assert_external "$file" "$target"
CALLER_RC=0; main_pid=42; expect 3 svc_activation_assert_external "$file" "$target"
main_pid=0; sub=running; expect 3 svc_activation_assert_external "$file" "$target"
sub=failed; active=failed; expect 0 svc_activation_assert_external "$file" "$target"
quiet=$(svc_activation_quiet "$file" "$target")
[ "$quiet" = $'inactive\tno-cgroup-witness' ]
main_pid=42; expect 3 svc_activation_quiet "$file" "$target"
main_pid=0
active=inactive; sub=""

mkdir -p "$T/loop-source" "$T/loop-installed" "$T/loop-bot"
loop_source="$T/loop-source/worker.service"
loop_installed="$T/loop-installed/worker.service"
printf 'owned loop\n' > "$loop_source"; cp "$loop_source" "$loop_installed"
file="$loop_installed"; target=worker.service; active=activating; sub=auto-restart
group=""; main_pid=0; control_pid=0; CALLER_RC=1; : > "$TRACE"
expect 3 svc_bot_disenroll_exact "$loop_source" "$loop_installed" "$target" "$T/loop-bot" worker "$T"
[ -f "$loop_installed" ] && [ ! -s "$TRACE" ]
CALLER_RC=0
loop_result=$(svc_bot_disenroll_exact "$loop_source" "$loop_installed" "$target" "$T/loop-bot" worker "$T")
[ "$loop_result" = effect-attempted ] && [ ! -e "$loop_installed" ]
[ "$(cat "$TRACE")" = "$(printf 'disable --now worker.service\ndaemon-reload')" ]
: > "$TRACE"; active=failed; sub=failed
expect 0 svc_bot_enroll_exact "$loop_source" "$loop_installed" "$target"
[ -f "$loop_installed" ]
[ "$(cat "$TRACE")" = "$(printf 'daemon-reload\nreset-failed worker.service\nenable --now worker.service')" ]

# systemd reports a runtime mask by its own link. Only this user's exact
# runtime link to /dev/null for the named target counts as masked.
export XDG_RUNTIME_DIR="$T/run"; mkdir -p "$XDG_RUNTIME_DIR/systemd/user" "$T/foreign-run"
file="$T/worker.service"; target=worker.service; load=masked; enabled=masked-runtime
active=inactive; sub=dead; group=""; old_enabled=enabled
MASK_FRAGMENT="$XDG_RUNTIME_DIR/systemd/user/worker.service"
ln -s "$file" "$MASK_FRAGMENT"
expect 3 svc_activation_snapshot "$file" "$target"
MASK_FRAGMENT="$T/foreign-run/worker.service"; ln -s /dev/null "$MASK_FRAGMENT"
expect 3 svc_activation_snapshot "$file" "$target"
MASK_FRAGMENT="$XDG_RUNTIME_DIR/systemd/user/worker.service"
rm "$MASK_FRAGMENT"; ln -s /dev/null "$MASK_FRAGMENT"
[ "$(svc_activation_snapshot "$file" "$target")" = 'masked-runtime masked inactive' ]
: > "$TRACE"; expect 0 svc_activation_resume "$file" "$target" 'enabled loaded active'
[ "$(cat "$TRACE")" = "$(printf 'unmask --runtime worker.service\nstart worker.service')" ]
# Residue: the restored higher-priority file loads, hiding a surviving runtime
# mask link. Only that exact link is removed; nothing is started.
ln -s /dev/null "$MASK_FRAGMENT"
[ "$(svc_activation_snapshot "$file" "$target")" = 'enabled loaded active' ]
: > "$TRACE"; [ "$(svc_activation_clear_runtime_mask "$file" "$target" 'enabled loaded active')" = removed ]
[ "$(cat "$TRACE")" = 'unmask --runtime worker.service' ] && [ ! -L "$MASK_FRAGMENT" ]
: > "$TRACE"; [ "$(svc_activation_clear_runtime_mask "$file" "$target" 'enabled loaded active')" = absent ]; unchanged
ln -s "$file" "$MASK_FRAGMENT"
expect 3 svc_activation_clear_runtime_mask "$file" "$target" 'enabled loaded active' 2>/dev/null; unchanged
expect 3 svc_activation_clear_runtime_mask "$file" "$target" 'masked-runtime masked inactive' 2>/dev/null; unchanged
rm "$MASK_FRAGMENT"
# Publication owns hidden-mask removal; start never starts over a surviving one.
ln -s /dev/null "$MASK_FRAGMENT"; active=inactive; : > "$TRACE"
expect 3 svc_activation_start "$file" "$target" 2>/dev/null
[ "$(cat "$TRACE")" = daemon-reload ] && [ -L "$MASK_FRAGMENT" ]
rm "$MASK_FRAGMENT"; : > "$TRACE"
[ "$(svc_activation_start "$file" "$target")" = start-requested ]
[ "$(cat "$TRACE")" = "$(printf 'daemon-reload\nstart worker.service')" ]
unset MASK_FRAGMENT XDG_RUNTIME_DIR
# The abort reload helper only reloads the user manager; it is Linux-only.
: > "$TRACE"; expect 0 svc_activation_reload; [ "$(cat "$TRACE")" = daemon-reload ]
: > "$TRACE"; _OS=Darwin; expect 3 svc_activation_reload 2>/dev/null; unchanged; _OS=Linux

_OS=Darwin; file="$T/fleet.keepalive.plist"; target=gui/501/fleet.keepalive
: > "$file"; : > "$TRACE"
saved=$(svc_activation_snapshot "$file" "$target")
[ "$saved" = 'unchanged loaded active' ]
CALLER_RC=1; expect 1 svc_activation_pause "$file" "$target" "$saved"; unchanged
CALLER_RC=3; expect 3 svc_activation_pause "$file" "$target" "$saved"; unchanged
CALLER_RC=0; TEST_MANAGER=Background
expect 3 svc_activation_pause "$file" "$target" "$saved"; unchanged
TEST_MANAGER=Aqua; FAIL_ACTION=bootout
expect 9 svc_activation_pause "$file" "$target" "$saved"
[ "$launched" = 1 ]; : > "$TRACE"; FAIL_ACTION=""
expect 0 svc_activation_pause "$file" "$target" "$saved"
expect 0 svc_activation_resume "$file" "$target" "$saved"
[ "$(cat "$TRACE")" = "$(printf 'bootout %s\nbootstrap gui/501 %s' "$target" "$file")" ]
[ -f "$file" ]
# A successful bootout can remain visible briefly; wait for the exact target.
launched=1; : > "$TRACE"; DELAY_ON_BOOTOUT=2
saved=$(svc_activation_snapshot "$file" "$target")
expect 0 svc_activation_pause "$file" "$target" "$saved"
[ "$(cat "$DELAY_UNLOAD")" = 0 ]
[ "$(cat "$TRACE")" = "bootout $target" ]
# A job that never unloads still refuses, with the target named on stderr.
launched=1; : > "$TRACE"; DELAY_ON_BOOTOUT=99
saved=$(svc_activation_snapshot "$file" "$target")
if svc_activation_pause "$file" "$target" "$saved" 2> "$T/pause.err"; then
    echo 'FAIL: persistent loaded job passed pause' >&2; exit 1
fi
grep -Fq "$target did not unload after bootout" "$T/pause.err"
rm "$DELAY_UNLOAD"; DELAY_ON_BOOTOUT=0
launched=0; : > "$TRACE"; saved=$(svc_activation_snapshot "$file" "$target")
expect 0 svc_activation_pause "$file" "$target" "$saved"
expect 0 svc_activation_resume "$file" "$target" "$saved"; unchanged

# A detached manager has no loaded-job PID in launchctl's list. The exact
# inactive bot unit can be removed without booting out a running process,
# while the general activation ancestry guard remains strict.
mkdir -p "$T/generated" "$T/installed" "$T/bot"
source_unit="$T/generated/worker.plist"
installed_unit="$T/installed/worker.plist"
printf 'selected worker\n' > "$source_unit"
cp "$source_unit" "$installed_unit"
file="$installed_unit"; target=gui/501/worker; launched=1; JOB_PID=600; CALLER_RC=1
: > "$TRACE"
expect 3 svc_bot_disenroll_exact "$source_unit" "$installed_unit" "$target" "$T/bot" worker "$T"
[ -f "$installed_unit" ]; unchanged
JOB_PID=-; CALLER_RC=3
: > "$TRACE"
expect 3 svc_activation_assert_external "$installed_unit" "$target"
unchanged
stop_output=$(svc_bot_disenroll_exact "$source_unit" "$installed_unit" "$target" "$T/bot" worker "$T")
[ "$stop_output" = effect-attempted ]
[ ! -e "$installed_unit" ]
[ "$(cat "$TRACE")" = "bootout $target" ]
JOB_PID=600; CALLER_RC=0

# Exercise the actual membership predicates with observed-data fixtures; only
# kernel reads are replaced. No real process ownership or service is queried.
unset -f python3
python3 - "$TEST_DIR/../claudlobby/_runtime_scripts/supervisor-caller.py" <<'PY'
import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('caller', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
with patch.object(module, 'ancestry', return_value=[100, 600]):
    assert module.main('launchd', '100', '600:600 799') == 1
    assert module.main('launchd', '100', '799:600 799') == 0
    assert module.main('launchd', '100', '-:799') == 3
    with patch.object(Path, 'read_text', return_value='0::/user.slice/worker.service/child\n'):
        assert module.main('cgroup', '100', '/user.slice/worker.service') == 1
        assert module.main('cgroup', '100', '/user.slice/work') == 0
        assert module.main('unit', '100', 'worker.service') == 1
        assert module.main('unit', '100', 'other.service') == 0
        assert module.main('unit', '100', '../worker.service') == 3
    with patch.object(Path, 'read_text', return_value='0::/user.slice/worker\\x2dother.service\n'):
        assert module.main('unit', '100', 'worker-other.service') == 3
    with patch.object(Path, 'read_text', side_effect=OSError('unreadable')):
        assert module.main('cgroup', '100', '/user.slice/worker.service') == 3
print('PASS: activation ordering, refusal, restoration and membership contracts')
PY
