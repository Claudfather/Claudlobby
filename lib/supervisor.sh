#!/bin/bash
# lib/supervisor.sh — the supervisor adapter: five verbs, each with a systemd
# spelling and a launchd spelling, behind the one $_OS switch every lib/
# script already re-derives for itself (#1573 boot admission, task 6).
#
# Sourced by lib-common.sh immediately after detect_os runs, so every verb
# below can read $_OS without re-deriving it. Bodies are MOVED from the code
# that already carries them — keepalive.sh's restart ladder (svc_kick),
# install-bot-systemd.sh / install-bot.sh (svc_enroll calls them; PR B moves
# their bodies in and turns them into thin wrappers), and spin-down-bot.sh's
# supervision leg (svc_disenroll) — not reinvented here.
#
# In THIS PR no existing call site migrates onto these verbs (PR B does the
# boot path). This file exists, is sourced, and is contract-tested
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
#   svc_kick <bot_dir>            — restart/kickstart; rc 2 if no branch
#                                    applies (caller falls back to
#                                    start-bot.sh itself — this file never
#                                    does, so it stays a pure mechanism).
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
# calls (the actual restart / enroll) are left unguarded, exactly as
# keepalive.sh's restart ladder leaves them, so a genuine failure still
# propagates under the caller's set -euo pipefail rather than being silently
# swallowed.
#
# svc_enroll's sibling-script lookup is deliberately NOT $CLAUDLOBBY_ROOT/lib
# (see the comment beside this file's own source line in lib-common.sh for
# why): it is this file's own directory, self-derived from ${BASH_SOURCE[0]}
# exactly once, the same way lib-common.sh derives CLAUDLOBBY_ROOT from its
# own location rather than trusting an inherited variable. The `:=` form
# leaves a deliberate seam: tests/test_supervisor_adapter.sh's hermetic
# svc_enroll cases pre-export this to a scratch dir of stand-in scripts
# rather than ever forking the real install-bot.sh, whose Darwin leg shells
# out to the absolute, unfakeable /bin/launchctl bootstrap -- a real call a
# hermetic test must never risk making.
: "${_SUPERVISOR_LIB_DIR:=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# svc_unit_name <bot_dir>
# The shared label resolver every other verb below builds on. BOT_SERVICE is
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
# `state = running` line is present.
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
                if printf '%s' "$out" | grep -q 'state = running'; then
                    printf 'loaded-active'
                else
                    printf 'loaded-inactive'
                fi
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

# svc_kick <bot_dir>
# The restart ladder moved verbatim from keepalive.sh's restart_bot_service:
# BOT_SERVICE-named systemd unit, else the pre-rename BOT_NAME.service, else
# launchd kickstart. Prints one line describing what it did (for the
# caller's own log — this file owns no log of its own) and returns whatever
# the restart/kickstart command itself returns. rc 2 when no branch applies
# — nothing was invoked — so the caller falls back to start-bot.sh exactly as
# restart_bot_service's own final branch does; that fallback is NOT moved
# here; it stays the caller's decision (PR B migrates the caller).
svc_kick() {
    local bot_dir="${1:?Usage: svc_kick <bot_dir>}"
    local bot_service bot_name
    bot_service="$(bot_conf_get "$bot_dir" BOT_SERVICE "")"
    bot_name="$(bot_conf_get "$bot_dir" BOT_NAME "")"
    if [ "$_OS" = "Linux" ] && [ -n "$bot_service" ] && [ -f "$HOME/.config/systemd/user/$bot_service.service" ]; then
        printf 'systemctl --user restart %s\n' "$bot_service"
        systemctl --user restart "$bot_service.service"
        return 0
    elif [ "$_OS" = "Linux" ] && [ -n "$bot_name" ] && [ -f "$HOME/.config/systemd/user/$bot_name.service" ]; then
        printf 'systemctl --user restart %s (pre-rename)\n' "$bot_name"
        systemctl --user restart "$bot_name.service"
        return 0
    elif [ "$_OS" = "Darwin" ] && [ -n "$bot_service" ] && [ -f "$HOME/Library/LaunchAgents/$bot_service.plist" ]; then
        printf 'launchctl kickstart %s\n' "$bot_service"
        launchctl kickstart -k "gui/$(id -u)/$bot_service"
        return 0
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

# svc_disenroll <bot_dir>
# Moved from spin-down-bot.sh's reap_supervision + reap_tmux legs (Linux:
# disable --now, daemon-reload, reset-failed, remove the installed unit;
# Darwin: bootout + remove the plist — bare `launchctl`, not spin-down-bot.sh's
# absolute /bin/launchctl: the same target, the same action, resolved through
# PATH like every other verb here so the whole adapter's external calls are
# uniformly fakeable, matching keepalive.sh's own kickstart spelling). BOTH
# branches then run the teardown a unit/plist cannot hook — killing the
# bot's private tmux server and dropping .tmux-env — unconditionally, because
# that leg is OS-independent in the source it was moved from (on Linux the
# unit's own ExecStop already did it; repeating it is idempotent). An
# unrecognized OS skips the supervision leg (nothing to invoke) but still
# runs the OS-independent tmux teardown, exactly as spin-down-bot.sh's own
# `case "$_OS" in ... *) skip ;; esac` falls through to its next leg rather
# than aborting.
svc_disenroll() {
    local bot_dir="${1:?Usage: svc_disenroll <bot_dir>}"
    local label
    label="$(bot_conf_get "$bot_dir" BOT_SERVICE "")"
    case "$_OS" in
        Linux)
            if [ -n "$label" ]; then
                local ud="$HOME/.config/systemd/user"
                systemctl --user disable --now "$label.service" 2>/dev/null || true
                rm -f "$ud/$label.service" "$ud/default.target.wants/$label.service"
                systemctl --user daemon-reload 2>/dev/null || true
                systemctl --user reset-failed "$label.service" 2>/dev/null || true
                printf 'systemd user unit %s.service stopped + disabled + removed\n' "$label"
            else
                printf 'svc_disenroll: BOT_SERVICE unset for %s -- no supervised unit to remove\n' "$bot_dir"
            fi
            ;;
        Darwin)
            if [ -n "$label" ]; then
                launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
                rm -f "$HOME/Library/LaunchAgents/$label.plist"
                printf 'launchd agent %s booted out + plist removed\n' "$label"
            else
                printf 'svc_disenroll: BOT_SERVICE unset for %s -- no supervised agent to remove\n' "$bot_dir"
            fi
            ;;
        *)
            printf 'svc_disenroll: unsupported OS (%s) -- skipping supervision leg\n' "$_OS" >&2
            ;;
    esac
    local sock
    sock="$(tmux_socket_for_bot "$bot_dir" 2>/dev/null)" || sock=""
    if [ -n "$sock" ]; then
        bot_tmux "$sock" kill-server 2>/dev/null || true
    fi
    rm -f "$bot_dir/.tmux-env" 2>/dev/null || true
    return 0
}
