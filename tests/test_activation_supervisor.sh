#!/bin/bash
# Exact-unit activation controls. Native commands are shell recording functions,
# so neither a PATH mistake nor an absolute executable can reach a supervisor.
set -euo pipefail
TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$TEST_DIR/../lib/supervisor.sh"
T="$(mktemp -d "${TMPDIR:?}/activation-supervisor.XXXXXX")"; trap 'rm -rf "$T"' EXIT
TRACE="$T/trace"; : > "$TRACE"
DELAY_UNLOAD="$T/delay-unload"; DELAY_ON_BOOTOUT=0
file="$T/worker.service"; target=worker.service; : > "$file"
CALLER_RC=0; QUERY_FAIL=0; FAIL_ACTION=""; SHADOW=0; TEST_MANAGER=Aqua; JOB_PID=600
enabled=enabled; load=loaded; active=active; group=/user.slice/worker.service
old_enabled=enabled; launched=1
systemctl() {
    [ "$1" = --user ]; shift
    if [ "$1" = show ]; then
        [ "$QUERY_FAIL" = 0 ] || return 7
        local fragment="$file"
        [ "$load" != masked ] || fragment=/dev/null
        printf 'Id=%s\nLoadState=%s\nActiveState=%s\nUnitFileState=%s\nFragmentPath=%s\nControlGroup=%s\n' \
            "$target" "$load" "$active" "$enabled" "$fragment" "$group"
        return
    fi
    printf '%s\n' "$*" >> "$TRACE"
    [ "$1" != "$FAIL_ACTION" ] || return 9
    case "$1" in
        mask) if [ "$SHADOW" = 0 ]; then load=masked; enabled=masked-runtime; fi ;;
        stop) active=inactive ;;
        unmask) load=loaded; enabled="$old_enabled" ;;
        start) active=active ;;
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
# An inactive, disabled timer stays inactive and disabled after restoration.
file="$T/worker.timer"; target=worker.timer; : > "$file"; : > "$TRACE"
enabled=disabled; old_enabled=disabled; active=inactive; group=""
saved=$(svc_activation_snapshot "$file" "$target")
expect 0 svc_activation_pause "$file" "$target" "$saved"
expect 0 svc_activation_resume "$file" "$target" "$saved"
[ "$(cat "$TRACE")" = "$(printf 'mask --runtime worker.timer\nstop worker.timer\nunmask --runtime worker.timer')" ]

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
python3 - "$TEST_DIR/../lib/supervisor-caller.py" <<'PY'
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
    with patch.object(Path, 'read_text', side_effect=OSError('unreadable')):
        assert module.main('cgroup', '100', '/user.slice/worker.service') == 3
print('PASS: activation ordering, refusal, restoration and membership contracts')
PY
