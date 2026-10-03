#!/bin/bash
# claudlobby/_runtime_scripts/supervisor.sh — native supervisor adapter for legacy lifecycle callers
# and selected-release inventory, activation, bot, and host-job operations.
# Exact selected operations receive frozen installed paths and targets from
# the Python owner; they never derive identity from mutable bot.conf.
#
# Sourced by lib-common.sh immediately after detect_os runs, so every verb
# below uses the same OS selection. Exact readers refuse ambiguous ownership;
# mutation callers additionally enforce selected-release admission.
#
# keepalive.sh and spin-up/down-bot.sh use the action verbs below. Readers
# and installer bodies retain their separate contracts under #1607. The
# adapter is sourced and contract-tested
# (tests/test_supervisor_adapter.sh) against fake systemctl/launchctl
# binaries. tests/test_supervisor_ratchet.py fences every OTHER claudlobby/_runtime_scripts/ file's
# direct systemctl/launchctl calls at their current count — this file is the
# one place that count is allowed to grow, which is why it is excluded from
# that scan.
#
#   svc_unit_name <bot_dir>       — BOT_SERVICE, or the pre-rename BOT_NAME
#                                    fallback while a unit/plist by that name
#                                    exists; prints the bare label.
#   svc_is_registered <bot_dir> [loaded_label]
#                                  — rc 0/1: is a unit/plist installed. An
#                                    explicit label uses the caller's sourced
#                                    bot.conf snapshot, including empty.
#   svc_state <bot_dir>           — prints loaded-active | loaded-inactive |
#                                    not-loaded | unknown.
#   svc_kick <bot_dir> [...]      — restart/kickstart; SVC_KICK_SELECTED=0
#                                    and rc 2 if no branch applies. A selected
#                                    action preserves native status (also 2).
#                                    The caller owns a no-target fallback.
#   svc_enroll <bot_dir>          — installs + starts the composed unit.
#   svc_disenroll <bot_dir>       — removes supervision + the tmux server the
#                                    unit/plist cannot hook.
#   svc_job_hosts_caller <label>  — rc 0/1: is the loaded launchd job <label>
#                                    running the caller (fails closed).
#   svc_enroll_agent <label> <src_plist>
#                                  — installs + (re)loads a composed launchd
#                                    plist, never stopping the job that is
#                                    running the caller (#1924).
#
# Every verb resolves the OS through $_OS (set by detect_os). An OS neither
# Linux nor Darwin invokes no external binary and reports the fact in the
# shape each verb already defines (svc_state prints "unknown"; svc_is_registered
# returns 1; svc_kick / svc_enroll return 2) rather than guessing.
#
# bash 3.2 safe (tests/test_bash_parse.py covers claudlobby/_runtime_scripts/): no apostrophes inside
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
# unreadable ownership preserves the installed unit. Rc 2 is a parsed service
# without WorkingDirectory, and is useful only for foreign inventory. The reader is stdlib-only
# and reads WorkingDirectory; no unit content is sourced or executed.
svc_bot_unit_owned_by() {
    local unit="${1:?unit file required}" bot_dir="${2:?bot directory required}" rc=0 output=""
    local python="${CLAUDLOBBY_NATIVE_PYTHON-python3}"
    if command -v "$python" >/dev/null 2>&1; then
        # Settle the reader's status inside the substitution: Bash 3.2 fires
        # an inherited ERR trap there even when the outer assignment has ||.
        output="$(if "$python" "$_SUPERVISOR_LIB_DIR/bot-unit-owner.py" "$unit" "$bot_dir" 2>&1; then
                     printf '\n0'
                 else
                     printf '\n%s' "$?"
                 fi)"
        rc="${output##*$'\n'}"
        output="${output%$'\n'*}"
        # The reader has no output protocol. A traceback from a failed reader
        # must not be mistaken for its rc 1 (a known foreign owner).
        if [ -z "$output" ]; then
            case "$rc" in
                0|1|2) return "$rc" ;;
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
    if [ "$#" -ge 2 ]; then label="$2"; else label="$(svc_unit_name "$bot_dir")"; fi
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
# claudlobby-plane-view). svc_kick resolves a BOT's label from bot.conf; a host service has
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

# Request one already-loaded, inactive host timer's service. The selected
# publication and inventory owners check its bytes, scope and placement before
# this call; this final native read refuses a vanished or active unit. Never
# bootstrap, unmask or restart a resident process to satisfy a manual run.
svc_host_job_run_exact() {
    local file="$1" target="$2"
    [ -f "$file" ] || return 3
    _svc_activation_read "$file" "$target" || return 3
    [ "$SVC_ACT_LOAD" = loaded ] || return 3
    case "$_OS" in
        Linux)
            case "$target" in *.service) ;; *) return 3 ;; esac
            case "$SVC_ACT_ACTIVE" in
                inactive) ;;
                failed) systemctl --user reset-failed "$target" || return $? ;;
                *) _svc_activation_unknown "$target ActiveState=$SVC_ACT_ACTIVE SubState=$SVC_ACT_SUB"; return 3 ;;
            esac
            printf 'invoking\n'
            systemctl --user start "$target" || return $?
            ;;
        Darwin)
            [ "$SVC_ACT_ACTIVE" = inactive ] || return 3
            printf 'invoking\n'
            launchctl kickstart "$target" || return $?
            ;;
        *) return 3 ;;
    esac
    printf 'run-requested\n'
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

# svc_job_hosts_caller <label>
# rc 0 when the loaded launchd job <label> is running THIS process: launchd
# reports a pid for it, and that pid is this shell or one of its ancestors.
# Stopping such a job stops the caller mid-run -- a bootout ends the job and
# launchd takes its process group with it, so nothing after the bootout runs.
# That is #1924: the nightly reload-fleet re-enrolled every fleet job, itself
# included, and its own bootout killed the run before the bootstrap meant to
# follow it, leaving the job unloaded and the nightly reload dead, unalerted.
#
# FAILS CLOSED: rc 0 too when the job is running and the answer cannot be
# established -- no pid line, a pid that is not a number, or an ancestry walk
# that broke off before reaching init. The two wrong answers cost different
# things: a job wrongly held is one deferred re-enroll, said out loud by the
# caller; a job wrongly stopped is the silent outage this exists to prevent.
# Every pid line in the output is compared, not only the first, so a nested
# section that ever carried one can only make the answer more conservative.
# rc 1 when it definitely is not: not loaded, loaded but not running, or the
# walk reached init without meeting any of its pids.
#
# The pid is launchd's own answer, from the same `print` output svc_state
# reads, and the walk goes UP from the caller, the direction
# claude-session-pid.sh walks: it asks which job is running me, and never
# searches the process table for one.
#
# launchd only; rc 1 on any other OS, where a re-enroll (daemon-reload +
# enable --now of the timer) never stops the service the timer belongs to --
# which is why the same nightly reload runs to completion on Linux.
svc_job_hosts_caller() {
    local label="${1:?Usage: svc_job_hosts_caller <label>}"
    [ "$_OS" = "Darwin" ] || return 1
    local out line pids="" running=0 p pp hops=0 chain=" "
    # A print that fails means not loaded. The fallback keeps the failing
    # command inside an || list, so an ERR trap armed with errtrace cannot
    # fire in this substitution whatever context the caller is in.
    out=$(launchctl print "gui/$(id -u)/$label" 2>/dev/null || printf '%s' '__svc_not_loaded__')
    case "$out" in *__svc_not_loaded__) return 1 ;; esac
    while IFS= read -r line; do
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in
            'state = running'*) running=1 ;;
            'pid = '*) pids="$pids ${line#pid = }" ;;
        esac
    done <<EOF
$out
EOF
    if [ -z "$pids" ]; then
        [ "$running" = 1 ] && return 0   # running, with no pid to compare
        return 1                          # loaded, not running
    fi
    p="$$"
    while :; do
        chain="$chain$p "
        case "$p" in 0|1) break ;; esac
        hops=$((hops + 1))
        [ "$hops" -le 64 ] || return 0
        pp=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d '[:space:]' || true)
        case "$pp" in ''|*[!0-9]*) return 0 ;; esac
        [ "$pp" != "$p" ] || return 0
        p="$pp"
    done
    for p in $pids; do
        case "$p" in ''|*[!0-9]*) return 0 ;; esac
        case "$chain" in *" $p "*) return 0 ;; esac
    done
    return 1
}

# svc_enroll_agent <label> <src_plist>
# Install a composed launchd plist into ~/Library/LaunchAgents and load it:
# copy, bootout, bootstrap -- the sequence the legacy timer installer ran
# inline with an absolute /bin/launchctl, moved here so the refusal below
# guards every caller, and resolved through PATH like every verb in this file
# so the whole sequence is fakeable (tests/test_supervisor_adapter.sh).
#
# It never stops a job that is running the caller (svc_job_hosts_caller). That
# job keeps the definition it was loaded from, and:
#   rc 0  its installed plist already matches <src_plist>, so there is nothing
#         to apply -- the nightly case, reload-fleet re-enrolling its own
#         unchanged job.
#   rc 4  it does not, and NOTHING is touched, the installed copy included. A
#         copy without the reload would leave a file that no longer describes
#         the loaded job; every later enrollment would compare equal against
#         it and never reload -- one deferral silently made permanent. Left
#         alone, the difference waits for the next enrollment run from outside
#         the job, which applies it the ordinary way.
# Otherwise: rc 0 installed + loaded; rc 1 the copy or the bootstrap failed
# (launchctl's own status is printed rather than returned, because its values
# could collide with 4); rc 2 not launchd, nothing invoked. Prints one line
# saying what it did, for the caller's own log.
svc_enroll_agent() {
    local label="${1:?Usage: svc_enroll_agent <label> <src_plist>}"
    local src="${2:?Usage: svc_enroll_agent <label> <src_plist>}"
    if [ "$_OS" != "Darwin" ]; then
        printf 'svc_enroll_agent: launchd only (saw %s) -- nothing invoked\n' "$_OS" >&2
        return 2
    fi
    local dest="$HOME/Library/LaunchAgents/$label.plist" uid rc=0
    if svc_job_hosts_caller "$label"; then
        if cmp -s "$src" "$dest"; then
            printf 'current: %s is running this enrollment and its installed plist already matches -- left loaded\n' "$label"
            return 0
        fi
        printf 'DEFERRED: %s is running this enrollment, so re-enrolling it now would stop it mid-run (#1924).\n' "$label" >&2
        printf '  Its composed plist differs from the installed one, and nothing was changed.\n' >&2
        printf '  The next enrollment run from outside the job applies it.\n' >&2
        return 4
    fi
    uid="$(id -u)"
    mkdir -p "${dest%/*}" 2>/dev/null || true
    if ! cp "$src" "$dest"; then
        printf 'svc_enroll_agent: could not copy %s to %s\n' "$src" "$dest" >&2
        return 1
    fi
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$uid" "$dest" || rc=$?
    if [ "$rc" -ne 0 ]; then
        printf 'svc_enroll_agent: launchctl bootstrap of %s failed (rc %s)\n' "$label" "$rc" >&2
        return 1
    fi
    printf 'installed + loaded: %s\n' "$label"
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

# Selected-release bot lifecycle. The Python owner supplies the exact source,
# saved installed placement and target from the active activation journal after
# checking the complete native inventory. These verbs never discover a label
# from mutable bot.conf and never remove a differently owned definition.
svc_bot_enroll_exact() {
    local source="$1" installed="$2" target="$3" state
    case "$source" in /*) ;; *) return 3 ;; esac
    case "$installed" in /*) ;; *) return 3 ;; esac
    [ "${source##*/}" = "${installed##*/}" ] || return 3
    [ -d "${installed%/*}" ] && [ ! -L "${installed%/*}" ] || return 3
    [ -f "$source" ] && [ ! -L "$source" ] || return 3
    case "$_OS" in
        Linux) [ "$target" = "${source##*/}" ] && [ "${target##*.}" = service ] || return 3 ;;
        Darwin) [ "${target##*/}.plist" = "${source##*/}" ] || return 3 ;;
        *) return 3 ;;
    esac
    if [ -e "$installed" ] || [ -L "$installed" ]; then
        [ -f "$installed" ] && [ ! -L "$installed" ] && cmp -s "$source" "$installed" || return 3
    else
        cp -p "$source" "$installed" || return $?
    fi
    if [ "$_OS" = Linux ]; then
            systemctl --user daemon-reload || return $?
    fi
    state=$(svc_inventory_state "$installed" "$target") || return 3
    case "$_OS:$state" in
        Linux:*' loaded active')
            systemctl --user enable "$target" || return $?
            systemctl --user restart "$target" || return $?
            ;;
        Linux:*' loaded inactive')
            systemctl --user enable --now "$target" || return $?
            ;;
        Linux:*' loaded failed')
            systemctl --user reset-failed "$target" || return $?
            systemctl --user enable --now "$target" || return $?
            ;;
        Darwin:'unchanged unloaded inactive') launchctl bootstrap "${target%/*}" "$installed" || return $? ;;
        Darwin:'unchanged loaded inactive') launchctl kickstart -k "$target" || return $? ;;
        Darwin:'unchanged loaded active') launchctl kickstart -k "$target" || return $? ;;
        *) return 3 ;;
    esac
}

svc_bot_disenroll_exact() {
    local source="$1" installed="$2" target="$3" bot_dir="$4" socket="$5" tmpdir="$6" state link
    case "$source" in /*) ;; *) return 3 ;; esac
    case "$installed" in /*) ;; *) return 3 ;; esac
    case "$bot_dir" in /*) ;; *) return 3 ;; esac
    case "$tmpdir" in /*) ;; *) return 3 ;; esac
    [ "${source##*/}" = "${installed##*/}" ] || return 3
    [ -d "${installed%/*}" ] && [ ! -L "${installed%/*}" ] || return 3
    [ -f "$source" ] && [ ! -L "$source" ] || return 3
    [ -f "$installed" ] && [ ! -L "$installed" ] && cmp -s "$source" "$installed" || return 3
    case "$socket" in ''|*[!a-zA-Z0-9_.-]*) return 3 ;; esac
    case "$_OS" in
        Linux) [ "$target" = "${source##*/}" ] && [ "${target##*.}" = service ] || return 3 ;;
        Darwin) [ "${target##*/}.plist" = "${source##*/}" ] || return 3 ;;
        *) return 3 ;;
    esac
    state=$(svc_inventory_state "$installed" "$target") || return 3
    # A launchd job can finish its start-bot wrapper while its private tmux
    # server remains live. With no job PID, caller ancestry cannot identify a
    # detached manager as external. Bootout of this exact inactive job has no
    # running launchd process to stop; public bot-stop admission separately
    # refuses the caller's own bot before reaching this adapter. Keep ancestry
    # proof for every active target and for Linux.
    case "$_OS:$state" in
        Darwin:'unchanged loaded inactive'|Darwin:'unchanged unloaded inactive') ;;
        *) svc_activation_assert_external "$installed" "$target" || return 3 ;;
    esac
    case "$_OS:$state" in
        Linux:*' loaded '*)
            link="${installed%/*}/default.target.wants/${installed##*/}"
            if [ -e "$link" ] || [ -L "$link" ]; then
                [ -L "$link" ] || return 3
                case "$(readlink "$link")" in "$installed"|"../${installed##*/}") ;; *) return 3 ;; esac
            fi
            printf 'effect-attempted\n'
            systemctl --user disable --now "$target" || return $?
            if [ "${state##* }" = failed ]; then
                systemctl --user reset-failed "$target" || return $?
            fi
            rm -f "$installed" "$link" || return $?
            systemctl --user daemon-reload || return $?
            ;;
        Darwin:'unchanged loaded '*)
            printf 'effect-attempted\n'
            launchctl bootout "$target" || return $?
            rm -f "$installed" || return $?
            ;;
        Darwin:'unchanged unloaded inactive')
            printf 'effect-attempted\n'
            rm -f "$installed" || return $? ;;
        *) return 3 ;;
    esac
    # The exact unit is now disabled or booted out, so tmux's exact no-server
    # text for this socket is a stale file from a clean exit. A live matching
    # server is still stopped; any other answer refuses. The caller's
    # quiescence check then probes the socket independently.
    if [ -S "$tmpdir/tmux-$(id -u)/$socket" ]; then
        svc_activation_stop_private_server "$bot_dir" "$socket" "$tmpdir" retired || return 3
    fi
    rm -f "$bot_dir/.tmux-env" || return $?
}

# Read-only current-session verdict for an already active exact bot unit.
# Reuse start-bot's session-scoped bridge readiness predicate; a stale startup
# marker alone never proves this session is ready. A caller with the frozen
# installed path and target can also prove the exact native job inactive when
# its private socket is missing; native read failure remains unknown.
svc_bot_session_observe() (
    local bot_dir="$1" expected="$2" tmpdir="$3" installed="${4:-}" target="${5:-}"
    local actual session socket pane token state declared_tmpdir
    if [ -n "$installed" ] || [ -n "$target" ]; then
        [ -n "$installed" ] && [ -n "$target" ] || return 3
    fi
    case "$bot_dir:$tmpdir" in /*:/*) ;; *) return 3 ;; esac
    case "$expected" in ''|*[!a-zA-Z0-9_.-]*) return 3 ;; esac
    export TMUX_TMPDIR="$tmpdir"
    . "$_SUPERVISOR_LIB_DIR/lib-common.sh" || return 3
    declared_tmpdir=$(bot_conf_get_path "$bot_dir" TMUX_TMPDIR "") || return 3
    [ -z "$declared_tmpdir" ] || [ "$declared_tmpdir" = "$tmpdir" ] || return 3
    actual=$(tmux_socket_for_bot "$bot_dir") || return 3
    [ "$actual" = "$expected" ] || return 3
    session=$(tmux_session_name "$bot_dir") || return 3
    socket="$tmpdir/tmux-$(id -u)/$expected"
    if [ ! -e "$socket" ] && [ ! -L "$socket" ]; then
        if [ -n "$installed" ] && [ -n "$target" ]; then
            _svc_activation_read "$installed" "$target" || { printf 'unknown\n'; return 0; }
            if [ "$SVC_ACT_ACTIVE" = inactive ]; then
                printf 'absent\n'; return 0
            fi
            printf 'unknown\n'; return 0
        fi
        # systemd's active/exited is the existing steady-state signal. During
        # active/running start-bot may simply not have created its socket yet;
        # launchd has no equivalent phase proof here, so neither is restarted.
        if [ "$_OS" = Linux ]; then
            _unit_start_facts "$expected.service"
            if [ "$_USF_ACTIVE/$_USF_SUB" = active/exited ]; then
                printf 'absent\n'; return 0
            fi
        fi
        printf 'unknown\n'; return 0
    fi
    [ -S "$socket" ] && [ -O "$socket" ] || { printf 'unknown\n'; return 0; }
    check_tmux_session "$session" "$expected" || { printf 'unknown\n'; return 0; }
    pane=$(bot_tmux "$expected" list-panes -t "$session" -F '#{pane_pid}' 2>/dev/null) || {
        printf 'unknown\n'; return 0;
    }
    case "$pane" in ''|*[!0-9]*) printf 'unknown\n'; return 0 ;; esac
    token=$(resolve_bot_telegram_token "$bot_dir" 2>/dev/null || true)
    state=$(wait_bridge_ready_state "$bot_dir" 0 "$pane" "$token" "$session" "$expected") || {
        printf 'unknown\n'; return 0;
    }
    case "$state" in up|no_handle|no_token) printf 'ready\n' ;; *) printf 'unknown\n' ;; esac
)

# Submit exactly one explicit control to the selected bot's private session.
# The public owner has already proved the frozen bot.conf and native unit. Check
# that binding again here before touching tmux; never discover a pane by name.
svc_bot_control_exact() (
    local bot_dir="$1" expected="$2" tmpdir="$3" control="$4"
    local session
    case "$control" in interrupt|compact) ;; *) return 3 ;; esac
    [ "$(svc_bot_session_observe "$bot_dir" "$expected" "$tmpdir")" = ready ] || return 3
    export TMUX_TMPDIR="$tmpdir"
    . "$_SUPERVISOR_LIB_DIR/lib-common.sh" || return 3
    session=$(tmux_session_name "$bot_dir") || return 3
    if [ "$control" = interrupt ]; then
        # Esc requests one turn/tool cancellation without Ctrl-C's idle-prompt
        # exit behavior. Tmux submission does not verify Claude cancelled it.
        # It takes the pane's send lock (#2036): an Escape landing inside
        # another sender's chunks would act on that half-typed payload.
        pane_send_key "$expected" "$session" Escape interrupt || return 3
    else
        # Keep the existing chunked pane primitive, but disable its optional
        # Enter repair: this explicit control is never automatically resent.
        PANE_SEND_VERIFY_TICKS=0 pane_send_verified "$expected" "$session" /compact || return 3
    fi
    printf 'control-submitted\n'
)

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

# `mask --runtime` reports its own link as FragmentPath, not /dev/null. Accept
# only this user's exact runtime link for TARGET, and only to /dev/null.
_svc_activation_runtime_mask() {
    local link="$1" target="$2" runtime="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    case "$runtime" in /*) ;; *) return 1 ;; esac
    [ "$link" = "$runtime/systemd/user/$target" ] && [ -L "$link" ] && [ -O "${link%/*}" ] \
        && [ "$(readlink "$link")" = /dev/null ]
}

# Reload this user's manager once so observations see files the caller just
# restored (early adoption abort) or published (Linux phase publication).
# Never starts, stops or retries.
svc_activation_reload() {
    [ "$_OS" = Linux ] || { _svc_activation_unknown "reload is Linux-only"; return 3; }
    systemctl --user daemon-reload
}

# A higher-priority installed file hides a surviving runtime mask from load
# state. Remove only this user's exact mask link for TARGET; never start, stop
# or retry. Callers: early adoption abort, whose SAVED is the frozen originally
# unmasked state; and Linux phase publication, whose SAVED is the candidate's
# fresh snapshot, so its ownership proof is Python's frozen-original check.
svc_activation_clear_runtime_mask() {
    local file="$1" target="$2" saved="$3" link
    [ "$_OS" = Linux ] || { _svc_activation_unknown "runtime masks are Linux-only"; return 3; }
    _svc_activation_saved "$saved" || return 3
    [ "$SVC_ACT_OLD_LOAD" = loaded ] || { _svc_activation_unknown "$target was not originally unmasked"; return 3; }
    _svc_activation_read "$file" "$target" || return 3
    [ "$SVC_ACT_LOAD" = loaded ] || { _svc_activation_unknown "$target restored file does not load"; return 3; }
    link="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/systemd/user/$target"
    if [ -e "$link" ] || [ -L "$link" ]; then
        _svc_activation_runtime_mask "$link" "$target" || { _svc_activation_unknown "$target runtime node is not a mask"; return 3; }
        systemctl --user unmask --runtime "$target" || return $?
        [ ! -e "$link" ] && [ ! -L "$link" ] || { _svc_activation_unknown "$target runtime mask remains"; return 3; }
        printf 'removed\n'
    else
        printf 'absent\n'
    fi
    _svc_activation_read "$file" "$target" || return 3
    [ "$SVC_ACT_FILE_STATE $SVC_ACT_LOAD" = "$SVC_ACT_OLD_FILE $SVC_ACT_OLD_LOAD" ]
}

_svc_activation_read() {
    local file="$1" target="$2" output key value seen=" " uid manager pid status label extra count=0
    case "$file" in /*) ;; *) _svc_activation_unknown "installed path is not absolute"; return 3 ;; esac
    SVC_ACT_FILE_STATE=""; SVC_ACT_LOAD=""; SVC_ACT_ACTIVE=""; SVC_ACT_SUB=""; SVC_ACT_GROUP=""; SVC_ACT_PID="-"; SVC_ACT_JOB_PIDS=""
    SVC_ACT_MAIN_PID=""; SVC_ACT_CONTROL_PID=""
    case "$_OS" in
        Linux)
            case "$target" in *[!a-zA-Z0-9_.@-]*|'') return 3 ;; esac
            case "$target" in *.service|*.timer|*.socket|*.path) ;; *) return 3 ;; esac
            [ "${file##*/}" = "$target" ] || return 3
            output=$(systemctl --user show --property=Id,LoadState,ActiveState,SubState,UnitFileState,FragmentPath,ControlGroup,MainPID,ControlPID "$target") || return 3
            local identity="" fragment=""
            while IFS='=' read -r key value; do
                case "$seen" in *" $key "*) return 3 ;; esac
                seen="$seen$key "
                case "$key" in
                    Id) identity="$value" ;;
                    LoadState) SVC_ACT_LOAD="$value" ;;
                    ActiveState) SVC_ACT_ACTIVE="$value" ;;
                    SubState) SVC_ACT_SUB="$value" ;;
                    UnitFileState) SVC_ACT_FILE_STATE="$value" ;;
                    FragmentPath) fragment="$value" ;;
                    ControlGroup) SVC_ACT_GROUP="$value" ;;
                    MainPID) SVC_ACT_MAIN_PID="$value" ;;
                    ControlPID) SVC_ACT_CONTROL_PID="$value" ;;
                    *) return 3 ;;
                esac
            done <<EOF
$output
EOF
            [ "$identity" = "$target" ] || return 3
            for key in Id LoadState ActiveState SubState UnitFileState FragmentPath; do
                case "$seen" in *" $key "*) ;; *) return 3 ;; esac
            done
            if [ "${target##*.}" = service ]; then
                for key in ControlGroup MainPID ControlPID; do
                    case "$seen" in *" $key "*) ;; *) return 3 ;; esac
                done
                case "$SVC_ACT_MAIN_PID:$SVC_ACT_CONTROL_PID" in
                    *[!0-9:]*|:*|*:) return 3 ;;
                esac
            fi
            case "$SVC_ACT_LOAD" in
                loaded) [ "$fragment" = "$file" ] || return 3 ;;
                masked) [ "$fragment" = /dev/null ] || _svc_activation_runtime_mask "$fragment" "$target" || return 3 ;;
                not-found) [ ! -e "$file" ] || return 3; SVC_ACT_FILE_STATE=not-found ;;
                *) return 3 ;;
            esac
            case "$SVC_ACT_SUB" in ''|*[!a-zA-Z0-9_-]*) return 3 ;; esac
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
    case "$_OS:$SVC_ACT_ACTIVE" in
        Linux:active|Linux:inactive|Linux:failed|Linux:activating|Linux:deactivating|\
        Darwin:active|Darwin:inactive) ;;
        *) _svc_activation_unknown "$target ActiveState=$SVC_ACT_ACTIVE SubState=$SVC_ACT_SUB"; return 3 ;;
    esac
}

# First adoption uses the existing session handoff and exact private tmux
# server. The coordinator supplies paths/socket from frozen, verified unit
# ownership; no fleet walk or process-table search occurs.
svc_activation_handoff() (
    local bot_dir="$1" expected="$2" tmpdir="$3" mode="${4:-stop}" actual session sessions declared_tmpdir
    case "$mode" in stop|explicit) ;; *) return 3 ;; esac
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
    if [ "$mode" = explicit ]; then
        "$_SUPERVISOR_LIB_DIR/pre-stop-handoff.sh" "$bot_dir" --explicit
    else
        "$_SUPERVISOR_LIB_DIR/pre-stop-handoff.sh" "$bot_dir"
    fi
)

svc_activation_stop_private_server() (
    local bot_dir="$1" expected="$2" tmpdir="$3" sessions session physical_tmpdir absent="${4:-refuse}"
    case "$absent" in refuse|retired|retired-purge) ;; *) return 3 ;; esac
    case "$bot_dir" in /*) ;; *) return 3 ;; esac
    case "$expected" in ''|*[!a-zA-Z0-9_.-]*) return 3 ;; esac
    case "$tmpdir" in /*) ;; *) return 3 ;; esac
    case "$tmpdir" in *$'\n'*|*$'\t'*) return 3 ;; esac
    export TMUX_TMPDIR="$tmpdir"
    . "$_SUPERVISOR_LIB_DIR/lib-common.sh" || return 3
    session=$(tmux_session_name "$bot_dir") || return 3
    if ! sessions=$(LC_ALL=C bot_tmux "$expected" list-sessions -F '#{session_name}' 2>&1); then
        # tmux leaves its socket file after a clean server exit, but a full
        # live-server backlog can print the same no-server text. Ordinary
        # retired cleanup may leave the directory; purge must refuse it.
        # Permission/connection/other failures remain unknown.
        case "$absent" in retired|retired-purge) ;; *) return 3 ;; esac
        physical_tmpdir=$(cd "$tmpdir" && pwd -P) || return 3
        case "$sessions" in
            "no server running on $tmpdir/tmux-$(id -u)/$expected"|\
            "no server running on $physical_tmpdir/tmux-$(id -u)/$expected")
                if [ "$absent" = retired-purge ]; then
                    echo 'retired private server liveness is unverified; inspect the retained socket and bot session, then clean up manually before purge' >&2
                    return 3
                fi
                return 0 ;;
            *) return 3 ;;
        esac
    fi
    [ "$sessions" = "$session" ] || return 3
    printf 'effect-attempted\n'
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
    local python="${CLAUDLOBBY_NATIVE_PYTHON-python3}"
    _svc_activation_read "$file" "$target" || { _svc_activation_unknown "$target state/domain"; return 3; }
    case "$_OS" in
        Linux)
            if [ -n "$SVC_ACT_GROUP" ]; then
                "$python" "$_SUPERVISOR_LIB_DIR/supervisor-caller.py" cgroup "$caller" "$SVC_ACT_GROUP" || rc=$?
            elif [ "${target##*.}" = service ]; then
                case "$SVC_ACT_ACTIVE:$SVC_ACT_SUB:$SVC_ACT_MAIN_PID:$SVC_ACT_CONTROL_PID" in
                    activating:auto-restart:0:0|failed:*:0:0)
                        "$python" "$_SUPERVISOR_LIB_DIR/supervisor-caller.py" unit "$caller" "$target" || rc=$?
                        ;;
                    active:*|activating:*|deactivating:*) rc=3 ;;
                esac
            fi
            ;;
        Darwin)
            # A detached/reparented child can lack the target's current PID in
            # its chain. Prove a different loaded job owns the chain, or block.
            "$python" "$_SUPERVISOR_LIB_DIR/supervisor-caller.py" launchd "$caller" "$SVC_ACT_PID:$SVC_ACT_JOB_PIDS" || rc=$?
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
    local file="$1" target="$2" saved="$3" remaining=20 reset=0 timer_owned="${5:-}"
    case "$timer_owned" in ""|timer-owned) ;; *) return 3 ;; esac
    [ -z "$timer_owned" ] || { [ "$_OS" = Linux ] && [ "${target##*.}" = service ]; } || return 3
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
            # systemctl may acknowledge a timer stop while the exact unit is
            # still deactivating. Observe its final state before publication.
            while :; do
                _svc_activation_read "$file" "$target" || return 3
                if [ "$SVC_ACT_LOAD" = masked ] && [ "$SVC_ACT_ACTIVE" = inactive ]; then
                    break
                fi
                # Parking a timer's service first fails the timer ("Unit to
                # trigger vanished"); stop does not clear that. Reset only this
                # exact masked, stopped timer, once; a failed service refuses.
                if [ "$reset" = 0 ] && [ "${target##*.}" = timer ] \
                        && [ "$SVC_ACT_LOAD:$SVC_ACT_ACTIVE" = masked:failed ]; then
                    systemctl --user reset-failed "$target" || return $?
                    reset=1
                    continue
                fi
                # The owner supplies this capability only from frozen timer
                # membership. Preserve native failure history; never run the job.
                if [ "$reset" = 0 ] && [ "$timer_owned" = timer-owned ] \
                        && [ "$SVC_ACT_LOAD:$SVC_ACT_ACTIVE:$SVC_ACT_MAIN_PID:$SVC_ACT_CONTROL_PID" = masked:failed:0:0 ]; then
                    printf 'activation parking: %s raw state masked failed MainPID=0 ControlPID=0\n' "$target" >&2
                    systemctl --user reset-failed "$target" || return $?
                    reset=1
                    continue
                fi
                [ "$remaining" -gt 0 ] || {
                    _svc_activation_unknown "$target did not settle after stop"; return 3;
                }
                remaining=$((remaining - 1))
                sleep 0.1 || return 3
            done
            ;;
        Darwin)
            if [ "$SVC_ACT_LOAD" != unloaded ]; then
                launchctl bootout "$target" || return $?
                # launchctl can acknowledge bootout before the exact job leaves
                # its domain. A first snapshot in that interval is not failure.
                while :; do
                    _svc_activation_read "$file" "$target" || {
                        _svc_activation_unknown "$target state after bootout"; return 3;
                    }
                    if [ "$SVC_ACT_LOAD" = unloaded ] && [ "$SVC_ACT_ACTIVE" = inactive ]; then
                        break
                    fi
                    [ "$remaining" -gt 0 ] || {
                        _svc_activation_unknown "$target did not unload after bootout"; return 3;
                    }
                    remaining=$((remaining - 1))
                    sleep 0.1 || {
                        _svc_activation_unknown "$target post-bootout wait interrupted"; return 3;
                    }
                done
            fi
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
            # Publication removes the original pause's hidden runtime masks; a
            # surviving runtime node under a loaded candidate is never started.
            local link="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/systemd/user/$target"
            [ ! -e "$link" ] && [ ! -L "$link" ] || {
                _svc_activation_unknown "$target runtime mask survives publication"; return 3;
            }
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
    if [ "$_OS:$SVC_ACT_ACTIVE" != Linux:failed ]; then
        [ "$SVC_ACT_ACTIVE" = inactive ] || { _svc_activation_unknown "$target remains active"; return 3; }
    else
        [ "$SVC_ACT_MAIN_PID:$SVC_ACT_CONTROL_PID" = 0:0 ] && [ -z "$SVC_ACT_GROUP" ] || {
            _svc_activation_unknown "$target failed unit still has process witnesses"; return 3;
        }
    fi
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
    local bot_dir="$2" ceiling token override="${3:-}"
    [ -d "$bot_dir" ] && [ -r "$bot_dir/bot.conf" ] || return 3
    . "$_SUPERVISOR_LIB_DIR/rolling-restart.sh" || return 3
    if [ -n "$override" ]; then
        case "$override" in *[!0-9]*) return 3 ;; esac
        [ "$override" -gt 0 ] || return 3
        ceiling="$override"
    else
        ceiling=$(rr_bot_ceiling "$bot_dir") || return 3
    fi
    case "$ceiling" in ''|*[!0-9]*) return 3 ;; esac
    token=$(bridge_fence_write "$bot_dir") || return 3
    [ -n "$token" ] && grep -Fq -- "$token" "$bot_dir/logs/startup.log" || return 3
    printf '%s\t%s\n' "$ceiling" "$token"
)

svc_activation_bot_ready() (
    export CLAUDLOBBY_ROOT="$1"
    local bot_dir="$2" ceiling="$3" token="$4" socket session outcome=channel handle
    case "$ceiling" in ''|*[!0-9]*) return 3 ;; esac
    [ -n "$token" ] || return 3
    . "$_SUPERVISOR_LIB_DIR/rolling-restart.sh" || return 3
    handle=$(bot_conf_get "$bot_dir" TELEGRAM_BOT_HANDLE "") || return 3
    if [ -z "$handle" ]; then
        outcome=no_handle
    elif bot_expects_no_token "$bot_dir"; then
        outcome=expected_no_token
    fi
    wait_bridge_ready "$bot_dir" "$ceiling" "$token" "$outcome" || return $?
    socket=$(tmux_socket_for_bot "$bot_dir") || return 3
    [ -n "$socket" ] || return 3
    session=$(tmux_session_name "$bot_dir") || return 3
    check_tmux_session "$session" "$socket" || return 3
    case "$WAIT_BRIDGE_READY_OUTCOME" in
        bridge) printf 'bridge-ready\n' ;;
        session) printf 'session-ready\n' ;;
        *) return 3 ;;
    esac
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

# A Background caller's current catalog omits the same user's GUI domain.
svc_inventory_gui_list() {
    [ "$_OS" = Darwin ] || return 3
    /bin/launchctl asuser "$(id -u)" /bin/launchctl list
}

svc_inventory_properties() {
    case "$_OS" in
        Linux)
            [[ "$1" =~ ^([a-zA-Z0-9_.@:-]|\\x[0-9a-fA-F]{2})+$ ]] || return 3
            systemctl --user show --property=Id,LoadState,ActiveState,UnitFileState,FragmentPath,WorkingDirectory,Environment,ExecStart,DropInPaths,NeedDaemonReload,Triggers,TriggeredBy "$1"
            ;;
        Darwin)
            # launchctl list in an Aqua session also reports same-UID user
            # services. This read-only inventory query may inspect either
            # domain; activation controls still require the manager domain.
            if ! _svc_inventory_domain "${1%/*}"; then
                local uid manager
                uid=$(launchctl manageruid) || return 3
                manager=$(launchctl managername) || return 3
                case "$uid" in ''|*[!0-9]*) return 3 ;; esac
                [ "$manager" = Aqua ] && [ "${1%/*}" = "user/$uid" ] || return 3
            fi
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
