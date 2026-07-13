#!/bin/bash
# migrate-fleet-to-system.sh -- move a flat fleet local/<fleet>/ to nested
# local/<system>/<fleet>/ and re-point its supervised bots. Staged, atomic,
# reversible. Part of Claudlobby #602 (recursive-containment vault).
#
# Usage:
#   migrate-fleet-to-system.sh <fleet> <system>
#   migrate-fleet-to-system.sh --rollback <fleet> <system>
#
# The move is a single atomic same-filesystem rename that carries ALL content
# (tracked config plus the gitignored runtime/ state and logs) together, so
# nothing is stranded -- this is why a plain mv beats git mv, which would leave
# the gitignored runtime/ husk behind. Re-enroll uses install-bot-systemd.sh /
# install-bot.sh (they re-copy the freshly composed unit, re-pointing
# WorkingDirectory) -- NOT spin-up-bot.sh (restart-only, keeps the stale path).
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

_mfs_local() { printf '%s/local' "${CLAUDLOBBY_ROOT:?CLAUDLOBBY_ROOT must be set}"; }

_mfs_dev() {  # echo the device id of a path (macOS stat -f, Linux stat -c)
    case "$(uname)" in
        Darwin) stat -f '%d' "$1" ;;
        *)      stat -c '%d' "$1" ;;
    esac
}

# --- pre-flight: refuse anything that is not safe to migrate ----------------
mfs_preflight() {
    local fleet="$1" system="$2" local_dir src
    local_dir="$(_mfs_local)"
    if ! src="$(resolve_fleet_dir "$fleet")"; then
        printf 'mfs: fleet %s not found\n' "$fleet" >&2; return 1
    fi
    if [ "$src" != "$local_dir/$fleet" ]; then
        printf 'mfs: fleet %s is not flat (resolved to %s) -- already nested?\n' "$fleet" "$src" >&2; return 1
    fi
    mkdir -p "$local_dir/$system"
    if [ "$(_mfs_dev "$src")" != "$(_mfs_dev "$local_dir/$system")" ]; then
        printf 'mfs: %s and %s are on different filesystems -- refusing (a non-atomic cross-fs copy)\n' "$src" "$local_dir/$system" >&2
        return 1
    fi
    return 0
}

# --- the risky file-op: atomic move (+ marker + git stage) ------------------
mfs_move_dir() {
    local fleet="$1" system="$2" local_dir src dst
    local_dir="$(_mfs_local)"
    src="$local_dir/$fleet"
    dst="$local_dir/$system/$fleet"
    [ -d "$src" ] || { printf 'mfs: %s not present\n' "$src" >&2; return 1; }
    [ -e "$dst" ] && { printf 'mfs: %s already exists -- aborting\n' "$dst" >&2; return 1; }
    mkdir -p "$local_dir/$system"
    # interim system marker (F8 later swaps this for a system.yaml)
    : > "$local_dir/$system/.claudron-system"
    # atomic whole-dir rename -- carries tracked config AND gitignored runtime/
    mv "$src" "$dst"
    # stage the tracked move in the vault repo -- do NOT commit (operator/sync commits)
    if [ -d "$local_dir/.git" ]; then git -C "$local_dir" add -A >/dev/null 2>&1 || true; fi
    printf '%s\n' "$dst"
}

# --- rollback: the exact inverse move ---------------------------------------
mfs_rollback_dir() {
    local fleet="$1" system="$2" local_dir src dst remaining
    local_dir="$(_mfs_local)"
    dst="$local_dir/$system/$fleet"
    src="$local_dir/$fleet"
    [ -d "$dst" ] || { printf 'mfs: nested %s not present\n' "$dst" >&2; return 1; }
    [ -e "$src" ] && { printf 'mfs: flat %s already exists -- aborting\n' "$src" >&2; return 1; }
    mv "$dst" "$src"
    # drop the system container if it now holds no fleets (only the marker left)
    if [ -d "$local_dir/$system" ]; then
        remaining="$(find "$local_dir/$system" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -1)"
        if [ -z "$remaining" ]; then
            rm -f "$local_dir/$system/.claudron-system"
            rmdir "$local_dir/$system" 2>/dev/null || true
        fi
    fi
    if [ -d "$local_dir/.git" ]; then git -C "$local_dir" add -A >/dev/null 2>&1 || true; fi
    printf '%s\n' "$src"
}

# --- bot stop / re-enroll ---------------------------------------------------
_mfs_bots() {  # echo each bot dir (has a bot.conf) under a fleet runtime/bots
    local fleet="$1" bots d
    if ! bots="$(resolve_bots_dir "$fleet" 2>/dev/null)"; then return 0; fi
    [ -d "$bots" ] || return 0
    for d in "$bots"/*; do
        [ -f "$d/bot.conf" ] && printf '%s\n' "$d"
    done
}

_mfs_botsvc() {  # read BOT_SERVICE from a bot dir bot.conf (subshell, no leak)
    local d="$1"
    [ -f "$d/bot.conf" ] || return 0
    ( set +u; . "$d/bot.conf" >/dev/null 2>&1; printf '%s' "${BOT_SERVICE:-}" )
}

mfs_stop_bots() {  # graceful stop + disable + UNLINK each bot installed unit
    local fleet="$1" d svc
    while IFS= read -r d; do
        [ -n "$d" ] || continue
        "$LIB_DIR/pre-stop-handoff.sh" "$d" >/dev/null 2>&1 || true
        svc="$(_mfs_botsvc "$d")"
        [ -n "$svc" ] || continue
        case "$(uname)" in
            Linux)
                systemctl --user stop "$svc.service" 2>/dev/null || true
                systemctl --user disable "$svc.service" 2>/dev/null || true
                rm -f "$HOME/.config/systemd/user/$svc.service" 2>/dev/null || true
                systemctl --user daemon-reload 2>/dev/null || true
                ;;
            Darwin)
                launchctl bootout "gui/$(id -u)/$svc" 2>/dev/null || true
                rm -f "$HOME/Library/LaunchAgents/$svc.plist" 2>/dev/null || true
                ;;
        esac
    done < <(_mfs_bots "$fleet")
}

mfs_reenroll_bots() {  # re-install the fresh nested unit + start each bot
    local fleet="$1" installer d
    case "$(uname)" in
        Linux)  installer="$LIB_DIR/install-bot-systemd.sh" ;;
        Darwin) installer="$LIB_DIR/install-bot.sh" ;;
        *) printf 'mfs: unsupported OS\n' >&2; return 1 ;;
    esac
    while IFS= read -r d; do
        [ -n "$d" ] || continue
        "$installer" "$d" || { printf 'mfs: re-enroll failed for %s\n' "$d" >&2; return 1; }
    done < <(_mfs_bots "$fleet")
}

_mfs_generate() {  # regenerate the fleet (nested-aware paths) -- fatal on fail
    local fleet="$1"
    claudlobby --root "$CLAUDLOBBY_ROOT" --fleet "$fleet" generate >/dev/null 2>&1
}

# --- verify -----------------------------------------------------------------
mfs_verify() {
    local fleet="$1"
    if ! "$LIB_DIR/reconcile-fleet.sh" "$fleet" >/dev/null 2>&1; then
        emit_failure_alert "vault-migrate" "fleet $fleet unhealthy after migration -- manual check needed" 2>/dev/null || true
        return 1
    fi
    return 0
}

# --- orchestration ----------------------------------------------------------
mfs_migrate() {
    local fleet="$1" system="$2"
    mfs_preflight "$fleet" "$system" || return 1
    mfs_stop_bots "$fleet"
    mfs_move_dir "$fleet" "$system" >/dev/null || return 1
    if ! _mfs_generate "$fleet"; then
        printf 'mfs: generate failed for %s -- rolling back the move\n' "$fleet" >&2
        mfs_rollback_dir "$fleet" "$system" >/dev/null || true
        _mfs_generate "$fleet" || true
        mfs_reenroll_bots "$fleet" || true
        return 1
    fi
    mfs_reenroll_bots "$fleet" || return 1
    mfs_verify "$fleet" || return 1
    printf 'mfs: migrated %s -> %s/%s (bots healthy)\n' "$fleet" "$system" "$fleet"
}

mfs_rollback() {
    local fleet="$1" system="$2"
    mfs_stop_bots "$fleet" || true
    mfs_rollback_dir "$fleet" "$system" >/dev/null || return 1
    _mfs_generate "$fleet" || true
    mfs_reenroll_bots "$fleet" || return 1
    mfs_verify "$fleet" || return 1
    printf 'mfs: rolled back %s (flat, bots healthy)\n' "$fleet"
}

# --- main (only when executed, not sourced) ---------------------------------
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    install_error_trap 2>/dev/null || true
    case "${1:-}" in
        --rollback) shift; mfs_rollback "${1:?fleet}" "${2:?system}" ;;
        "" | -h | --help)
            printf 'Usage: %s <fleet> <system> | --rollback <fleet> <system>\n' "$(basename "$0")"; exit 2 ;;
        *) mfs_migrate "${1:?fleet}" "${2:?system}" ;;
    esac
fi
