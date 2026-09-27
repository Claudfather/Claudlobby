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
