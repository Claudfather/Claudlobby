#!/bin/bash
# migrate-fleet-to-system.sh — staged, reversible migration of a FLAT fleet
# (local/<fleet>/) into a nested vault SYSTEM container (local/<system>/<fleet>/),
# re-pointing every bot supervision unit at the new path.
#
# Model: the per-bot move-bot pattern at fleet scale — preflight, stop + disable
# + UNLINK the installed unit (the B3 unlink: a restart-only re-enroll keeps a
# stale WorkingDirectory), atomically move the dir, regenerate at the new path,
# then re-enroll via install-bot*.sh (unconditional cp of the freshly composed
# unit), and finally reconcile to prove health.
#
# Why a PLAIN mv (not git mv): one atomic same-filesystem rename moves ALL
# content — the git-tracked tree AND the gitignored runtime/ state+logs — so
# nothing is stranded. git mv would leave the gitignored runtime/ husk behind.
# The tracked half of the move is staged (git -C local add -A) but NOT committed;
# the operator or the vault-sync step commits.
#
# Usage:
#   migrate-fleet-to-system.sh <fleet> <system>
#   migrate-fleet-to-system.sh --rollback <fleet> <system>
#
# Sourceable: sourcing this file defines the mfs_* functions WITHOUT running
# main, so the file-operation core can be unit-tested in isolation.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

# --- small helpers -----------------------------------------------------------

_mfs_die() {
    echo "migrate-fleet-to-system: $*" >&2
    return 1
}

# _mfs_device <path>
# Device id of the nearest existing ancestor of <path>. A not-yet-created target
# thus compares by the parent dir that mkdir -p will create it in. Portable stat.
_mfs_device() {
    local p="${1:?Usage: _mfs_device <path>}"
    while [ ! -e "$p" ]; do p="$(dirname "$p")"; done
    if [ "$_OS" = "Darwin" ]; then stat -f %d "$p"; else stat -c %d "$p"; fi
}

# _mfs_lock_path <root> <fleet>
# Stable cross-process lock path, keyed on root+fleet, in the system temp dir —
# never inside local/ (where git -C local add -A would otherwise stage it).
_mfs_lock_path() {
    local root="$1" fleet="$2"
    printf '%s/claudlobby-mfs-%s.lock' "${TMPDIR:-/tmp}" \
        "$(printf '%s' "$root/$fleet" | tr '/ ' '__')"
}

# _mfs_ensure_marker <sysdir>
# Create the interim nested-system marker (idempotent). F8 later replaces it
# with a system.yaml; presence marks this dir as a vault system container.
_mfs_ensure_marker() {
    local sysdir="${1:?Usage: _mfs_ensure_marker <sysdir>}"
    local marker="$sysdir/.claudron-system"
    [ -f "$marker" ] && return 0
    mkdir -p "$sysdir"
    {
        printf '%s\n' "# claudlobby nested-system container (interim marker, #602 P3)"
        printf '%s\n' "# F8 replaces this with system.yaml. Do not hand-edit."
    } > "$marker"
}

# _mfs_stage <root>
# Stage the tracked move in the vault git repo — do NOT commit. Best-effort:
# a non-repo local/ (or a git that rejects) is benign and must not fail the move.
_mfs_stage() {
    local root="${1:?Usage: _mfs_stage <root>}"
    git -C "$root/local" add -A >/dev/null 2>&1 || true
}

# _mfs_system_has_fleets <sysdir>
# Return 0 if <sysdir> still holds at least one nested fleet (a subdir carrying a
# fleet.yaml), else 1. Drives rollback cleanup of an emptied system container.
_mfs_system_has_fleets() {
    local sysdir="${1:?Usage: _mfs_system_has_fleets <sysdir>}" d
    for d in "$sysdir"/*/; do
        if [ -f "$d/fleet.yaml" ]; then
            return 0
        fi
    done
    return 1
}

# --- preflight ---------------------------------------------------------------

# mfs_preflight <fleet> <system>
# Assert the fleet is a flat-migration candidate today and that the move will be
# an atomic same-fs rename. Returns nonzero with a clear message on any failure.
# Read-only: makes no filesystem changes.
mfs_preflight() {
    local fleet="${1:?Usage: mfs_preflight <fleet> <system>}"
    local system="${2:?Usage: mfs_preflight <fleet> <system>}"
    local root="${CLAUDLOBBY_ROOT:?}"
    local flat="$root/local/$fleet"
    local sysdir="$root/local/$system"

    # The fleet MUST resolve FLAT today (resolve_fleet_dir returns local/<fleet>
    # first when it exists). A nested or missing result means this is not a
    # flat-migration candidate — likely already migrated.
    local resolved
    if ! resolved="$(resolve_fleet_dir "$fleet")"; then
        _mfs_die "fleet '$fleet' does not resolve (no local/$fleet, and no unique nested fleet.yaml)"
        return 1
    fi
    if [ "$resolved" != "$flat" ]; then
        _mfs_die "fleet '$fleet' is not FLAT today (resolves to '$resolved', expected '$flat') — already migrated?"
        return 1
    fi
    if [ ! -f "$flat/fleet.yaml" ]; then
        _mfs_die "no fleet.yaml under '$flat' — refusing to migrate a non-fleet dir"
        return 1
    fi

    # Same-filesystem: the mv must be an atomic rename, never a cross-fs copy.
    # Compare local/<fleet> against local/<system> (or, before it exists, the
    # nearest existing ancestor — the local/ parent that will contain it).
    local dev_src dev_sys
    dev_src="$(_mfs_device "$flat")"
    dev_sys="$(_mfs_device "$sysdir")"
    if [ "$dev_src" != "$dev_sys" ]; then
        _mfs_die "local/$fleet (dev $dev_src) and local/$system (dev $dev_sys) are on DIFFERENT filesystems — mv would be a non-atomic cross-fs copy; aborting"
        return 1
    fi

    # A pre-existing nested destination is a conflict — never overwrite.
    if [ -e "$sysdir/$fleet" ]; then
        _mfs_die "destination '$sysdir/$fleet' already exists — refusing to overwrite"
        return 1
    fi
    return 0
}

# --- the risky file-op: atomic, complete, reversible -------------------------

# mfs_move_dir <fleet> <system>
# mkdir the system container, drop the interim marker, then move the flat fleet
# under it in ONE atomic same-fs rename (all content, tracked + gitignored),
# and stage the tracked half. Idempotent-safe. with_lock serializes the move.
mfs_move_dir() {
    local fleet="${1:?Usage: mfs_move_dir <fleet> <system>}"
    local system="${2:?Usage: mfs_move_dir <fleet> <system>}"
    local root="${CLAUDLOBBY_ROOT:?}"
    local src="$root/local/$fleet"
    local sysdir="$root/local/$system"
    local dst="$sysdir/$fleet"

    # Idempotent-safe: already nested (src gone, dst present) is success.
    if [ ! -d "$src" ] && [ -d "$dst" ]; then
        _mfs_ensure_marker "$sysdir"
        _mfs_stage "$root"
        echo "migrate-fleet-to-system: already nested at '$dst' — no-op"
        return 0
    fi
    if [ ! -d "$src" ]; then
        _mfs_die "source '$src' not found — nothing to move"
        return 1
    fi
    if [ -e "$dst" ]; then
        _mfs_die "destination '$dst' already exists — refusing to overwrite"
        return 1
    fi

    local lock
    lock="$(_mfs_lock_path "$root" "$fleet")"
    with_lock "$lock" _mfs_move_locked "$src" "$sysdir" "$dst" "$root"
}

# _mfs_move_locked <src> <sysdir> <dst> <root>  (runs inside with_lock)
_mfs_move_locked() {
    local src="$1" sysdir="$2" dst="$3" root="$4"
    mkdir -p "$sysdir"
    _mfs_ensure_marker "$sysdir"
    # PLAIN mv (not git mv): one atomic same-fs rename moves the tracked tree AND
    # the gitignored runtime/ state+logs together — no husk left behind.
    mv "$src" "$dst"
    _mfs_stage "$root"
}

# mfs_rollback_dir <fleet> <system>
# Exact inverse of mfs_move_dir: move the nested fleet back to the flat location,
# stage, and remove the system container + marker only if it is now empty of
# fleets. Restores the flat layout exactly. Idempotent-safe.
mfs_rollback_dir() {
    local fleet="${1:?Usage: mfs_rollback_dir <fleet> <system>}"
    local system="${2:?Usage: mfs_rollback_dir <fleet> <system>}"
    local root="${CLAUDLOBBY_ROOT:?}"
    local flat="$root/local/$fleet"
    local sysdir="$root/local/$system"
    local dst="$sysdir/$fleet"

    # Idempotent-safe: already flat (dst gone, flat present) is success.
    if [ ! -d "$dst" ] && [ -d "$flat" ]; then
        _mfs_stage "$root"
        echo "migrate-fleet-to-system: already flat at '$flat' — no-op"
        return 0
    fi
    if [ ! -d "$dst" ]; then
        _mfs_die "nested source '$dst' not found — nothing to roll back"
        return 1
    fi
    if [ -e "$flat" ]; then
        _mfs_die "flat destination '$flat' already exists — refusing to overwrite"
        return 1
    fi

    local lock
    lock="$(_mfs_lock_path "$root" "$fleet")"
    with_lock "$lock" _mfs_rollback_locked "$dst" "$flat" "$sysdir" "$root"
}

# _mfs_rollback_locked <dst> <flat> <sysdir> <root>  (runs inside with_lock)
_mfs_rollback_locked() {
    local dst="$1" flat="$2" sysdir="$3" root="$4"
    mv "$dst" "$flat"
    # Leave the container + marker for any OTHER fleets it still holds; remove
    # both only when it is empty of fleets, so the flat layout is restored exactly.
    if ! _mfs_system_has_fleets "$sysdir"; then
        rm -f "$sysdir/.claudron-system" 2>/dev/null || true
        rmdir "$sysdir" 2>/dev/null || true
    fi
    _mfs_stage "$root"
}

# --- per-bot supervision teardown / re-enroll --------------------------------

# mfs_stop_bots <fleet>
# For each bot in the fleet: graceful handoff, then stop + disable + UNLINK the
# installed unit (the B3 unlink), then tear down the bot private tmux server.
# Every step is guarded so a bot with no installed unit / empty BOT_SERVICE is a
# safe no-op — the tool must not crash on a bot that is not really enrolled.
# Resolves paths fresh, so pre-move it sees the flat layout and post-move (in a
# rollback) it sees the nested one.
mfs_stop_bots() {
    local fleet="${1:?Usage: mfs_stop_bots <fleet>}"
    local fleet_dir fleet_yaml bots_dir uid
    fleet_dir="$(resolve_fleet_dir "$fleet")" || fleet_dir="$CLAUDLOBBY_ROOT/local/$fleet"
    fleet_yaml="$fleet_dir/fleet.yaml"
    bots_dir="$(resolve_bots_dir "$fleet")"
    uid="$(id -u)"

    local b bot_dir svc sock
    while IFS= read -r b; do
        [ -z "$b" ] && continue
        bot_dir="$bots_dir/$b"
        if [ ! -d "$bot_dir" ]; then
            echo "  stop: $b — no runtime dir at $bot_dir, skipping" >&2
            continue
        fi

        # Graceful context capture before teardown (best-effort, never blocks).
        "$LIB_DIR/pre-stop-handoff.sh" "$bot_dir" >/dev/null 2>&1 || true

        svc="$(bot_conf_get "$bot_dir" BOT_SERVICE "")"
        if [ -z "$svc" ]; then
            echo "  stop: $b — empty BOT_SERVICE (not enrolled), skipping unit teardown" >&2
        else
            case "$_OS" in
            Linux)
                systemctl --user stop "$svc.service" 2>/dev/null || true
                systemctl --user disable "$svc.service" 2>/dev/null || true
                rm -f "$HOME/.config/systemd/user/$svc.service"
                systemctl --user daemon-reload 2>/dev/null || true
                ;;
            Darwin)
                /bin/launchctl bootout "gui/$uid/$svc" 2>/dev/null || true
                rm -f "$HOME/Library/LaunchAgents/$svc.plist"
                ;;
            esac
        fi

        # Tear down the bot private tmux server (best-effort — a missing server
        # is benign). Resolved from bot identity via the shared SSOT helper.
        sock="$(tmux_socket_for_bot "$bot_dir" 2>/dev/null || true)"
        if [ -n "$sock" ]; then
            bot_tmux "$sock" kill-server 2>/dev/null || true
        fi
    done <<< "$(parse_fleet_bots "$fleet_yaml")"
    return 0
}

# mfs_reenroll_bots <fleet>
# Re-enroll every bot via install-bot-systemd.sh / install-bot.sh — the
# unconditional cp of the freshly composed unit (which now carries the new
# nested WorkingDirectory). NOT spin-up-bot.sh: a restart-only path would keep
# the stale pre-move WorkingDirectory (the B3 blocker). install-bot*.sh both
# copy AND start (enable --now / bootstrap). Assumes `claudlobby generate` has
# already run at the new path. Guarded so an un-enrollable bot is skipped, not
# fatal; a real installer failure is surfaced (nonzero return) but does not abort
# the remaining bots.
mfs_reenroll_bots() {
    local fleet="${1:?Usage: mfs_reenroll_bots <fleet>}"
    local fleet_dir fleet_yaml bots_dir installer
    fleet_dir="$(resolve_fleet_dir "$fleet")" || fleet_dir="$CLAUDLOBBY_ROOT/local/$fleet"
    fleet_yaml="$fleet_dir/fleet.yaml"
    bots_dir="$(resolve_bots_dir "$fleet")"

    case "$_OS" in
    Linux)  installer="$LIB_DIR/install-bot-systemd.sh" ;;
    Darwin) installer="$LIB_DIR/install-bot.sh" ;;
    *) _mfs_die "unsupported OS '$_OS'"; return 1 ;;
    esac

    local b bot_dir svc rc=0
    while IFS= read -r b; do
        [ -z "$b" ] && continue
        bot_dir="$bots_dir/$b"
        if [ ! -d "$bot_dir" ]; then
            echo "  reenroll: $b — no runtime dir at $bot_dir (run 'claudlobby generate'), skipping" >&2
            continue
        fi
        svc="$(bot_conf_get "$bot_dir" BOT_SERVICE "")"
        if [ -z "$svc" ]; then
            echo "  reenroll: $b — empty BOT_SERVICE (not a supervised bot), skipping" >&2
            continue
        fi
        if ! "$installer" "$bot_dir"; then
            echo "  reenroll: $b — $installer failed" >&2
            rc=1
        fi
    done <<< "$(parse_fleet_bots "$fleet_yaml")"
    return "$rc"
}

# --- verify ------------------------------------------------------------------

# mfs_verify <fleet>
# Reconcile the fleet and assert health: no orphan and no missing bots.
# reconcile-fleet.sh exits 0 regardless of bucket contents, so we parse its
# report rather than trust its exit code. On failure, emit a loud fleet alert
# and return nonzero (the caller must NOT proceed).
mfs_verify() {
    local fleet="${1:?Usage: mfs_verify <fleet>}"
    local bots_dir out orphan_line missing_line ok
    bots_dir="$(resolve_bots_dir "$fleet")"

    out="$("$LIB_DIR/reconcile-fleet.sh" "$fleet" 2>&1)" || true
    printf '%s\n' "$out"

    orphan_line="$(printf '%s\n' "$out" | grep 'orphan:' || true)"
    missing_line="$(printf '%s\n' "$out" | grep 'missing:' || true)"

    ok=1
    # Must positively see the healthy shape: both lines present AND (none).
    [ -n "$orphan_line" ] || ok=0
    [ -n "$missing_line" ] || ok=0
    case "$orphan_line" in *'(none)'*) ;; *) ok=0 ;; esac
    case "$missing_line" in *'(none)'*) ;; *) ok=0 ;; esac

    if [ "$ok" -ne 1 ]; then
        emit_failure_alert "$bots_dir" "migrate_verify_failed" \
            "fleet '$fleet' migration verify FAILED — reconcile shows orphan/missing bots; do NOT proceed, inspect and roll back" || true
        _mfs_die "verify failed for fleet '$fleet' — see reconcile output above"
        return 1
    fi
    echo "migrate-fleet-to-system: verify OK — fleet '$fleet' healthy (no orphan/missing)"
    return 0
}

# --- orchestration -----------------------------------------------------------

# _mfs_generate <fleet>
# Single recompose at the CURRENT path — install-bot*.sh then copies the freshly
# composed unit whose WorkingDirectory now points at the resolved location.
# --root pins the compositor to the same root the tool is mutating (rather than
# the auto-detected package root), so the generate lands in this vault.
_mfs_generate() {
    local fleet="${1:?Usage: _mfs_generate <fleet>}"
    if ! claudlobby --root "$CLAUDLOBBY_ROOT" --fleet "$fleet" generate; then
        _mfs_die "claudlobby --fleet $fleet generate failed"
        return 1
    fi
}

# main [--rollback] <fleet> <system>
# forward:  preflight -> stop -> move -> regenerate -> re-enroll -> verify.
# rollback: stop -> rollback-move -> regenerate -> re-enroll -> verify.
# On ANY failure it STOPS with a clear, recoverable message — never a silent
# half-migration.
main() {
    install_error_trap ""

    case "${1:-}" in
        -h|--help) show_help "${BASH_SOURCE[0]}"; return 0 ;;
    esac

    local mode="forward"
    if [ "${1:-}" = "--rollback" ]; then mode="rollback"; shift; fi
    local fleet="${1:?Usage: migrate-fleet-to-system.sh [--rollback] <fleet> <system>}"
    local system="${2:?Usage: migrate-fleet-to-system.sh [--rollback] <fleet> <system>}"

    if [ "$mode" = "forward" ]; then
        echo "== migrate-fleet-to-system: $fleet -> $system/$fleet =="
        if ! mfs_preflight "$fleet" "$system"; then
            _mfs_die "preflight failed — nothing changed"
            return 1
        fi
        mfs_stop_bots "$fleet" || true
        if ! mfs_move_dir "$fleet" "$system"; then
            _mfs_die "MOVE FAILED — bots are stopped; inspect local/$fleet and local/$system/$fleet before retrying"
            return 1
        fi
        if ! _mfs_generate "$fleet"; then
            _mfs_die "generate failed AFTER move — fleet is nested at local/$system/$fleet but not regenerated. Fix and re-run, or roll back: $0 --rollback $fleet $system"
            return 1
        fi
        if ! mfs_reenroll_bots "$fleet"; then
            _mfs_die "re-enroll failed AFTER move+generate — some bots not supervised. Re-run install-bot for the failed bot(s), or roll back: $0 --rollback $fleet $system"
            return 1
        fi
        mfs_verify "$fleet" || return 1
        echo "== DONE: $fleet migrated to $system/$fleet and re-enrolled =="
        return 0
    fi

    echo "== ROLLBACK: $system/$fleet -> $fleet =="
    mfs_stop_bots "$fleet" || true
    if ! mfs_rollback_dir "$fleet" "$system"; then
        _mfs_die "ROLLBACK MOVE FAILED — inspect local/$system/$fleet and local/$fleet"
        return 1
    fi
    if ! _mfs_generate "$fleet"; then
        _mfs_die "generate failed AFTER rollback — fleet is flat at local/$fleet but not regenerated; fix and re-run"
        return 1
    fi
    if ! mfs_reenroll_bots "$fleet"; then
        _mfs_die "re-enroll failed AFTER rollback — some bots not supervised; re-run install-bot for the failed bot(s)"
        return 1
    fi
    mfs_verify "$fleet" || return 1
    echo "== DONE: rolled back $fleet to flat layout =="
    return 0
}

# Run main only when executed, not when sourced (tests source for the mfs_* fns).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
