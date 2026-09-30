#!/bin/bash
# reload-fleet.sh — selected-fleet plugin refresh and idle session reload.
#
# Steps (under one fleet-wide lock):
#   1. claude plugin update <each FLEET_PLUGINS_REQUIRED> — refresh the shared
#      host plugin cache (~/.claude/plugins/cache, shared fleet-wide).
#   2. drop data/.reload-pending on every selected RUNNING bot. keepalive.sh performs the
#      actual /reload-plugins + /reload-skills at each bot's next idle tick — a
#      single, idle-gated activation path (fork F2(b) in the update-lifecycle plan).
#
# The public fleet reload command supplies the roster and plugin scope from
# the active sealed plan. Authored config and native enrollment are never changed.
#
# A failed plugin refresh is LOUD, never silent: it emits reload_failed and
# alerts the manager before any bot is marked. Marker-write failures are also
# reported; any earlier markers remain visible to the next keepalive tick.
#
# Needs `claude` and the selected claudlobby CLI. Native callers use the public
# command; this packaged script receives the selected plan scope from it.
#
# Usage: reload-fleet.sh --selected-release ID --fleet NAME --bots-dir DIR
#                        [--plugin NAME]... [--bot NAME]...
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

own_tool_path   # timer PATH is minimal, and stale units predate #802 (#805)

# Fleet timers append their fleet name to the script command. Route that
# historical native entry into the public selected-release admission before
# doing any refresh work; the public adapter re-enters with frozen scope below.
if [ "$#" -eq 1 ] && [ "${1#--}" = "$1" ]; then
    [ "$1" = "${CLAUDLOBBY_FLEET:-}" ] || {
        echo "reload-fleet: timer fleet context differs" >&2; exit 2;
    }
    [ "$LIB_DIR" = "${CLAUDLOBBY_NATIVE_DIR:-}" ] || {
        echo "reload-fleet: timer native owner differs" >&2; exit 2;
    }
    [ -n "${CLAUDLOBBY_RELEASE_ID:-}" ] || {
        echo "reload-fleet: timer has no selected release" >&2; exit 2;
    }
    # shellcheck source=cli-context.sh
    . "$LIB_DIR/cli-context.sh"
    _claudlobby_require_root || exit $?
    _claudlobby_require_cli || exit $?
    exec "$CLAUDLOBBY_CLI" --root "$CLAUDLOBBY_ROOT" --fleet "$1" fleet reload
fi

FLEET="" BOTS_DIR="" SELECTED_RELEASE=""
PLUGINS=() BOTS=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --selected-release|--fleet|--bots-dir|--plugin|--bot)
            [ "$#" -ge 2 ] || { echo "reload-fleet: missing $1 value" >&2; exit 2; }
            case "$1" in
                --selected-release) SELECTED_RELEASE="$2" ;;
                --fleet) FLEET="$2" ;;
                --bots-dir) BOTS_DIR="$2" ;;
                --plugin) PLUGINS+=("$2") ;;
                --bot) BOTS+=("$2") ;;
            esac
            shift 2 ;;
        *) echo "reload-fleet: unexpected argument $1" >&2; exit 2 ;;
    esac
done
case "${CLAUDLOBBY_ROOT:-}:${CLAUDLOBBY_NATIVE_DIR:-}:${CLAUDLOBBY_CLI:-}:$BOTS_DIR" in
    /*:/*:/*:/*) ;;
    *) echo "reload-fleet: selected root, native owner, CLI and bots directory required" >&2; exit 2 ;;
esac
if [ "$LIB_DIR" != "$CLAUDLOBBY_NATIVE_DIR" ] || [ "$SELECTED_RELEASE" != "${CLAUDLOBBY_RELEASE_ID:-}" ] \
        || [ -z "$SELECTED_RELEASE" ] || [ -z "$FLEET" ] \
        || [ "$FLEET" != "${CLAUDLOBBY_FLEET:-}" ]; then
    echo "reload-fleet: selected native or fleet context differs" >&2
    exit 2
fi
for _bot in ${BOTS[@]+"${BOTS[@]}"}; do
    case "$_bot" in ''|.|..|*/*) echo "reload-fleet: invalid selected bot" >&2; exit 2 ;; esac
done
export CLAUDLOBBY_FLEET="$FLEET"
mkdir -p "${CLAUDLOBBY_ROOT}/state"
LOG="${CLAUDLOBBY_ROOT}/state/reload-fleet.log"

# LOUD failure — emit the reload_failed event + alert the manager via the shared
# lib-common primitive (emit_failure_alert is also used by Mechanism 2's
# update-claude-code.sh, so the two mechanisms never fork the alert path).
loud_fail() {
    local reason="$1"
    printf '%s reload_failed: %s\n' "$(ts_iso)" "$reason" >> "$LOG"
    emit_failure_alert "$BOTS_DIR" "reload_failed" "$reason"
}

# --- a run that dies mid-step still says so (#1924) ---------------------------
# Every failure below is loud because it RETURNS to loud_fail. A run that is
# killed never returns: launchd booting out the job that is running it (#1924,
# silent for days), a host shutdown, the OOM killer. So a run records the step
# it is in, in a file named for its own pid, and removes the file only on its
# way out. Two readers:
#   - its EXIT trap, which bash runs on SIGTERM, SIGINT and SIGHUP as well as
#     on a normal end, without waiting for the step in the foreground, raises
#     loud_fail naming the step whenever the run is leaving unfinished;
#   - the NEXT run of this fleet raises for any record whose pid is gone --
#     the only witness to a SIGKILL, which runs no trap at all.
# A file rather than a variable: with flock the critical steps run in
# with_lock's subshell, where a variable set never reaches this shell's trap.
INFLIGHT_DIR="${CLAUDLOBBY_ROOT}/state/reload-fleet.inflight"
RUN_KEY="${FLEET:-root}"
INFLIGHT="$INFLIGHT_DIR/$RUN_KEY.$$"
_RF_FINISHED=0
_RF_STARTED="$(ts_iso)"

# _rf_step <label>: say in the log which step is starting, before it runs,
# and record it where the readers above look.
_rf_step() {
    # With flock the steps run in with_lock's subshell, which a kill aimed at
    # this shell alone leaves running. Once this shell is gone the run is
    # over: start no further step, and never re-create the record its EXIT
    # trap has already raised.
    kill -0 "$$" 2>/dev/null || exit 143
    printf '%s reload-fleet[%s] pid %s step: %s\n' "$(ts_iso)" "$RUN_KEY" "$$" "$1" >> "$LOG"
    { printf 'started=%s\nstep=%s\n' "$_RF_STARTED" "$1" > "$INFLIGHT.tmp" \
        && mv -f "$INFLIGHT.tmp" "$INFLIGHT"; } 2>/dev/null || true
}

# _rf_recorded <file> <key>: one field of a record, or nothing.
_rf_recorded() {
    sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -n 1 || true
}

# Replaces lib-common's EXIT trap, so it runs that cleanup itself.
_rf_on_exit() {
    local rc=$? step how
    if [ "$_RF_FINISHED" != 1 ]; then
        step=$(_rf_recorded "$INFLIGHT" step)
        # After a fatal signal bash reports 0 here (measured, bash 5.2), so an
        # unfinished run with no status of its own is one that was killed.
        if [ "$rc" -gt 128 ]; then
            how="killed by signal $((rc - 128))"
        elif [ "$rc" -eq 0 ]; then
            how="killed"
        else
            how="aborted (exit $rc)"
        fi
        loud_fail "$how during step: ${step:-before its first step}; the run did not finish" || true
    fi
    rm -f "$INFLIGHT" "$INFLIGHT.tmp" 2>/dev/null || true
    _lc_cleanup
}

# A record whose run is gone was left by a kill no trap could answer for.
# Say so now, once, and drop it. A record whose pid is still a reload-fleet
# run belongs to a run in progress, which speaks for itself.
_rf_raise_unfinished() {
    local f name pid args started step
    for f in "$INFLIGHT_DIR/$RUN_KEY".*; do
        [ -f "$f" ] || continue
        name="${f##*/}"
        pid="${name##*.}"
        [ "${name%.*}" = "$RUN_KEY" ] || continue    # a fleet whose name extends this one
        case "$pid" in ''|*[!0-9]*) continue ;; esac  # a .tmp half-write
        [ "$pid" != "$$" ] || continue
        args=$(ps -o args= -p "$pid" 2>/dev/null || true)
        case "$args" in *reload-fleet*) continue ;; esac
        started=$(_rf_recorded "$f" started)
        step=$(_rf_recorded "$f" step)
        loud_fail "a previous run (pid $pid, started ${started:-at an unrecorded time}) never finished: it died during step: ${step:-unrecorded}, where no trap could report it" || true
        rm -f "$f"
    done
}

mkdir -p "$INFLIGHT_DIR"
_rf_raise_unfinished
trap '_rf_on_exit' EXIT
_rf_step "waiting for the reload lock"

# --- plugin/cache refresh, serialized under the fleet-wide lock ---
_reason_file=$(safe_mktemp)
_step_out=$(safe_mktemp)    # reused by every _run_step; one temp, not one per step
_step_rc=$(safe_mktemp)

_warm_npx() {
    printf '%s npx cache degraded — warming (best-effort, once per episode)\n' "$(ts_iso)" >> "$LOG"
    claudlobby_cli ${FLEET:+--fleet "$FLEET"} host cache warm >> "$LOG" 2>&1 || true
}

# _run_step <label> <command...>
# Run one critical step, appending its output to $LOG, and on failure record a
# reason that says what ACTUALLY went wrong — exit status plus the command's own
# last line of output. The previous code discarded both and reported only the
# last command name, so a PATH failure (exit 127, "claude: command not found")
# surfaced as "claude plugin update failed: <plugin>" and read as a broken
# plugin. That misdirection cost the triage, not the outage (#805). Every
# plugin update routes through this step so failures retain the actual cause.
#
# The output streams into $LOG AS THE STEP RUNS, with a copy in $_step_out for
# the reason below. It used to be buffered and appended only once the step
# returned, so a step killed mid-run took all of its output with it.
# The step now runs inside a pipeline, so its status comes back through a
# file, and `|| _rc=$?` keeps the ERR trap exactly as quiet as it was.
_run_step() {
    local label="$1"; shift
    local cmd="$1" rc=0 detail
    _rf_step "$label"
    : > "$_step_rc"
    { _rc=0; "$@" || _rc=$?; printf '%s' "$_rc" > "$_step_rc"; } 2>&1 \
        | tee -a "$LOG" > "$_step_out" || true
    rc=$(cat "$_step_rc" 2>/dev/null || true)
    case "$rc" in ''|*[!0-9]*) rc=1 ;; esac     # no status written: never a success
    [ "$rc" -eq 0 ] && return 0
    # Last non-blank line of the command's own output — the real error text.
    detail=$(grep -v '^[[:space:]]*$' "$_step_out" | tail -n 1)
    # 127 is the unresolvable-tool signature: name the command and the PATH it
    # was not found on, so the alert points at the install rather than the work.
    [ "$rc" -eq 127 ] && detail="$cmd not found on PATH=$PATH"
    printf '%s failed (exit %d)%s' "$label" "$rc" "${detail:+: $detail}" > "$_reason_file"
    return 1
}

_reload_critical() {
    # The public adapter supplied these exact selected-plan locations. Refuse
    # missing or redirected targets before refreshing a shared plugin cache.
    if [ ! -d "$BOTS_DIR" ] || [ -L "$BOTS_DIR" ]; then
        printf 'selected bots directory is missing or redirected: %s' "$BOTS_DIR" > "$_reason_file"
        return 1
    fi
    local _bot bot_dir
    for _bot in ${BOTS[@]+"${BOTS[@]}"}; do
        bot_dir="$BOTS_DIR/$_bot"
        if [ ! -d "$bot_dir" ] || [ -L "$bot_dir" ] || [ -L "$bot_dir/data" ] \
                || [ -L "$bot_dir/data/.reload-pending" ]; then
            printf 'selected bot reload path is missing or redirected: %s' "$_bot" > "$_reason_file"
            return 1
        fi
        if ! FLEET_NAME="$FLEET" tmux_socket_for_bot "$bot_dir" >/dev/null 2>&1; then
            printf 'selected bot has no private tmux socket: %s' "$_bot" > "$_reason_file"
            return 1
        fi
    done
    # step 0: npx cache preflight. A cold cache turns MCP startup into an IO
    # storm on SD-card hardware, so verify before touching plugins — inside
    # the lock, because warm-cache mutates the host-shared ~/.npm/_npx.
    # Cache health never aborts the reload, and a degraded cache warms at
    # most once per episode (debounced): a permanently-missing package must
    # not become a daily warm loop.
    _rf_step "npx cache preflight"
    if "$LIB_DIR/check-npx-cache.sh" ${FLEET:+--fleet "$FLEET"} >> "$LOG" 2>&1; then
        debounce_clear "${CLAUDLOBBY_ROOT}/state" "npx" "warm-attempted"
    else
        debounce_notify "${CLAUDLOBBY_ROOT}/state" "npx" "warm-attempted" _warm_npx ""
    fi
    # Tool preflight: fail on the MISSING TOOL rather than on whichever command
    # happened to run first. A timer env resolves neither by default, and an
    # unresolvable tool is an install/PATH fault — naming it is the whole
    # difference between a 5-minute fix and a two-day silent outage.
    # The binary the fleet launches (fleet_claude_bin: CLAUDE_BIN, else the
    # staged link #1768, else PATH) -- a plugin refreshed through a different
    # binary than the bots run is a refresh nobody boots with.
    local _claude
    _claude="$(fleet_claude_bin)"
    if [ "${#PLUGINS[@]}" -gt 0 ] && ! command -v "$_claude" >/dev/null 2>&1; then
        printf '%s not found on PATH=%s — install Claude Code or set CLAUDE_BIN' "$_claude" "$PATH" > "$_reason_file"
        return 1
    fi
    local _p
    for _p in ${PLUGINS[@]+"${PLUGINS[@]}"}; do
        _run_step "claude plugin update $_p" "$_claude" plugin update "$_p" || return 1
    done
}

if ! with_lock "${CLAUDLOBBY_ROOT}/state/reload-fleet.lock" _reload_critical; then
    reason=$(cat "$_reason_file" 2>/dev/null || true)
    # An empty reason file is a stop no step explained (the step subshell
    # killed, or an abort inside it): name the step instead of alerting blank.
    [ -n "$reason" ] || reason="reload refresh stopped during step: $(_rf_recorded "$INFLIGHT" step)"
    loud_fail "$reason"
    _RF_FINISHED=1    # reported: the EXIT trap must not raise it a second time
    exit 1
fi
# $_reason_file lives under lib-common's _LC_TMPDIR, reaped on exit by _rf_on_exit.

# --- mark selected RUNNING bots for a keepalive-driven live reload ---
_rf_step "mark running bots for live reload"
marked=0
for _bot in ${BOTS[@]+"${BOTS[@]}"}; do
        bot_dir="$BOTS_DIR/$_bot"
        # "Running" = the bot's session is alive on its OWN per-bot server.
        socket=$(FLEET_NAME="$FLEET" tmux_socket_for_bot "$bot_dir")
        if check_tmux_session "$(tmux_session_name "$bot_dir")" "$socket"; then
            mkdir -p "$bot_dir/data"
            touch "$bot_dir/data/.reload-pending"
            marked=$((marked + 1))
            printf 'marked\t%s\n' "$_bot"
        fi
done
for _p in ${PLUGINS[@]+"${PLUGINS[@]}"}; do printf 'refreshed\t%s\n' "$_p"; done
printf '%s reload-fleet: plugin refresh OK, marked %d running bot(s) for idle reload\n' \
    "$(ts_iso)" "$marked" >> "$LOG"
_RF_FINISHED=1
