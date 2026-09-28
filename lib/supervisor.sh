#!/bin/bash
# lib/supervisor.sh — the supervisor adapter: five verbs (svc_is_registered,
# svc_state, svc_kick, svc_enroll, svc_disenroll) plus svc_unit_name, the
# shared label resolver, each with a systemd spelling and a launchd
# spelling, behind the one $_OS switch every lib/ script already
# re-derives for itself (#1573 boot admission, task 6). Two of the five
# (svc_is_registered, svc_state) call the resolver; svc_kick and
# svc_disenroll resolve the label inline, exactly as the code they were
# moved from did.
#
# Sourced by lib-common.sh immediately after detect_os runs, so every verb
# below can read $_OS without re-deriving it. Bodies are MOVED from the code
# that already carries them — keepalive.sh's restart ladder (svc_kick),
# install-bot-systemd.sh / install-bot.sh (svc_enroll calls them; PR B moves
# their bodies in and turns them into thin wrappers), and spin-down-bot.sh's
# supervision leg (svc_disenroll) — not reinvented here.
#
# keepalive.sh and spin-up/down-bot.sh use the action verbs below. Readers
# and installer bodies retain their separate contracts under #1607. The
# adapter is sourced and contract-tested
# (tests/test_supervisor_adapter.sh) against fake systemctl/launchctl
# binaries. tests/test_supervisor_ratchet.py fences every OTHER lib/ file's
# direct systemctl/launchctl calls at their current count — this file is the
# one place that count is allowed to grow, which is why it is excluded from
# that scan.
#
#   svc_unit_name <bot_dir>       — BOT_SERVICE, or the pre-rename BOT_NAME
#                                    fallback while a unit/plist by that name
#                                    exists; prints the bare label.
#   svc_is_registered <bot_dir>   — rc 0/1: is a unit/plist installed.
#   svc_state <bot_dir>           — prints loaded-active | loaded-inactive |
#                                    not-loaded | unknown.
#   svc_kick <bot_dir> [...]      — restart/kickstart; SVC_KICK_SELECTED=0
#                                    and rc 2 if no branch applies. A selected
#                                    action preserves native status (also 2).
#                                    The caller owns a no-target fallback.
#   svc_enroll <bot_dir>          — installs + starts the composed unit.
#   svc_disenroll <bot_dir>       — removes supervision + the tmux server the
#                                    unit/plist cannot hook.
#
# Every verb resolves the OS through $_OS (set by detect_os). An OS neither
# Linux nor Darwin invokes no external binary and reports the fact in the
# shape each verb already defines (svc_state prints "unknown"; svc_is_registered
# returns 1; svc_kick / svc_enroll return 2) rather than guessing.
#
# bash 3.2 safe (tests/test_bash_parse.py covers lib/): no apostrophes inside
# $( ), printf '%s' for values, and every probing external call guarded with
# `|| true` where "not found" is a state, not an error — mirroring exactly
# which calls carry that guard in the source they were moved from. Action
# calls preserve their own status explicitly. Callers capture it to distinguish
# selection from failure, then restore their unguarded ERR/errexit boundary;
# capture must never turn a failed action into a second startup attempt.
#
# svc_enroll's sibling-script lookup is deliberately NOT $CLAUDLOBBY_ROOT/lib
# (see the comment beside this file's own source line in lib-common.sh for
# why): it is this file's own directory, self-derived from ${BASH_SOURCE[0]}
# exactly once, the same way lib-common.sh derives CLAUDLOBBY_ROOT from its
# own location rather than trusting an inherited variable. The `:=` form
# leaves a deliberate seam, and it is honoured in PRODUCTION too, not only in
# tests: any caller may pre-export _SUPERVISOR_LIB_DIR to redirect which
# installer svc_enroll executes. tests/test_supervisor_adapter.sh's hermetic
# svc_enroll cases use that same lever to point at a scratch dir of stand-in
# scripts rather than ever forking the real install-bot.sh, whose Darwin leg
# shells out to the absolute, unfakeable /bin/launchctl bootstrap -- a real
# call a hermetic test must never risk making.
#
# The default is a parameter expansion, not `$(cd "$(dirname ...)" && pwd)`:
# that idiom forks a subshell on every source, and lib-common.sh (which
# sources this file on hot paths) already assigns the variable from its own
# forkless derivation before the source line, so in the normal case this
# expansion never fires at all. It stays for a DIRECT source of this file.
case "${_SUPERVISOR_LIB_DIR:-}" in
    "")
        case "${BASH_SOURCE[0]}" in
            */*) _SUPERVISOR_LIB_DIR="${BASH_SOURCE[0]%/*}" ;;
            *)   _SUPERVISOR_LIB_DIR="." ;;
        esac
        ;;
esac

# svc_bot_unit_owned_by <unit-file> <bot-dir>
# A label suffix is only a candidate, never ownership (#1811). Missing or
# unreadable ownership preserves the installed unit. The reader is stdlib-only
# and reads WorkingDirectory; no unit content is sourced or executed.
svc_bot_unit_owned_by() {
    local unit="${1:?unit file required}" bot_dir="${2:?bot directory required}" rc=0 output=""
    if command -v python3 >/dev/null 2>&1; then
        output="$(python3 "$_SUPERVISOR_LIB_DIR/bot-unit-owner.py" "$unit" "$bot_dir" 2>&1)" || rc=$?
        # The reader has no output protocol. A traceback from a failed reader
        # must not be mistaken for its rc 1 (a known foreign owner).
        if [ -z "$output" ]; then
            case "$rc" in
                0|1) return "$rc" ;;
            esac
        fi
    fi
    printf 'unit ownership unknown; preserving %s (reader unavailable or unsupported unit)\n' "$unit" >&2
    return 3
}

# svc_unit_name <bot_dir>
# The shared label resolver. svc_is_registered and svc_state build on it;
# svc_kick and svc_disenroll read BOT_SERVICE inline, as the code they were
# moved from did, and svc_enroll needs no label at all. BOT_SERVICE is
# the source of truth; when it is unset (or explicitly empty — bot_conf_get's
# ${val:-$default} treats both the same), the pre-rename fallback is BOT_NAME,
# but ONLY while a unit/plist by that bare name is actually installed —
# keepalive.sh's restart_bot_service carries the identical fallback shape.
# Always rc 0: an unresolved label is a valid answer (the empty string), not
# a failure — callers that need a boolean ask svc_is_registered instead.
svc_unit_name() {
    local bot_dir="${1:?Usage: svc_unit_name <bot_dir>}"
    local label
    label="$(bot_conf_get "$bot_dir" BOT_SERVICE "")"
    if [ -n "$label" ]; then
        printf '%s' "$label"
        return 0
    fi
    local bot_name
    bot_name="$(bot_conf_get "$bot_dir" BOT_NAME "")"
    [ -n "$bot_name" ] || return 0
    case "$_OS" in
        Linux)
            [ -f "$HOME/.config/systemd/user/$bot_name.service" ] && printf '%s' "$bot_name"
            ;;
        Darwin)
            [ -f "$HOME/Library/LaunchAgents/$bot_name.plist" ] && printf '%s' "$bot_name"
            ;;
    esac
    return 0
}

# svc_is_registered <bot_dir>
# rc 0 = a unit/plist is installed for the resolved label; rc 1 otherwise
# (including an unresolved label, or an OS this adapter does not recognize).
svc_is_registered() {
    local bot_dir="${1:?Usage: svc_is_registered <bot_dir>}"
    local label
    label="$(svc_unit_name "$bot_dir")"
    [ -n "$label" ] || return 1
    case "$_OS" in
        Linux)  [ -f "$HOME/.config/systemd/user/$label.service" ] ;;
        Darwin) [ -f "$HOME/Library/LaunchAgents/$label.plist" ] ;;
        *)      return 1 ;;
    esac
}

# svc_state <bot_dir>
# Prints exactly one of: loaded-active | loaded-inactive | not-loaded | unknown.
# Linux reads ActiveState + LoadState from ONE `systemctl --user show` (never
# two calls — service_is_starting's own comment explains why: two calls can
# straddle a state change), parsed by NAME, never by position — the same
# defensive shape service_is_starting uses just above this file's source
# point, because systemctl does not promise to emit properties in request
# order. Darwin has no cheap sub-state query; `launchctl print` exiting
# nonzero means not loaded at all, and a loaded agent is active only while a
# `state = running` line is present — matched with a bash `case` pattern,
# never piped to `grep -q`: `grep -q` exits the instant it finds a match,
# without draining the rest of stdin, so on a large `launchctl print` output
# (~120KB, measured) the upstream `printf` can still be writing when `grep`
# closes its read end, SIGPIPEs, and hands the pipeline a nonzero status
# under the caller's `pipefail` — inverting a MATCHING "running" verdict to
# "not running" for exactly the bots whose launchd output is biggest.
svc_state() {
    local bot_dir="${1:?Usage: svc_state <bot_dir>}"
    local label
    label="$(svc_unit_name "$bot_dir")"
    if [ -z "$label" ]; then
        printf 'unknown'
        return 0
    fi
    case "$_OS" in
        Linux)
            local active="" loaded="" _k _v
            while IFS='=' read -r _k _v; do
                case "$_k" in
                    ActiveState) active=$_v ;;
                    LoadState) loaded=$_v ;;
                esac
            done <<EOF
$(systemctl --user show -p ActiveState -p LoadState "$label.service" 2>/dev/null | tr -d '\r')
EOF
            if [ -z "$loaded" ] || [ "$loaded" = "not-found" ]; then
                printf 'not-loaded'
            elif [ "$active" = "active" ]; then
                printf 'loaded-active'
            else
                printf 'loaded-inactive'
            fi
            ;;
        Darwin)
            local out
            if out=$(launchctl print "gui/$(id -u)/$label" 2>/dev/null); then
                case "$out" in
                    *'state = running'*) printf 'loaded-active' ;;
                    *)                   printf 'loaded-inactive' ;;
                esac
            else
                printf 'not-loaded'
            fi
            ;;
        *)
            printf 'unknown'
            ;;
    esac
    return 0
}

# svc_kick <bot_dir> [before_action_function [loaded_service [loaded_name]]]
# Resolve once, then announce before acting. With a callback, it owns output;
# without one, preserve the original printed-description contract. Explicit
# identity arguments are snapshots sourced by the caller, including an empty
# value. The one-argument API retains bot_conf_get parsing for direct users.
# SVC_KICK_SELECTED is a SAME-SHELL result: 0 means no target (rc 2), while 1
# means a target was selected, even if the callback or native command failed.
# In particular native rc 2 must never license an extra start-bot fallback.
svc_kick() {
    local bot_dir="${1:?Usage: svc_kick <bot_dir>}"
    local before_action="${2:-}" bot_service bot_name desc target kind rc
    SVC_KICK_SELECTED=0
    if [ "$#" -ge 3 ]; then bot_service="$3"; else bot_service="$(bot_conf_get "$bot_dir" BOT_SERVICE "")"; fi
    if [ "$#" -ge 4 ]; then bot_name="$4"; else bot_name="$(bot_conf_get "$bot_dir" BOT_NAME "")"; fi
    if [ "$_OS" = "Linux" ] && [ -n "$bot_service" ] && [ -f "$HOME/.config/systemd/user/$bot_service.service" ]; then
        desc="systemctl --user restart $bot_service"
        target="$bot_service.service"; kind=systemd
    elif [ "$_OS" = "Linux" ] && { [ "$#" -ge 4 ] || [ -n "$bot_name" ]; } && [ -f "$HOME/.config/systemd/user/$bot_name.service" ]; then
        desc="systemctl --user restart $bot_name (pre-rename)"
        target="$bot_name.service"; kind=systemd
    elif [ "$_OS" = "Darwin" ] && [ -n "$bot_service" ] && [ -f "$HOME/Library/LaunchAgents/$bot_service.plist" ]; then
        desc="launchctl kickstart $bot_service"
        target="gui/$(id -u)/$bot_service"; kind=launchd
    else
        return 2
    fi
    SVC_KICK_SELECTED=1
    if [ -n "$before_action" ]; then
        "$before_action" "$desc" || return $?
    else
        printf '%s\n' "$desc" || return $?
    fi
    rc=0
    case "$kind" in
        systemd) systemctl --user restart "$target" || rc=$? ;;
        launchd) launchctl kickstart -k "$target" || rc=$? ;;
    esac
    return "$rc"
}

# svc_restart_host <unit>
# Restart an installed HOST service by its composed name (claudlobby-plane-daemon,
# claudlobby-plane-view), for a caller that moved the code it runs (#1251's
# pull-root). svc_kick resolves a BOT's label from bot.conf; a host service has
# no bot.conf, and its unit name IS its label. rc 0 restarted; the supervisor's
# own rc when it refused; rc 2 when no unit by that name is installed here, so
# nothing was invoked -- a host that never enrolled the service is not a failed
# restart.
svc_restart_host() {
    local unit="${1:?Usage: svc_restart_host <unit>}" rc=0
    if [ "$_OS" = "Linux" ] && [ -f "$HOME/.config/systemd/user/$unit.service" ]; then
        systemctl --user restart "$unit.service" || rc=$?
        return "$rc"
    elif [ "$_OS" = "Darwin" ] && [ -f "$HOME/Library/LaunchAgents/$unit.plist" ]; then
        launchctl kickstart -k "gui/$(id -u)/$unit" || rc=$?
        return "$rc"
    fi
    return 2
}

# svc_enroll <bot_dir>
# In THIS PR, a thin dispatcher onto the two existing installer scripts —
# install-bot-systemd.sh's sequence (stale-unit cleanup, copy the composed
# unit, daemon-reload, enable --now) and install-bot.sh's (stale-plist
# cleanup, copy, bootstrap) are NOT duplicated here; PR B moves those bodies
# in and turns the two scripts into wrappers around this verb. An
# unrecognized OS invokes neither script.
svc_enroll() {
    local bot_dir="${1:?Usage: svc_enroll <bot_dir>}"
    case "$_OS" in
        Linux)  "$_SUPERVISOR_LIB_DIR/install-bot-systemd.sh" "$bot_dir" ;;
        Darwin) "$_SUPERVISOR_LIB_DIR/install-bot.sh" "$bot_dir" ;;
        *)
            printf 'svc_enroll: unsupported OS (%s) -- nothing to enroll with\n' "$_OS" >&2
            return 2
            ;;
    esac
}

# svc_disenroll <bot_dir> [log_function [loaded_service [launchctl_command]]]
# Supervision first, then the OS-independent private tmux teardown. No legacy
# BOT_NAME fallback: spin-down never had one. A callback receives the existing
# spin-down log text; absent a callback, preserve the adapter diagnostic voice.
# The production reaper passes /bin/launchctl explicitly, preserving its binary
# resolution. The optional command only scopes direct contract-test invocations;
# there is no host-global binary override.
svc_disenroll() {
    local bot_dir="${1:?Usage: svc_disenroll <bot_dir>}"
    local logger="${2:-}" label launchctl_command="${4:-launchctl}" message
    if [ "$#" -ge 3 ]; then label="$3"; else label="$(bot_conf_get "$bot_dir" BOT_SERVICE "")"; fi
    if [ -z "$label" ]; then
        if [ -n "$logger" ]; then
            "$logger" "BOT_SERVICE unset — no supervised unit to remove" || return $?
        else
            case "$_OS" in
                Linux) printf 'svc_disenroll: BOT_SERVICE unset for %s -- no supervised unit to remove\n' "$bot_dir" ;;
                Darwin) printf 'svc_disenroll: BOT_SERVICE unset for %s -- no supervised agent to remove\n' "$bot_dir" ;;
                *) printf 'svc_disenroll: unsupported OS (%s) -- skipping supervision leg\n' "$_OS" >&2 ;;
            esac
        fi
    else
        case "$_OS" in
            Linux)
                local ud="$HOME/.config/systemd/user"
                systemctl --user disable --now "$label.service" 2>/dev/null || true
                rm -f "$ud/$label.service" "$ud/default.target.wants/$label.service"
                systemctl --user daemon-reload 2>/dev/null || true
                systemctl --user reset-failed "$label.service" 2>/dev/null || true
                message="systemd user unit $label.service stopped + disabled + removed"
                ;;
            Darwin)
                "$launchctl_command" bootout "gui/$(id -u)/$label" 2>/dev/null || true
                rm -f "$HOME/Library/LaunchAgents/$label.plist"
                message="launchd agent $label booted out + plist removed"
                ;;
            *) message="unsupported OS ($_OS) — skipping supervision leg" ;;
        esac
        if [ -n "$logger" ]; then
            "$logger" "$message" || return $?
        elif [ "$_OS" = Linux ] || [ "$_OS" = Darwin ]; then
            printf '%s\n' "$message"
        else
            printf 'svc_disenroll: unsupported OS (%s) -- skipping supervision leg\n' "$_OS" >&2
        fi
    fi
    local sock
    sock="$(tmux_socket_for_bot "$bot_dir" 2>/dev/null)" || sock=""
    if [ -n "$sock" ]; then
        bot_tmux "$sock" kill-server 2>/dev/null || true
        [ -z "$logger" ] || "$logger" "tmux server -L $sock killed" || return $?
    else
        [ -z "$logger" ] || "$logger" "no resolvable tmux socket — skipping tmux leg" || return $?
    fi
    rm -f "$bot_dir/.tmux-env" 2>/dev/null || true
    return 0
}

# Private activation controls. FILE/TARGET come from the verified enrollment
# manifest, not a label glob. TARGET is a systemd basename (including suffix),
# or an explicit launchd gui/<uid>/<label> or user/<uid>/<label>.
# Snapshot stdout is three atoms: unit-file state, load state, active state.
# The activation owner journals it BEFORE parking installed systemd files; pause
# accepts a parked FILE, resume requires it restored. This adapter never removes
# source files or enablement links. Runtime masks do NOT survive reboot: durable
# activation/start admission belongs to the activation owner, not this adapter.
# Preflight EVERY target before parking any. Pause rechecks caller membership.
_svc_activation_unknown() {
    printf 'activation supervision unknown: %s\n' "$*" >&2
    return 3
}

_svc_activation_read() {
    local file="$1" target="$2" output key value seen=" " uid manager pid status label extra count=0
    case "$file" in /*) ;; *) _svc_activation_unknown "installed path is not absolute"; return 3 ;; esac
    SVC_ACT_FILE_STATE=""; SVC_ACT_LOAD=""; SVC_ACT_ACTIVE=""; SVC_ACT_GROUP=""; SVC_ACT_PID="-"; SVC_ACT_JOB_PIDS=""
    case "$_OS" in
        Linux)
            case "$target" in *[!a-zA-Z0-9_.@-]*|'') return 3 ;; esac
            case "$target" in *.service|*.timer|*.socket|*.path) ;; *) return 3 ;; esac
            [ "${file##*/}" = "$target" ] || return 3
            output=$(systemctl --user show --property=Id,LoadState,ActiveState,UnitFileState,FragmentPath,ControlGroup "$target") || return 3
            local identity="" fragment=""
            while IFS='=' read -r key value; do
                case "$seen" in *" $key "*) return 3 ;; esac
                seen="$seen$key "
                case "$key" in
                    Id) identity="$value" ;;
                    LoadState) SVC_ACT_LOAD="$value" ;;
                    ActiveState) SVC_ACT_ACTIVE="$value" ;;
                    UnitFileState) SVC_ACT_FILE_STATE="$value" ;;
                    FragmentPath) fragment="$value" ;;
                    ControlGroup) SVC_ACT_GROUP="$value" ;;
                    *) return 3 ;;
                esac
            done <<EOF
$output
EOF
            [ "$identity" = "$target" ] || return 3
            for key in Id LoadState ActiveState UnitFileState FragmentPath; do
                case "$seen" in *" $key "*) ;; *) return 3 ;; esac
            done
            if [ "${target##*.}" = service ]; then
                case "$seen" in *' ControlGroup '*) ;; *) return 3 ;; esac
            fi
            case "$SVC_ACT_LOAD" in
                loaded) [ "$fragment" = "$file" ] || return 3 ;;
                masked) [ "$fragment" = /dev/null ] || return 3 ;;
                not-found) [ ! -e "$file" ] || return 3; SVC_ACT_FILE_STATE=not-found ;;
                *) return 3 ;;
            esac
            ;;
        Darwin)
            local domain="${target%/*}" name="${target##*/}"
            [ "${file##*/}" = "$name.plist" ] || return 3
            case "$name" in *[!a-zA-Z0-9_.-]*|'') return 3 ;; esac
            uid=$(launchctl manageruid) || return 3
            case "$uid" in ''|*[!0-9]*) return 3 ;; esac
            manager=$(launchctl managername) || return 3
            case "$domain:$manager" in "gui/$uid:Aqua"|"user/$uid:Background") ;; *) return 3 ;; esac
            # Unlike print/procinfo, list has a documented three-column format.
            # It queries the caller's domain, hence the explicit context proof.
            output=$(launchctl list) || return 3
            SVC_ACT_FILE_STATE=unchanged; SVC_ACT_LOAD=unloaded; SVC_ACT_ACTIVE=inactive
            while read -r pid status label extra; do
                if [ "$count" -eq 0 ]; then
                    [ "$pid $status $label $extra" = 'PID Status Label ' ] || return 3
                else
                    [ -z "$extra" ] && [ -n "$label" ] || return 3
                    case "$pid" in -) ;; ''|*[!0-9]*) return 3 ;; esac
                    case "${status#-}" in ''|*[!0-9]*) return 3 ;; esac
                    if [ "$pid" != - ]; then
                        [ "$pid" -gt 1 ] || return 3
                        SVC_ACT_JOB_PIDS="$SVC_ACT_JOB_PIDS $pid"
                    fi
                    if [ "$label" = "$name" ]; then
                        [ "$SVC_ACT_LOAD" = unloaded ] || return 3
                        SVC_ACT_LOAD=loaded
                        if [ "$pid" != - ]; then
                            [ "$pid" -gt 1 ] || return 3
                            SVC_ACT_PID="$pid"; SVC_ACT_ACTIVE=active
                        fi
                    fi
                fi
                count=$((count + 1))
            done <<EOF
$output
EOF
            ;;
        *) return 3 ;;
    esac
    case "$SVC_ACT_ACTIVE" in active|inactive) ;; *) return 3 ;; esac
}

# First adoption uses the existing session handoff and exact private tmux
# server. The coordinator supplies paths/socket from frozen, verified unit
# ownership; no fleet walk or process-table search occurs.
svc_activation_handoff() (
    local bot_dir="$1" expected="$2" tmpdir="$3" actual session sessions declared_tmpdir
    [ -d "$bot_dir" ] || return 3
    case "$tmpdir" in /*) ;; *) return 3 ;; esac
    case "$tmpdir" in *$'\n'*|*$'\t'*) return 3 ;; esac
    export TMUX_TMPDIR="$tmpdir"
    . "$_SUPERVISOR_LIB_DIR/lib-common.sh" || return 3
    declared_tmpdir=$(bot_conf_get_path "$bot_dir" TMUX_TMPDIR "") || return 3
    [ -z "$declared_tmpdir" ] || [ "$declared_tmpdir" = "$tmpdir" ] || return 3
    actual=$(tmux_socket_for_bot "$bot_dir") || return 3
    [ "$actual" = "$expected" ] || return 3
    session=$(tmux_session_name "$bot_dir") || return 3
    sessions=$(bot_tmux "$expected" list-sessions -F '#{session_name}' 2>/dev/null) || return 3
    [ "$sessions" = "$session" ] || return 3
    "$_SUPERVISOR_LIB_DIR/pre-stop-handoff.sh" "$bot_dir"
)

svc_activation_stop_private_server() (
    local bot_dir="$1" expected="$2" tmpdir="$3" sessions session
    case "$bot_dir" in /*) ;; *) return 3 ;; esac
    case "$expected" in ''|*[!a-zA-Z0-9_.-]*) return 3 ;; esac
    case "$tmpdir" in /*) ;; *) return 3 ;; esac
    case "$tmpdir" in *$'\n'*|*$'\t'*) return 3 ;; esac
    export TMUX_TMPDIR="$tmpdir"
    . "$_SUPERVISOR_LIB_DIR/lib-common.sh" || return 3
    session=$(tmux_session_name "$bot_dir") || return 3
    sessions=$(bot_tmux "$expected" list-sessions -F '#{session_name}' 2>/dev/null) || return 3
    [ "$sessions" = "$session" ] || return 3
    bot_tmux "$expected" kill-server || return 3
)

svc_activation_snapshot() {
    [ -f "$1" ] || { _svc_activation_unknown "missing installed file: $1"; return 3; }
    _svc_activation_read "$1" "$2" || { _svc_activation_unknown "$2 state"; return 3; }
    case "$SVC_ACT_FILE_STATE" in enabled|enabled-runtime|disabled|static|masked|masked-runtime|unchanged) ;; *) return 3 ;; esac
    printf '%s %s %s\n' "$SVC_ACT_FILE_STATE" "$SVC_ACT_LOAD" "$SVC_ACT_ACTIVE"
}

# rc 0 external, 1 hosted by this unit, 3 unable to establish either fact.
# Kernel ancestry/control-group data only: never BOT_SERVICE/env or argv text.
svc_activation_assert_external() {
    local file="$1" target="$2" caller="${3:-$$}" rc=0
    _svc_activation_read "$file" "$target" || { _svc_activation_unknown "$target state/domain"; return 3; }
    case "$_OS" in
        Linux)
            if [ -n "$SVC_ACT_GROUP" ]; then
                python3 "$_SUPERVISOR_LIB_DIR/supervisor-caller.py" cgroup "$caller" "$SVC_ACT_GROUP" || rc=$?
            elif [ "$SVC_ACT_ACTIVE" = active ] && [ "${target##*.}" = service ]; then rc=3
            fi
            ;;
        Darwin)
            # A detached/reparented child can lack the target's current PID in
            # its chain. Prove a different loaded job owns the chain, or block.
            python3 "$_SUPERVISOR_LIB_DIR/supervisor-caller.py" launchd "$caller" "$SVC_ACT_PID:$SVC_ACT_JOB_PIDS" || rc=$?
            ;;
        *) rc=3 ;;
    esac
    case "$rc" in
        0) return 0 ;;
        1) printf 'activation refused: %s hosts the caller ancestry\n' "$target" >&2; return 1 ;;
        *) _svc_activation_unknown "$target caller ancestry"; return 3 ;;
    esac
}

_svc_activation_saved() {
    local extra=""
    read -r SVC_ACT_OLD_FILE SVC_ACT_OLD_LOAD SVC_ACT_OLD_ACTIVE extra <<EOF
$1
EOF
    [ -z "$extra" ] || return 3
    case "$_OS:$SVC_ACT_OLD_FILE:$SVC_ACT_OLD_LOAD" in
        Linux:enabled:loaded|Linux:enabled-runtime:loaded|Linux:disabled:loaded|Linux:static:loaded|Linux:masked:masked|Linux:masked-runtime:masked|Darwin:unchanged:loaded|Darwin:unchanged:unloaded) ;;
        *) return 3 ;;
    esac
    case "$SVC_ACT_OLD_ACTIVE" in active|inactive) ;; *) return 3 ;; esac
    case "$SVC_ACT_OLD_LOAD:$SVC_ACT_OLD_ACTIVE" in masked:active|unloaded:active) return 3 ;; esac
}

svc_activation_pause() {
    local file="$1" target="$2" saved="$3"
    _svc_activation_saved "$saved" || return 3
    svc_activation_assert_external "$file" "$target" "${4:-$$}" || return $?
    case "$_OS" in
        Linux)
            # The enrollment owner parks higher-priority installed bytes first.
            # A successful command alone is not evidence that its mask won.
            case "$SVC_ACT_OLD_FILE" in masked|masked-runtime) ;;
                *) systemctl --user mask --runtime "$target" || return $? ;;
            esac
            _svc_activation_read "$file" "$target" || return 3
            [ "$SVC_ACT_LOAD" = masked ] || { _svc_activation_unknown "$target mask did not take precedence"; return 3; }
            systemctl --user stop "$target" || return $?
            ;;
        Darwin)
            [ "$SVC_ACT_LOAD" = unloaded ] || launchctl bootout "$target" || return $?
            ;;
    esac
    _svc_activation_read "$file" "$target" || return 3
    [ "$SVC_ACT_ACTIVE" = inactive ] || return 3
    case "$_OS:$SVC_ACT_LOAD" in Linux:masked|Darwin:unloaded) return 0 ;; *) return 3 ;; esac
}

svc_activation_resume() {
    local file="$1" target="$2" saved="$3"
    [ -f "$file" ] || { _svc_activation_unknown "restore installed file before resume: $file"; return 3; }
    _svc_activation_saved "$saved" || return 3
    _svc_activation_read "$file" "$target" || return 3
    case "$_OS" in
        Linux)
            case "$SVC_ACT_OLD_FILE" in masked|masked-runtime) ;;
                *) systemctl --user unmask --runtime "$target" || return $? ;;
            esac
            _svc_activation_read "$file" "$target" || return 3
            [ "$SVC_ACT_FILE_STATE $SVC_ACT_LOAD" = "$SVC_ACT_OLD_FILE $SVC_ACT_OLD_LOAD" ] || return 3
            if [ "$SVC_ACT_OLD_ACTIVE" = active ]; then systemctl --user start "$target" || return $?; fi
            ;;
        Darwin)
            if [ "$SVC_ACT_OLD_LOAD" = loaded ] && [ "$SVC_ACT_LOAD" = unloaded ]; then
                launchctl bootstrap "${target%/*}" "$file" || return $?
            fi
            ;;
    esac
    _svc_activation_read "$file" "$target" || return 3
    [ "$SVC_ACT_LOAD" = "$SVC_ACT_OLD_LOAD" ] || return 3
    # Restoring a launchd definition also restores its normal launch policy;
    # the activation owner sequences those starts and owns readiness checks.
    [ "$_OS" = Darwin ] || [ "$SVC_ACT_ACTIVE" = "$SVC_ACT_OLD_ACTIVE" ]
}

# Exact candidate start after the publication owner verified bytes and quiet
# ownership. The coordinator holds EX and arms the release-bound unit grant.
# No enable/install/discovery: timers are started by their exact .timer target.
svc_activation_start() {
    local file="$1" target="$2"
    [ -f "$file" ] || { _svc_activation_unknown "missing candidate: $file"; return 3; }
    case "$_OS" in
        Linux)
            case "$target" in *[!a-zA-Z0-9_.@-]*|'') return 3 ;; esac
            case "$target" in *.service|*.timer) ;; *) return 3 ;; esac
            [ "${file##*/}" = "$target" ] || return 3
            systemctl --user daemon-reload || return $?
            _svc_activation_read "$file" "$target" || return 3
            [ "$SVC_ACT_ACTIVE" = inactive ] || return 3
            if [ "$SVC_ACT_LOAD" = masked ]; then
                [ "$SVC_ACT_FILE_STATE" = masked-runtime ] || return 3
                systemctl --user unmask --runtime "$target" || return $?
                systemctl --user daemon-reload || return $?
            fi
            _svc_activation_read "$file" "$target" || return 3
            [ "$SVC_ACT_LOAD $SVC_ACT_ACTIVE" = 'loaded inactive' ] || return 3
            systemctl --user start "$target" || return $?
            ;;
        Darwin)
            _svc_activation_read "$file" "$target" || return 3
            [ "$SVC_ACT_ACTIVE" = inactive ] || return 3
            if [ "$SVC_ACT_LOAD" = unloaded ]; then
                # RunAtLoad is allowed only inside the already-armed scope.
                launchctl bootstrap "${target%/*}" "$file" || return $?
            else
                launchctl kickstart "$target" || return $?
            fi
            ;;
        *) return 3 ;;
    esac
    printf 'start-requested\n' # native acknowledgement, not application readiness
}

# Native inactive plus exact Linux v2 cgroup emptiness, when a group is known.
# Darwin needs the caller's pre-stop PID/socket witnesses as well. This does
# not infer that arbitrary detached processes on the host are absent.
svc_activation_quiet() {
    local file="$1" target="$2" group="${3:-}" tree paths path members
    _svc_activation_read "$file" "$target" || return 3
    [ "$SVC_ACT_ACTIVE" = inactive ] || { _svc_activation_unknown "$target remains active"; return 3; }
    if [ "$_OS" = Linux ]; then
        group="${group:-$SVC_ACT_GROUP}"
        if [ -n "$group" ]; then
            case "$group" in /*) ;; *) return 3 ;; esac
            case "$group" in /|*..*|*$'\n'*) return 3 ;; esac
            [ -r /sys/fs/cgroup/cgroup.controllers ] || { _svc_activation_unknown 'cgroup v2 unavailable'; return 3; }
            tree="/sys/fs/cgroup$group"
            if [ -e "$tree" ]; then
                [ -d "$tree" ] && [ -r "$tree/cgroup.procs" ] || return 3
                paths=$(find "$tree" -type f -name cgroup.procs -print) || return 3
                [ -n "$paths" ] || return 3
                while IFS= read -r path; do
                    members=$(cat "$path") || return 3
                    [ -z "$members" ] || { _svc_activation_unknown "$target has remaining cgroup members"; return 3; }
                done <<EOF
$paths
EOF
            fi
            printf 'inactive\tcgroup-empty\n'; return 0
        fi
    fi
    printf 'inactive\tno-cgroup-witness\n'
}

# Reuse the restart owner's readiness policy without invoking its fleet walk.
# Source only inside these cold activation calls, never the per-tool hot path.
svc_activation_bot_fence() (
    export CLAUDLOBBY_ROOT="$1"
    local bot_dir="$2" ceiling token
    [ -d "$bot_dir" ] && [ -r "$bot_dir/bot.conf" ] || return 3
    . "$_SUPERVISOR_LIB_DIR/rolling-restart.sh" || return 3
    ceiling=$(rr_bot_ceiling "$bot_dir") || return 3
    case "$ceiling" in ''|*[!0-9]*) return 3 ;; esac
    token=$(bridge_fence_write "$bot_dir") || return 3
    [ -n "$token" ] && grep -Fq -- "$token" "$bot_dir/logs/startup.log" || return 3
    printf '%s\t%s\n' "$ceiling" "$token"
)

svc_activation_bot_ready() (
    export CLAUDLOBBY_ROOT="$1"
    local bot_dir="$2" ceiling="$3" token="$4" socket session
    case "$ceiling" in ''|*[!0-9]*) return 3 ;; esac
    [ -n "$token" ] || return 3
    . "$_SUPERVISOR_LIB_DIR/rolling-restart.sh" || return 3
    wait_bridge_ready "$bot_dir" "$ceiling" "$token" || return $?
    socket=$(tmux_socket_for_bot "$bot_dir") || return 3
    [ -n "$socket" ] || return 3
    session=$(tmux_session_name "$bot_dir") || return 3
    check_tmux_session "$session" "$socket" || return 3
    printf 'bridge-ready\n'
)

# Read-only enrollment observations. Catalog rows are tab-separated; native
# property output remains native output, parsed strictly by the inventory owner.
# List both search paths and loaded names: either list alone misses consumers.
svc_inventory_catalog() {
    local output path name rest uid manager domain
    printf 'manager\t%s\n' "$_OS"
    case "$_OS" in
        Linux)
            output=$(systemd-analyze --user unit-paths) || return 3
            while IFS= read -r path; do
                case "$path" in /*) printf 'directory\t%s\n' "$path" ;; *) return 3 ;; esac
            done <<EOF
$output
EOF
            output=$(systemctl --user list-unit-files --no-legend --no-pager --plain) || return 3
            while read -r name rest; do
                [ -z "$name" ] || printf 'installed\t%s\n' "$name"
            done <<EOF
$output
EOF
            output=$(systemctl --user list-units --all --no-legend --no-pager --plain) || return 3
            while read -r name rest; do
                [ -z "$name" ] || printf 'loaded\t%s\n' "$name"
            done <<EOF
$output
EOF
            ;;
        Darwin)
            uid=$(launchctl manageruid) || return 3
            case "$uid" in ''|*[!0-9]*) return 3 ;; esac
            manager=$(launchctl managername) || return 3
            case "$manager" in Aqua) domain="gui/$uid" ;; Background) domain="user/$uid" ;; *) return 3 ;; esac
            printf 'domain\t%s\n' "$domain"
            printf 'directory\t%s\n' "$HOME/Library/LaunchAgents" /Library/LaunchAgents /System/Library/LaunchAgents
            launchctl list || return 3
            ;;
        *) return 3 ;;
    esac
}

svc_inventory_properties() {
    case "$_OS" in
        Linux)
            case "$1" in ''|*[!a-zA-Z0-9_.@:-]*) return 3 ;; esac
            systemctl --user show --property=Id,LoadState,ActiveState,UnitFileState,FragmentPath,WorkingDirectory,Environment,ExecStart,DropInPaths,NeedDaemonReload,Triggers,TriggeredBy "$1"
            ;;
        Darwin)
            _svc_inventory_domain "${1%/*}" || return 3
            case "${1##*/}" in ''|*[!a-zA-Z0-9_.@:-]*) return 3 ;; esac
            launchctl print "$1"
            ;;
        *) return 3 ;;
    esac
}

# Exact caller-domain proof is required for both observations. print output is
# not an Apple API: the inventory owner validates the observed macOS grammar and
# refuses unknown shapes. These queries neither load nor alter a launchd job.
_svc_inventory_domain() {
    [ "$_OS" = Darwin ] || return 3
    local uid manager
    uid=$(launchctl manageruid) || return 3
    case "$uid" in ''|*[!0-9]*) return 3 ;; esac
    manager=$(launchctl managername) || return 3
    case "$1:$manager" in "gui/$uid:Aqua"|"user/$uid:Background") return 0 ;; *) return 3 ;; esac
}

svc_inventory_disabled() {
    _svc_inventory_domain "$1" || return 3
    launchctl print-disabled "$1"
}

# A parked file is legitimate during activation recovery. Unlike snapshot,
# this query does not require source bytes to remain at the installed path.
svc_inventory_state() {
    _svc_activation_read "$1" "$2" || return 3
    printf '%s %s %s\n' "$SVC_ACT_FILE_STATE" "$SVC_ACT_LOAD" "$SVC_ACT_ACTIVE"
}
