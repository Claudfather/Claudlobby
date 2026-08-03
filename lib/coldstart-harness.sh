#!/bin/bash
# coldstart-harness.sh — mechanical half of the cold-start simulation (documentation/validating-cold-start.md).
#
# Builds a history-free export of the repo, records what the host looked like
# before a cold run, and afterwards reaps everything the run created.
#
# Usage:
#   coldstart-harness.sh prepare [--ref REF] [--dir DIR]
#   coldstart-harness.sh status
#   coldstart-harness.sh reap [--keep-tree] [--dry-run]
#   coldstart-harness.sh transcript
#
# The design rule is instrument-and-reap, not fence. A blind cold run cannot be
# told "do not enroll supervision" without revealing that supervision exists,
# which contaminates the very thing being measured. So the run is left free and
# the host is diffed around it. The escape is the finding.
#
# Safety property: reap only ever removes units, sockets and processes that are
# ABSENT from the pre-run snapshot. A pre-existing production fleet on the same
# host is invisible to it and cannot be torn down by it.

set -euo pipefail

# Capture what the CALLER inherited, before lib-common.sh is sourced: it does
# `: "${CLAUDLOBBY_ROOT:=...}"` + export, so after sourcing the variable is
# always set and preflight could never see the unset case it exists to check.
_INHERITED_ROOT="${CLAUDLOBBY_ROOT:-}"
_INHERITED_VAULT="${CLAUDRON_VAULT_PATH:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/lib-common.sh"
install_error_trap ""

CLAUDLOBBY_SRC="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="${COLDSTART_STATE_DIR:-$HOME/.claudlobby-coldstart}"
SNAP="$STATE_DIR/snapshot"

# State lives outside the export on purpose: reap has to still work after the
# tree is gone, which is exactly the situation a bounced evaluator is left in.

die() { printf 'coldstart: %s\n' "$*" >&2; exit 1; }
say() { printf 'coldstart: %s\n' "$*"; }

# ---------------------------------------------------------------- snapshotting

snap_launch_units() {
    # One unit label per line, sorted. Empty file on an unsupported platform.
    if [ "$_OS" = "Darwin" ]; then
        launchctl list 2>/dev/null | awk 'NR>1 {print $3}' | sort -u
    else
        systemctl --user list-unit-files --no-legend --no-pager 2>/dev/null \
            | awk '{print $1}' | sort -u
    fi
}

snap_unit_files() {
    if [ "$_OS" = "Darwin" ]; then
        ls -1 "$HOME/Library/LaunchAgents" 2>/dev/null | sort -u
    else
        ls -1 "$HOME/.config/systemd/user" 2>/dev/null | sort -u
    fi
}

snap_tmux_sockets() {
    local dir="/tmp/tmux-$(id -u)"
    [ -d "/private/tmp/tmux-$(id -u)" ] && dir="/private/tmp/tmux-$(id -u)"
    ls -1 "$dir" 2>/dev/null | sort -u
}

write_snapshot() {
    mkdir -p "$SNAP"
    snap_launch_units  > "$SNAP/units.txt"
    snap_unit_files    > "$SNAP/unitfiles.txt"
    snap_tmux_sockets  > "$SNAP/sockets.txt"
    say "snapshot: $(wc -l < "$SNAP/units.txt" | tr -d ' ') units, $(wc -l < "$SNAP/unitfiles.txt" | tr -d ' ') unit files, $(wc -l < "$SNAP/sockets.txt" | tr -d ' ') tmux sockets"
}

# New items = present now, absent in the snapshot.
#
# For units this is NOT sufficient on its own. macOS churns per-app agents
# constantly (Spotlight spawns com.apple.mdworker.shared.* between any two
# samples), so a raw set difference reports OS noise as "created by the run" —
# and reap would then bootout Apple daemons. Ownership is therefore established
# positively: a unit we enrolled always has a corresponding NEW unit file in the
# user unit directory, which a transient system agent never does. The
# com.apple.* exclusion is belt-and-braces on top of that.
new_since() {
    local kind="$1" now
    now="$(safe_mktemp)"
    case "$kind" in
        units)     snap_launch_units  > "$now" ;;
        unitfiles) snap_unit_files    > "$now" ;;
        sockets)   snap_tmux_sockets  > "$now" ;;
    esac
    if [ "$kind" != "units" ]; then
        comm -13 "$SNAP/$kind.txt" "$now"
        return 0
    fi

    local files unit
    files="$(safe_mktemp)"
    snap_unit_files | comm -13 "$SNAP/unitfiles.txt" - > "$files"
    comm -13 "$SNAP/units.txt" "$now" | while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        case "$unit" in com.apple.*) continue ;; esac
        # Keep only units backed by a unit file this run also introduced.
        if grep -qxF "$unit.plist" "$files" 2>/dev/null \
           || grep -qxF "$unit" "$files" 2>/dev/null \
           || grep -qxF "$unit.service" "$files" 2>/dev/null; then
            printf '%s\n' "$unit"
        fi
    done
}

# ------------------------------------------------------------------- preflight

preflight() {
    local bad=0
    # A cold run must resolve everything from its own tree. An inherited root or
    # vault pointer silently aims the run at the real install.
    if [ -n "$_INHERITED_ROOT" ]; then
        say "WARN: CLAUDLOBBY_ROOT is set ($_INHERITED_ROOT) — unset it before the run"; bad=1
    fi
    if [ -n "$_INHERITED_VAULT" ]; then
        say "WARN: CLAUDRON_VAULT_PATH is set — unset it before the run"; bad=1
    fi
    if [ -f "$HOME/.claude/CLAUDE.md" ]; then
        say "WARN: a user-level ~/.claude/CLAUDE.md exists — it leaks context into the cold session"; bad=1
    fi
    [ "$bad" -eq 0 ] && say "preflight: clean (no inherited root, vault or user CLAUDE.md)"
    return 0
}

# --------------------------------------------------------------------- prepare

cmd_prepare() {
    local ref="HEAD" dir=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref) ref="${2:?--ref needs a value}"; shift 2 ;;
            --dir) dir="${2:?--dir needs a value}"; shift 2 ;;
            *) die "unknown option: $1" ;;
        esac
    done
    if [ -z "$dir" ]; then
        # Trim any trailing slash on TMPDIR — macOS exports it with one, which
        # would otherwise show up as a doubled slash in every path we print.
        local base="${TMPDIR:-/tmp}"
        dir="${base%/}/claudlobby-coldstart/tree"
    fi

    git -C "$CLAUDLOBBY_SRC" rev-parse --verify "$ref" >/dev/null 2>&1 \
        || die "not a valid ref: $ref"

    preflight

    rm -rf "$dir"; mkdir -p "$dir"
    # Export, never clone: a .git carries the commit messages that describe the
    # very defects the run is supposed to rediscover.
    git -C "$CLAUDLOBBY_SRC" archive "$ref" | tar -x -C "$dir"

    local carried=""
    for x in .git local .venv .env fleet.yaml runtime state; do
        [ -e "$dir/$x" ] && carried="$carried $x"
    done
    [ -n "$carried" ] && die "export is contaminated, carries:$carried"

    write_snapshot
    mkdir -p "$STATE_DIR"
    {
        printf 'ref=%s\n' "$(git -C "$CLAUDLOBBY_SRC" rev-parse "$ref")"
        printf 'tree=%s\n' "$dir"
        # Recorded now, while the tree still exists: Claude Code keys its project
        # dir by the REALPATH, and transcript lookup has to keep working after
        # reap has deleted the tree — at which point pwd -P can no longer resolve.
        printf 'tree_real=%s\n' "$(cd "$dir" && pwd -P)"
        printf 'src=%s\n'  "$CLAUDLOBBY_SRC"
    } > "$STATE_DIR/run.env"

    say "exported $ref -> $dir ($(find "$dir" -type f | wc -l | tr -d ' ') files, no history)"
    printf '\n  Run the cold session in a NEW terminal:\n\n    cd %s && claude\n\n  Then type /setup and nothing else.\n\n' "$dir"
    printf '  When it finishes (or you stop it):  %s reap\n\n' "$SCRIPT_DIR/coldstart-harness.sh"
}

# ---------------------------------------------------------------------- status

cmd_status() {
    [ -d "$SNAP" ] || die "no snapshot — run 'prepare' first"
    # shellcheck source=/dev/null
    [ -f "$STATE_DIR/run.env" ] && . "$STATE_DIR/run.env"
    say "tree: ${tree:-<unknown>} $([ -d "${tree:-/nonexistent}" ] && echo '(present)' || echo '(gone)')"
    local n
    for kind in units unitfiles sockets; do
        n="$(new_since "$kind" | wc -l | tr -d ' ')"
        printf '  new %-10s %s\n' "$kind" "$n"
        new_since "$kind" | sed 's/^/      + /'
    done
    if [ -n "${tree:-}" ]; then
        printf '  processes referencing the tree: %s\n' "$(pgrep -f "$tree" 2>/dev/null | wc -l | tr -d ' ')"
    fi
}

# ------------------------------------------------------------------------ reap

cmd_reap() {
    local keep_tree=0 dry=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --keep-tree) keep_tree=1; shift ;;
            --dry-run)   dry=1; shift ;;
            *) die "unknown option: $1" ;;
        esac
    done
    [ -d "$SNAP" ] || die "no snapshot — nothing to reap against"
    # shellcheck source=/dev/null
    [ -f "$STATE_DIR/run.env" ] && . "$STATE_DIR/run.env"

    local pfx="";  [ "$dry" -eq 1 ] && pfx="[dry-run] "

    # Order matters: stop the watchdogs before the thing they watch, or a 60s
    # keepalive walks the bot back up between the two steps.
    local unit
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        say "${pfx}bootout unit: $unit"
        [ "$dry" -eq 1 ] && continue
        if [ "$_OS" = "Darwin" ]; then
            launchctl bootout "gui/$(id -u)/$unit" 2>/dev/null || true
        else
            systemctl --user disable --now "$unit" 2>/dev/null || true
        fi
    done <<< "$(new_since units | grep -v '^$' | sort -r || true)"

    local f unit_dir
    if [ "$_OS" = "Darwin" ]; then unit_dir="$HOME/Library/LaunchAgents"; else unit_dir="$HOME/.config/systemd/user"; fi
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        say "${pfx}remove unit file: $f"
        [ "$dry" -eq 1 ] || rm -f "$unit_dir/$f"
    done <<< "$(new_since unitfiles | grep -v '^$' || true)"

    local sock
    while IFS= read -r sock; do
        [ -n "$sock" ] || continue
        say "${pfx}kill tmux server: $sock"
        [ "$dry" -eq 1 ] && continue
        "$_TMUX_BIN" -L "$sock" kill-server 2>/dev/null || true
        rm -f "/tmp/tmux-$(id -u)/$sock" "/private/tmp/tmux-$(id -u)/$sock" 2>/dev/null || true
    done <<< "$(new_since sockets | grep -v '^$' || true)"

    if [ -n "${tree:-}" ]; then
        local pids
        pids="$(pgrep -f "$tree" 2>/dev/null || true)"
        if [ -n "$pids" ]; then
            say "${pfx}kill processes referencing the tree: $(printf '%s' "$pids" | tr '\n' ' ')"
            [ "$dry" -eq 1 ] || printf '%s\n' "$pids" | xargs kill 2>/dev/null || true
        fi
        if [ "$keep_tree" -eq 0 ] && [ -d "$tree" ]; then
            say "${pfx}remove tree: $tree"
            [ "$dry" -eq 1 ] || rm -rf "$tree"
        fi
    fi

    [ "$dry" -eq 1 ] && return 0
    systemctl --user daemon-reload 2>/dev/null || true
    say "reap complete — verify with: $SCRIPT_DIR/coldstart-harness.sh status"
}

# ------------------------------------------------------------------ transcript

cmd_transcript() {
    # shellcheck source=/dev/null
    [ -f "$STATE_DIR/run.env" ] || die "no run recorded — run 'prepare' first"
    . "$STATE_DIR/run.env"
    # Claude Code keys a project dir by the session cwd with slashes turned into
    # dashes — and it records the REALPATH. On macOS /tmp and /var are symlinks
    # into /private, so the symlink form yields a key that never exists. Try the
    # resolved path first, then the literal one.
    local key proj real
    # Prefer the realpath recorded at prepare time; fall back to resolving now
    # (older run.env, or a tree that still exists).
    real="${tree_real:-}"
    [ -n "$real" ] || real="$(cd "$tree" 2>/dev/null && pwd -P)" || real="$tree"
    for key in "$(printf '%s' "$real" | tr '/' '-')" "$(printf '%s' "$tree" | tr '/' '-')"; do
        if [ -d "$HOME/.claude/projects/$key" ]; then
            proj="$HOME/.claude/projects/$key"; break
        fi
    done
    # No basename glob fallback: the default tree basename is "tree", which
    # matches unrelated projects (any path containing "worktrees"), and silently
    # analysing the wrong session is far worse than reporting none.
    [ -n "${proj:-}" ] && [ -d "$proj" ] || die "no transcript dir for $tree (looked for key $(printf '%s' "$real" | tr '/' '-'))"
    say "transcripts for the cold run:"
    find "$proj" -name '*.jsonl' -exec ls -la {} \;
    # Sibling dirs exist for any bot the run booted; surface those too.
    find "$HOME/.claude/projects" -maxdepth 1 -type d -name "$key-*" 2>/dev/null | sed 's/^/  bot session: /'
}

# ------------------------------------------------------------------------ main

[ $# -ge 1 ] || die "usage: coldstart-harness.sh {prepare|status|reap|transcript} [options]"
cmd="$1"; shift
case "$cmd" in
    prepare)    cmd_prepare "$@" ;;
    status)     cmd_status "$@" ;;
    reap)       cmd_reap "$@" ;;
    transcript) cmd_transcript "$@" ;;
    *)          die "unknown command: $cmd" ;;
esac
