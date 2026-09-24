#!/usr/bin/env bash
# update-claude-code.sh — Download the latest Claude Code binary daily.
#
# Download-only. Intended to run daily via systemd timer. Idempotent: if already
# on latest, does nothing. It does NOT restart any bot — the binary cannot
# hot-reload, so it is applied on the next restart instead: any natural restart,
# or the weekly worker-only bounce (weekly-worker-restart.sh). Retiring the old
# daily fleet-bounce here is what removes the daily-reset context loss.
#
# SUCCESS IS A MEASUREMENT OF THE STAGED BINARY, NOT OF npm: the binary the
# fleet launches must RUN (exit 0) and print a parseable version. npm can exit 0
# while omitting the platform-native optional dependency, leaving a stub that
# exits 1 — every bot that starts or restarts afterwards fails to launch, while
# the bots already running hide it. So an UNRUNNABLE binary is a different
# signal from a STALE one (stale is bounded to <=1 week by the weekly worker
# restart): it is raised at both ends of a run — binary_unrunnable before the
# install, binary_update_failed after it — and binary_repaired says when a
# reinstall fixed it.
#
# TWO MODES. In place, the default: `npm install -g` over the binary the fleet
# launches (under sudo when that is a root-owned system install), so a broken
# install is live the moment it lands. Staged, opt-in with
# CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED=1 (#1768): each version goes into its own npm
# prefix under state/claude/versions, is verified THERE, and only then is the
# one fleet link (state/bin/claude) moved, in a single rename, keeping the
# previous version. No sudo, and a failed install never reaches a bot.
#
# Usage: update-claude-code.sh [<fleet-name>]
#   The optional fleet name is recorded with the run; this script restarts no bot.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

# Timer environments carry a minimal PATH; own tool resolution here so npm /
# claude resolve identically under systemd, launchd, cron, or a shell.
# _HOMEBREW (lib-common) covers brew-installed node on macOS.
# PREPENDS (unlike own_tool_path, which appends): must run the NEWEST npm to do
# the install, while detecting the binary to UPDATE via _FLEET_PATH below (#635).
PATH="$HOME/.local/bin:$HOME/.npm-global/bin${_HOMEBREW:+:$_HOMEBREW/bin}:$PATH"
export PATH

FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
BOTS_DIR="$(resolve_bots_dir "$FLEET")"
LOG_DIR="${CLAUDLOBBY_ROOT}/state"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/claude-update.log"

# Stamped at write time: how long an install ran is evidence when one goes wrong.
log() { printf '%s %s\n' "$(ts_iso)" "$*" >> "$LOG"; }

# Loud failure: raise it through the shared emit_failure_alert primitive (fleet
# event + manager tmux nudge + Telegram escalation) — the same alert path
# Mechanism 1's reload-fleet.sh uses, so neither mechanism forks it — then exit
# non-zero so the timer run is marked failed.
update_failed() {
    local rc="$1" msg="$2"
    log "UPDATE FAILED — $msg"
    emit_failure_alert "$BOTS_DIR" "binary_update_failed" "$msg"
    exit "$rc"
}

# --- Resolve the binary the FLEET launches (not this script's own PATH) ------
# The update must target the SAME claude that start-bot.sh runs. This script's
# PATH (above) prepends the user prefixes so npm/node resolve under a bare timer
# env — but start-bot.sh exports its launch PATH with the SYSTEM dirs FIRST, so
# the fleet runs e.g. /usr/bin/claude even when ~/.npm-global holds a newer copy.
# Detecting via this script's PATH updated that shadow user copy and left the
# fleet's binary stale (#635). Mirror start-bot's ordering for detection +
# version + the sudo choice; the PATH above still finds npm/node to RUN the
# install. CLAUDE_BIN and the staged fleet link (#1768) come from
# fleet_claude_bin, the resolver start-bot.sh launches through; only the PATH
# fallback's ordering below is still a mirror of start-bot's (#1772).
# CLAUDE_BIN is the same override start-bot.sh launches with — when the fleet
# pins its binary explicitly, the updater targets THAT one (SSOT). Absent it,
# mirror start-bot's launch ordering. CLAUDE_UPDATE_FLEET_PATH lets a test / an
# unusual host substitute the resolution order.
_FLEET_PATH="${CLAUDE_UPDATE_FLEET_PATH:-/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin${_HOMEBREW:+:$_HOMEBREW/bin}}"
fleet_claude() {
    local c
    c="$(fleet_claude_bin)"
    if [ "$c" = claude ]; then
        PATH="$_FLEET_PATH" command -v claude 2>/dev/null || true
    else
        printf '%s' "$c"
    fi
}

# --- The one predicate: does the fleet's binary work? ------------------------
# It RAN (exit 0) and the first line of its stdout carries a parseable X.Y.Z.
# Sets CLAUDE_VERSION and returns 0; otherwise leaves CLAUDE_VERSION empty, sets
# CLAUDE_VERSION_WHY and returns 1. There is deliberately NO sentinel value: a
# could-not-measure rendered as a string gets logged, compared and diffed like a
# version. Callers branch on the return code, never on the text. This is the
# rule claudlobby/source_state.py decides for read doors (unreachable is not a
# value); that module answers whether a PATH can be opened, and a stub that
# cannot run opens fine. Local until a second consumer needs it (#1772).
# Measures $1 when given (a staged binary, #1768), else the fleet's binary.
measure_claude_version() {
    CLAUDE_VERSION=""
    CLAUDE_VERSION_WHY=""
    local p out first said rc=0 re='[0-9]+\.[0-9]+\.[0-9]+'
    p="${1:-$(fleet_claude)}"
    if [ -z "$p" ]; then
        CLAUDE_VERSION_WHY="no claude binary resolved"
        return 1
    fi
    # Settled inside the substitution (|| exit) so install_error_trap never sees
    # the failing binary. Unneeded on bash 5.2 (measured: callers are all `if`s,
    # whose ERR suppression reaches in); kept for bash 3.2, unmeasured.
    out="$("$p" --version 2>/dev/null || exit $?)" || rc=$?
    first="${out%%$'\n'*}"
    if [ "$rc" -eq 0 ] && [[ $first =~ $re ]]; then
        CLAUDE_VERSION="${BASH_REMATCH[0]}"
        return 0
    fi
    # Could not measure: say why in the binary's own words. stderr is where a
    # binary that cannot run explains itself; the read above discards it so a
    # warning can never be parsed as the version.
    said="$("$p" --version 2>&1 || true)"
    said="${said%%$'\n'*}"
    if [ "$rc" -ne 0 ]; then
        CLAUDE_VERSION_WHY="$p --version exited $rc"
    else
        CLAUDE_VERSION_WHY="$p --version printed no parseable version"
    fi
    CLAUDE_VERSION_WHY="$CLAUDE_VERSION_WHY${said:+: ${said:0:200}}"
    return 1
}

# --- Measure the fleet's binary BEFORE the install ----------------------------
_claude_path="$(fleet_claude)"
old_version=""
old_why=""
_current="not installed"
if [ -n "$_claude_path" ]; then
    if measure_claude_version; then
        old_version="$CLAUDE_VERSION"
        _current="$old_version"
    else
        old_why="$CLAUDE_VERSION_WHY"
        _current="CANNOT RUN ($old_why)"
    fi
fi
log "UPDATE starting (current: $_current, target: ${_claude_path:-none}, fleet: ${FLEET:-none})"

# A binary that cannot run is an outage already in progress: every bot that
# starts or restarts on this host launches it. Say so BEFORE the install, which
# can run for many minutes and may not repair it.
if [ -n "$old_why" ]; then
    log "UPDATE ALERT — the fleet's binary cannot run before the update: $old_why"
    emit_failure_alert "$BOTS_DIR" "binary_unrunnable" \
        "the fleet's claude binary cannot run ($old_why) — a bot that starts or restarts on this host will not launch until it is repaired; reinstalling now"
fi

# --- The staged update (#1768): OPT-IN, CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED=1 --------
# The in-place install below writes over the binary every bot launches AND the
# file every running session re-executes (a session's grep/rg are its own binary
# run as a multi-call tool, through CLAUDE_CODE_EXECPATH, the RESOLVED path). On
# 2026-09-23 npm exited 0 leaving a 500-byte stub, and the host could not start a
# bot for 23 h (#1767). Staged, a version is installed into its OWN npm prefix
# under CLAUDLOBBY_ROOT, measured THERE, and only then does the ONE link the
# fleet launches (fleet_claude_bin) move, in a single rename, keeping the
# previous version. An unverified install is never visible to a bot, a failed
# one leaves the fleet exactly where it was, and no step needs sudo. Off by
# default: it decides which binary every bot on the host launches, and lib/ has
# no deployment gate of its own, so the switch is the rollout
# (claudlobby/switches.py).
_CLAUDE_PKG="@anthropic-ai/claude-code"
_CLAUDE_EXE_REL="lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
# A real Claude Code binary is ~226 MB and the stub a broken install leaves is
# 500 bytes (both measured on the Pi, #1768), so the floor sits far from both.
_CLAUDE_MIN_BINARY_BYTES="${CLAUDE_MIN_BINARY_BYTES:-10000000}"
# The live process table; a seam, so a host without one is testable here.
_PROC_DIR="${CLAUDE_UPDATE_PROC_DIR:-/proc}"
_STAGED_LINK="$CLAUDLOBBY_ROOT/$_FLEET_CLAUDE_LINK_REL"
_STAGED_VERSIONS="$CLAUDLOBBY_ROOT/$_FLEET_CLAUDE_VERSIONS_REL"

# verify_staged <exe>: rc 0 iff <exe> ran and printed a version
# (measure_claude_version, #1770) AND is above the size floor. Run first, so a
# stub that fails is reported in its own words (it names the cause); the floor
# then catches what runs but cannot be Claude Code. STAGED_WHY says which.
verify_staged() {
    local exe="$1" size
    STAGED_WHY=""
    if [ ! -x "$exe" ]; then
        STAGED_WHY="no executable at $exe"
        return 1
    fi
    if ! measure_claude_version "$exe"; then
        STAGED_WHY="$CLAUDE_VERSION_WHY"
        return 1
    fi
    size="$(wc -c <"$exe" 2>/dev/null | tr -d ' ' || true)"
    case "$size" in "" | *[!0-9]*) size=0 ;; esac
    if [ "$size" -lt "$_CLAUDE_MIN_BINARY_BYTES" ]; then
        STAGED_WHY="$exe is $size bytes, under the $_CLAUDE_MIN_BINARY_BYTES-byte floor: not a Claude Code binary"
        return 1
    fi
}

# prune_plan: decide, for every staged version, "keep <name> <why>" or
# "delete <name>" -- or refuse to decide. It exits 3, having printed nothing a
# caller may act on, when ANY input it protects cannot be read: the process
# table (absent, unlistable, or showing no process whose executable it can
# read), the fleet link, .previous, or the versions dir itself. #1146's rule:
# an answer that could not be read never licenses a delete, and here the delete
# is the 2026-09-23 break (a session whose binary vanished). Every path is
# compared RESOLVED on both sides: the table holds the kernel's canonical
# paths, and CLAUDLOBBY_ROOT may be spelled through a symlink or with a
# trailing slash, so a raw string test fails to recognise the linked version.
prune_plan() {
    python3 - "$_PROC_DIR" "$_STAGED_VERSIONS" "$_STAGED_LINK" <<'PY'
import os
import sys

proc, vroot, link = sys.argv[1:4]


def refuse(why):
    sys.stderr.write("prune: %s\n" % why)
    sys.exit(3)


try:
    pids = [p for p in os.listdir(proc) if p.isdigit()]
except OSError as e:
    refuse("cannot list the process table at %s (%s)" % (proc, e))
running = []
for pid in pids:
    try:
        running.append(os.readlink(os.path.join(proc, pid, "exe")))
    except OSError:
        # Exited since the listing, a kernel thread, or another user's process.
        continue
# The positive control: a real table always shows this job's own processes, so
# a table showing none is not telling the truth about what is NOT running.
if not running:
    refuse("the process table at %s shows no process whose executable can be read"
           % proc)

protected = []
try:
    protected.append(os.path.realpath(os.readlink(link)))
except FileNotFoundError:
    pass
except OSError as e:
    refuse("cannot read the fleet link %s (%s)" % (link, e))
try:
    with open(os.path.join(vroot, ".previous")) as f:
        previous = f.read().strip()
    if previous:
        protected.append(os.path.realpath(previous))
except FileNotFoundError:
    pass
except OSError as e:
    refuse("cannot read %s (%s)" % (os.path.join(vroot, ".previous"), e))

try:
    names = sorted(os.listdir(vroot))
except OSError as e:
    refuse("cannot list %s (%s)" % (vroot, e))
for name in names:
    d = os.path.join(vroot, name)
    if name.startswith(".") or os.path.islink(d) or not os.path.isdir(d):
        continue
    here = os.path.realpath(d) + os.sep
    if any(p.startswith(here) for p in protected):
        print("keep %s linked-or-previous" % name)
    elif any(e.startswith(here) for e in running):
        print("keep %s running" % name)
    else:
        print("delete %s" % name)
PY
}

# prune_refused <why>: keep everything, and say so where the operator looks.
prune_refused() {
    log "UPDATE prune REFUSED: $1; nothing deleted, every staged version kept"
    emit_fleet_notice "$BOTS_DIR" "binary_prune_skipped" \
        "the staged claude prune refused to run ($1). Nothing was deleted; staged versions (~230 MB each) accumulate until it can read everything it protects again"
}

# prune_versions: keep the linked version, the previous one, and every version a
# live process executes from; delete the rest, and only on a plan made from a
# complete read (prune_plan). With no process table at all it deletes nothing.
prune_versions() {
    local plan="$_STAGED_VERSIONS/.prune-plan" verdict name why
    if [ ! -e "$_PROC_DIR/self/exe" ]; then
        prune_refused "no process table at $_PROC_DIR, so which versions are running cannot be read"
        return 0
    fi
    # A top-level `if` into a file, never a command substitution: the refusal is
    # a non-zero exit, and install_error_trap fires inside a substitution
    # whatever surrounds it, which would turn a correct refusal into a
    # script_error alert. The path is fixed because the run holds the lock.
    if ! prune_plan >"$plan" 2>>"$LOG"; then
        rm -f "$plan"
        prune_refused "it could not read everything it protects (the log line above says what)"
        return 0
    fi
    while read -r verdict name why; do
        case "$verdict" in
            delete) rm -rf "${_STAGED_VERSIONS:?}/$name" && log "UPDATE prune: removed $name" ;;
            keep) [ "$why" = running ] && log "UPDATE prune: kept $name, a running process executes from it" ;;
        esac
    done <"$plan"
    rm -f "$plan"
}

# staged_update: the whole staged run. Runs under the lock (below), in a
# subshell, so update_failed's exit ends the run and nothing else.
staged_update() {
    local target cur vdir exe stage npm_rc=0 was
    # A crashed run's staging: never linked, so never launched. Safe to remove
    # only because this runs under the lock, where no live run's staging exists.
    rm -rf "$_STAGED_VERSIONS"/.staging-* 2>/dev/null || true
    cur="$(readlink "$_STAGED_LINK" 2>/dev/null || true)"
    was="${cur:-the system claude}"
    # The version to stage: CLAUDE_UPDATE_VERSION pins one, else npm's latest.
    target="${CLAUDE_UPDATE_VERSION:-}"
    if [ -z "$target" ]; then
        target="$(npm view "$_CLAUDE_PKG" version 2>>"$LOG" || true)"
        target="${target##*$'\n'}"
    fi
    if ! [[ $target =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        update_failed 1 "staged update: could not resolve a version to stage (got '${target:0:80}'); the fleet link was NOT moved and the fleet still launches $was"
    fi
    vdir="$_STAGED_VERSIONS/$target"
    exe="$vdir/$_CLAUDE_EXE_REL"
    if [ -e "$_STAGED_LINK" ] && [ "$_STAGED_LINK" -ef "$exe" ] && verify_staged "$exe"; then
        log "UPDATE no-op (staged): the fleet link already runs $target"
        prune_versions
        return 0
    fi
    if [ ! -d "$vdir" ]; then
        stage="$_STAGED_VERSIONS/.staging-$target-$$"
        log "UPDATE staging (no sudo): npm install -g --prefix $stage $_CLAUDE_PKG@$target"
        npm install -g --prefix "$stage" --no-fund --no-audit "$_CLAUDE_PKG@$target" \
            >>"$LOG" 2>&1 || npm_rc=$?
        log "UPDATE staging finished (npm exit $npm_rc)"
        if ! verify_staged "$stage/$_CLAUDE_EXE_REL"; then
            rm -rf "$stage"
            update_failed 1 "staged $target cannot run ($STAGED_WHY; npm exit $npm_rc) — the fleet link was NOT moved; the fleet still launches $was"
        fi
        if ! mv "$stage" "$vdir"; then
            rm -rf "$stage"
            update_failed 1 "the verified staging of $target could not be moved into $vdir; the fleet link was NOT moved and the fleet still launches $was"
        fi
    fi
    # Measured again where the link will point: that file, not the staging copy,
    # is what a bot launches.
    if ! verify_staged "$exe"; then
        update_failed 1 "$target at $vdir cannot run ($STAGED_WHY) — the fleet link was NOT moved; remove $vdir to restage it"
    fi
    new_version="$CLAUDE_VERSION"
    # The rollback is the version the fleet launched before this one, recorded
    # BEFORE the swap: a crash between the two then costs the pointer to the
    # version before last, never to the one the fleet is leaving. A swap that
    # fails puts the old pointer back. Re-linking the version already linked must
    # not record it as its own rollback.
    local prev_file="$_STAGED_VERSIONS/.previous" prev_before="" had_prev=0
    if [ -f "$prev_file" ]; then
        had_prev=1
        prev_before="$(cat "$prev_file" 2>/dev/null || true)"
    fi
    if [ -n "$cur" ] && ! [ "$cur" -ef "$exe" ]; then
        if ! printf '%s\n' "$cur" >"$prev_file"; then
            update_failed 1 "could not record the rollback pointer $prev_file; the fleet link was NOT moved and the fleet still launches $was"
        fi
    fi
    if ! atomic_link_swap "$_STAGED_LINK" "$exe" 2>>"$LOG"; then
        if [ "$had_prev" = 1 ]; then
            printf '%s\n' "$prev_before" >"$prev_file" || true
        else
            rm -f "$prev_file"
        fi
        update_failed 1 "the link swap failed ($_STAGED_LINK -> $exe); the fleet link was NOT moved and the fleet still launches $was"
    fi
    log "UPDATE linked (staged): the fleet link now runs $new_version (previous: $was); each bot picks it up at its next restart"
    if [ -n "$old_why" ]; then
        emit_fleet_notice "$BOTS_DIR" "binary_repaired" \
            "the fleet's claude binary runs again: $new_version, staged and linked (before this update it could not run: $old_why)"
    fi
    prune_versions
}

_staged_update_isolated() { (staged_update); }

if [ "${CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED:-0}" = "1" ]; then
    if [ -n "${CLAUDE_BIN:-}" ]; then
        log "UPDATE skipped (staged): CLAUDE_BIN pins the fleet's binary to $CLAUDE_BIN, so a staged link would never be launched"
        exit 0
    fi
    mkdir -p "$_STAGED_VERSIONS" "$(dirname "$_STAGED_LINK")"
    # One run at a time. A second run waits, then finds the first run's work
    # done; without the lock, its staging clean-up would delete a LIVE staging.
    _staged_rc=0
    with_lock "$_STAGED_VERSIONS/.lock" _staged_update_isolated || _staged_rc=$?
    exit "$_staged_rc"
fi

# Staged updates OFF, but an earlier armed run left the fleet launching the staged
# link (the resolver says so): an in-place install would update a binary no bot
# runs. Say so, change nothing, and name both ways out.
if [ "$_claude_path" = "$_STAGED_LINK" ]; then
    _staged_to="$(readlink "$_STAGED_LINK" 2>/dev/null || true)"
    log "UPDATE skipped — staged updates are OFF, but the fleet launches the staged link $_STAGED_LINK -> $_staged_to; an in-place install would not reach it. Re-arm CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED=1, or remove the link to return the fleet to the system claude."
    emit_fleet_notice "$BOTS_DIR" "binary_update_skipped" \
        "staged claude updates are off, but the fleet still launches the staged link ($_staged_to); nothing was updated. Re-arm CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED=1, or remove $_STAGED_LINK to return to the system claude"
    exit 0
fi

# --- Install: elevate only when the fleet's binary is a root-owned system install
_npm=(npm install -g @anthropic-ai/claude-code@latest)
if [ -n "$_claude_path" ] && [[ "$_claude_path" == /usr/* ]]; then
    _npm=(sudo "${_npm[@]}")
fi
# The repair an unrunnable result needs: the install again. The stub's own advice
# (the package's install.cjs) exits 0 and repairs nothing when the platform
# package is absent.
_repair="to repair, re-run: ${_npm[*]} (not the package's install.cjs, which cannot restore a missing platform package), then check --version"
log "UPDATE running: ${_npm[*]}"
npm_rc=0
"${_npm[@]}" >> "$LOG" 2>&1 || npm_rc=$?
log "UPDATE install finished (npm exit $npm_rc)"

# --- Verify the STAGED binary: this, not npm's exit status, is the verdict ----
if measure_claude_version; then
    new_version="$CLAUDE_VERSION"
    [ "$npm_rc" -eq 0 ] || update_failed 1 "npm install returned $npm_rc — the fleet's binary runs $new_version"
elif [ "$npm_rc" -ne 0 ]; then
    update_failed 1 "npm install returned $npm_rc and the fleet's binary cannot run ($CLAUDE_VERSION_WHY); $_repair"
else
    update_failed 1 "npm install returned 0 but the staged binary cannot run ($CLAUDE_VERSION_WHY) — a bot that starts or restarts on this host will not launch until it is repaired; $_repair"
fi
log "UPDATE verified: the staged binary ran and reported $new_version"

if [ -n "$old_why" ]; then
    log "UPDATE repaired: the binary could not run before the update and now runs $new_version (staged; applied on next restart)"
    emit_fleet_notice "$BOTS_DIR" "binary_repaired" \
        "the fleet's claude binary runs again: $new_version (before this update it could not run: $old_why)"
elif [ "$old_version" = "$new_version" ]; then
    log "UPDATE no-op: already on $new_version"
else
    # Download-only: the new binary is staged in place. Bots pick it up on their
    # next restart — any natural restart, or the weekly worker-only bounce
    # (weekly-worker-restart.sh). No fleet bounce here: that daily forced
    # restart was the daily-reset context loss this role shift removes.
    log "UPDATE version changed: ${old_version:-not installed} → $new_version (staged; applied on next restart)"
fi
exit 0
