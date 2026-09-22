#!/bin/bash
# lib/boot-admission.sh — the boot admission gate (#1573, PR B).
#
# THIS REPLACES the #304 host-wide boot lock that lived at the top of
# lib/start-bot.sh: a single fleet-wide mkdir lock, a fixed 8s hold, no
# ordering, a 60s age-based force-claim that a forward clock step turns into a
# live-lock steal, and a 120s flat cap. The gate is that lock done properly --
# N slots instead of one, managers first instead of arrival order, a real
# dispersed wait cap, and a reaper instead of an age-based force-claim. It is
# not a protocol BESIDE the lock; the lock is deleted in the same commit.
#
# Sourced by lib-common.sh immediately after lib/supervisor.sh, through the
# same forkless _LIB_COMMON_DIR idiom, so svc_unit_name and $_OS are already
# in scope. Every helper below is a function: this file does nothing at source
# time but define constants, so a test may source it on its own.
#
#   boot_admission_acquire <bot_dir>  — prints granted:<slot> | timeout |
#                                       unavailable | disabled on STDOUT ONLY;
#                                       rc 0 always.
#   boot_admission_release <bot_dir>  — removes this bot slot, its ticket and
#                                       its marker; idempotent; logs WHICH slot
#                                       it freed, or that it freed nothing.
#   _boot_admission_reap [bots_dir]   — slots whose holder is dead or past the
#                       [keep_tree]     GRANT budget; tickets and markers whose
#                                       owner is dead or has stopped polling;
#                                       trees under any other boot id. Never
#                                       <keep_tree>, the caller own live tree.
#
# THE GATE NEVER BLOCKS INDEFINITELY AND NEVER FAILS CLOSED. Every failure
# path -- unwritable state dir, unresolvable epoch, empty unit name, cap
# reached -- proceeds, logs, and where it is a verdict, events. A gate that
# could strand a bot forever would be the failure it replaces, not a fix.
#
# STATE lives under $CLAUDLOBBY_ROOT/state/boot/<boot-id>/{tickets,slots},
# keyed on a BOOT ID that is PINNED ONCE per boot in state/boot/.boot-id and
# never re-resolved by a reader -- see "the boot key" below for why the clock
# cannot be that key. A tree under any OTHER boot id is stale UNCONDITIONALLY,
# never by reading a pid that a reboot has made meaningless. With no key at all
# the gate falls back to the un-keyed path with a log line, as plugin_ensure
# does.
#
# TWO HORIZONS, because there are two phases. A SLOT is a GRANT and is aged
# against BOOT_HOLD_CEILING_S (ready_timeout_s + 120), the budget for the
# bring-up a holder is doing while it cannot poll. A TICKET and a MARKER belong
# to a bot that is still WAITING, whose budget is BOOT_ADMISSION_WAIT_MAX_S --
# by construction up to 21x longer. Ageing those against the grant budget
# deleted a live waiter's records for most of its wait: managers-first collapsed
# after one ceiling, and the .boot-queued marker vanished mid-queue, re-opening
# the #1002 window the marker exists to close. So a waiter RE-WRITES its ticket
# and its marker on every poll, fork-free, and the same alive-AND-fresh pair
# then means "alive AND STILL POLLING" for them -- strictly stronger than either
# half, one constant rather than two, and the ceiling stays correctly sized for
# the granted phase, which nothing refreshes.
#
# TAKE is `mkdir slots/<n>` -- the atomic mutex. `mv` is NOT one: `mv src dst`
# where dst is an existing directory moves src INSIDE it rather than failing
# (measured, R0). RECLAIM is one atomic rename to a name that cannot already
# exist -- mktemp -d makes the holder, the slot moves to <holder>/slot -- so
# exactly one racer wins and every loser sees its source already gone. Never
# rmdir + mkdir.
#
# LIVENESS is `kill -0 <pid>` PAIRED WITH the FRESHNESS of the file that
# records the pid (marker_age_within's arithmetic, inlined so the reaper takes
# the clock once). kill -0 alone is NOT sufficient: a RE-USED pid blocked a
# bot indefinitely and was proven live within minutes of deploy (#1425,
# lib/keepalive.sh:88-99, the remedy this estate already ships). A slot past
# the hold ceiling is reclaimed regardless of pid state, and a pid-less slot
# inside the claim grace is HELD rather than reaped -- "I cannot see the
# holder" must never license a delete (#1146 direction; claudlobby/
# source_state.py has the rule).
#
# LOG is owned by the CALL SITE, which sets it and runs setup_log_dir before
# calling in. This file writes to ${LOG:-/dev/null} defensively and never
# assigns LOG itself. ONLY THE VERDICT REACHES STDOUT, because the caller
# captures it in a command substitution (R17).
#
# NOT OPERATOR KNOBS. BOOT_ADMISSION_CLAIM_GRACE_S is a compose-time constant;
# BOOT_ADMISSION_POLL_S and BOOT_ADMISSION_CALLER_CAP_S are lib/-internal call
# conventions (the harness runs at sub-second quanta; the serial restart
# drivers scope the cap per bot). PR B task 4 documents all three in
# documentation/environment-variables.md AS not-knobs -- which is the state
# they are in until that task lands, not the state they are in now.
#
# BOOT_ADMISSION_DISABLED=1 is a TEMPORARY rollback carrier (F17), read from
# the sourced env -- a fleet composed `env:` or its bot.conf. It prints
# `disabled`, logs one line, and mints no ticket, no marker and no slot. PR C
# removes it; it is not a knob and must not grow one.
#
# bash 3.2 safe (tests/test_bash_parse.py covers lib/): no apostrophes inside
# $( ), printf %s for values, every variable quoted.

# --- compose-time constants and the un-regenerated bot.conf fallbacks -------
# An un-regenerated bot.conf has NONE of the BOOT_* keys at all -- a third
# state beside empty, non-numeric and zero, and the one "no operator step"
# actually depends on. Every read below is ${KEY:-<default>} so such a fleet
# degrades silently to a one-slot gate with todays timings rather than
# aborting under set -u.
_BOOT_ADMISSION_CLAIM_GRACE_DEFAULT_S=10
_BOOT_ADMISSION_POLL_DEFAULT_S=2
_BOOT_ADMISSION_SLOTS_DEFAULT=1
_BOOT_ADMISSION_WAIT_MAX_DEFAULT_S=1200
# The F4 shape, twice over: lib/start-bot.sh already names 90 by hand as the
# RC_READY_TIMEOUT_S fallback (claudlobby/boot.py READY_TIMEOUT_FLOOR_S), and
# 210 is that floor plus the 120s margin lib/rolling-restart.sh:86-104 already
# derives and enumerates.
_BOOT_READY_TIMEOUT_DEFAULT_S=90
_BOOT_HOLD_CEILING_DEFAULT_S=210
# The wall-clock bound on a backgrounded metric emit, and the same number
# lib/keepalive.sh bounds its own emit with. See _boot_admission_metric.
_BOOT_ADMISSION_EMIT_TIMEOUT_DEFAULT_S=110

# --- internals --------------------------------------------------------------

# --- fork-free file reads ---------------------------------------------------
# These SET GLOBALS rather than printing, and that is the whole point: a command
# substitution is itself a fork, so a helper that printed its answer would cost
# exactly what the `sed | head` it replaces cost. The reaper runs at the top of
# every poll of EVERY waiter, so this is 21 tickets + 21 markers per waiter
# every BOOT_ADMISSION_POLL_S on a 21-bot host. MEASURED on a 21-ticket /
# 21-marker / 1-slot queue, counting every exec through a PATH shim: 213 per
# reap before this (63 sed, 64 head, 43 stat, 43 date) at 360-454 ms, against
# 44 (43 stat, 1 date) at 127-142 ms after -- three reps each, M-series Mac,
# spent during precisely the window the gate exists to de-contend.
_BA_LINE=""
_BA_PID=""
_BA_EPOCH=""

# _boot_admission_read_first <file> -> _BA_LINE (empty when absent or empty)
_boot_admission_read_first() {
    _BA_LINE=""
    [ -f "${1:-}" ] || return 0
    IFS= read -r _BA_LINE < "$1" 2>/dev/null || true
    return 0
}

# _boot_admission_read_kv <file> -> _BA_PID, _BA_EPOCH (both empty when absent)
_boot_admission_read_kv() {
    local line
    _BA_PID=""
    _BA_EPOCH=""
    [ -f "${1:-}" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            pid=*) _BA_PID="${line#pid=}" ;;
            epoch=*) _BA_EPOCH="${line#epoch=}" ;;
        esac
    done < "$1" 2>/dev/null || true
    return 0
}

# Everything the gate says goes to the call sites LOG. Never stdout: the
# caller captures stdout and a stray line there becomes a verdict (R17).
_boot_admission_log() {
    printf '%s %s\n' "$(ts_iso)" "$*" >> "${LOG:-/dev/null}" 2>/dev/null || true
    return 0
}

# ONE ticket-stamp format, not a disjunction: `date +%s%N` when it answers 19
# digits, else `printf %010d%09d <seconds> 0` -- the same quantity at second
# resolution, so the fallback sorts WITH real ns stamps rather than before
# every one of them (R2). macOS `date` has no %N and prints a literal trailing
# N, which the all-digits guard rejects.
_boot_admission_stamp() {
    local s sec
    s="$(date +%s%N 2>/dev/null || true)"
    case "$s" in
        ''|*[!0-9]*) s="" ;;
        *) if [ "${#s}" -ne 19 ]; then s=""; fi ;;
    esac
    if [ -z "$s" ]; then
        sec="$(date +%s 2>/dev/null || true)"
        case "$sec" in ''|*[!0-9]*) sec=0 ;; esac
        s="$(printf '%010d%09d' "$sec" 0)"
    fi
    printf '%s' "$s"
}

# clamp((ncpu or 1) // 4, 1, 4) -- byte-for-byte the clamp
# claudlobby.boot.derive_slots applies, so `BOOT_ADMISSION_SLOTS=auto` resolves
# to the same number whether the composer or the host derives it.
# tests/test_boot_policy.py feeds both implementations the same cpu counts.
_boot_admission_derive_slots() {
    local n="${1:-}" q
    case "$n" in ''|*[!0-9]*) n=1 ;; esac
    if [ "$n" -lt 1 ]; then n=1; fi
    q=$(( n / 4 ))
    if [ "$q" -lt 1 ]; then q=1; fi
    if [ "$q" -gt 4 ]; then q=4; fi
    printf '%s' "$q"
}

# The host CPU count, on both OSes. getconf answers on Linux and on macOS;
# sysctl is the Darwin fallback for a host whose getconf does not.
_boot_admission_ncpu() {
    local n=""
    n="$(getconf _NPROCESSORS_ONLN 2>/dev/null || true)"
    case "$n" in ''|*[!0-9]*) n="" ;; esac
    if [ -z "$n" ]; then
        n="$(sysctl -n hw.ncpu 2>/dev/null || true)"
        case "$n" in ''|*[!0-9]*) n="" ;; esac
    fi
    printf '%s' "$n"
}

# PR A clock-step fold, verbatim (lib/lib-common.sh:1293-1298): a tick whose
# delta is negative or exceeds <step_s> shifts the ORIGIN instead of counting.
# The RTC-less primary host steps its clock forward by the whole downtime once
# NTP syncs after boot, which is exactly when the gate is busiest; a flat
# subtraction would expire every waiter cap in the same instant.
_boot_admission_fold() {
    local now="$1" last="$2" started="$3" step="$4" delta
    delta=$(( now - last ))
    if [ "$delta" -lt 0 ] || [ "$delta" -gt "$step" ]; then
        started=$(( started + delta ))
    fi
    printf '%s' "$started"
}

# effective cap = cap + arrival_rank x (ready_timeout_s / slots).
# Every ticket at a cold boot is minted within the same second, so a FLAT cap
# expires them all in the same second and reconstitutes the storm the gate
# replaces. Dispersion degrades one bot at a time. A cap of 0 means NEVER WAIT
# and stays 0 at every rank -- dispersing it would turn the one setting that
# promises not to wait into the longest wait on the host.
_boot_admission_effective_cap() {
    local cap="${1:-}" rank="${2:-}" ready="${3:-}" slots="${4:-}" disp
    case "$cap" in ''|*[!0-9]*) cap=$_BOOT_ADMISSION_WAIT_MAX_DEFAULT_S ;; esac
    case "$rank" in ''|*[!0-9]*) rank=0 ;; esac
    case "$ready" in ''|*[!0-9]*) ready=$_BOOT_READY_TIMEOUT_DEFAULT_S ;; esac
    case "$slots" in ''|*[!0-9]*) slots=1 ;; esac
    if [ "$slots" -lt 1 ]; then slots=1; fi
    if [ "$cap" -eq 0 ]; then printf '0'; return 0; fi
    disp=$(( ready / slots ))
    if [ "$disp" -lt 1 ]; then disp=1; fi
    printf '%s' "$(( cap + rank * disp ))"
}

# --- the boot key: one id per boot, pinned once -----------------------------
#
# THE TREE IS KEYED ON A BOOT ID, NOT ON THE CLOCK. resolve_boot_epoch is not
# stable across a clock step on either OS -- the Linux rung is `uptime -s`
# (now minus uptime), the macOS rung is kern.boottime, which settimeofday
# adjusts, and the third is literally `date +%s` minus /proc/uptime. This
# estate's primary host is RTC-less and opens a stale-clock window at EVERY
# boot, i.e. exactly when the gate is busiest. An epoch-keyed tree therefore
# moved UNDER a live waiter: acquire bound the path once, the reaper re-resolved
# it every poll, and the first poll after the step rm -rf'd the tree the waiter
# was queued in -- stranding it for its whole cap (112 minutes on the 21-bot
# host, then proceeding ungated: worse than having no gate) and wiping every
# marker on the host at the same instant.
#
# A boot id does not move: kern.bootsessionuuid on Darwin (through sysctl_bin,
# because a launchd unit's composed PATH has no /usr/sbin and a bare `sysctl`
# silently answers nothing there), /proc/sys/kernel/random/boot_id on Linux, and
# resolve_boot_epoch as the LAST rung for a host with neither -- which keeps
# today's naming on such a host rather than inventing a second scheme for it.
#
# It is PINNED, and the pin is what every reader consults: R14's premise ("a
# different key means a reboot happened") is then true BY CONSTRUCTION rather
# than by assumption, and the unconditional sweep it licenses is safe again.
# CLAUDLOBBY_BOOT_ID is the test seam, CLAUDLOBBY_BOOT_EPOCH's sibling.
_BA_KEY=""
_BA_EPOCH_PIN=""

# A boot id becomes a PATH COMPONENT, so its shape is validated before it is
# used as one: hex digits and dashes only -- a UUID from either OS source, or
# the all-digit epoch fallback -- non-empty, not leading with a dash, bounded in
# length. A `.` is deliberately outside that set, which is also what keeps
# `plugins.lock` and the `plugins-updated.<epoch>.<plugin>` stamps out of the
# reaper's candidate set, and `admission-noepoch` with it.
_boot_admission_id_ok() {
    local s="${1:-}"
    case "$s" in
        ''|-*|*[!0-9A-Fa-f-]*) return 1 ;;
    esac
    [ "${#s}" -le 64 ] || return 1
    return 0
}

# This host's live boot id, validated, or empty. /proc is probed by READABILITY
# rather than by $_OS so a test that forces _OS still gets the real answer.
_boot_admission_boot_id() {
    local id="" bin
    if [ -n "${CLAUDLOBBY_BOOT_ID:-}" ]; then
        id="$CLAUDLOBBY_BOOT_ID"
    elif [ -r /proc/sys/kernel/random/boot_id ]; then
        IFS= read -r id < /proc/sys/kernel/random/boot_id 2>/dev/null || true
    else
        bin="$(sysctl_bin 2>/dev/null || true)"
        if [ -n "$bin" ]; then
            id="$("$bin" -n kern.bootsessionuuid 2>/dev/null || true)"
        fi
    fi
    if ! _boot_admission_id_ok "$id"; then
        id="$(resolve_boot_epoch 2>/dev/null || true)"
        _boot_admission_id_ok "$id" || id=""
    fi
    printf '%s' "$id"
}

# READ-ONLY: the pin as it stands, with NO resolution and NO write. This is the
# door the reaper and the release use, and the read-only-ness is the fix: a
# reader that re-resolved anything is the defect the pin exists to close.
# Sets _BA_KEY and _BA_EPOCH_PIN; both empty when there is no usable pin.
_boot_admission_read_pin() {
    local f="${1:-}/.boot-id" line
    _BA_KEY=""
    _BA_EPOCH_PIN=""
    [ -f "$f" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            id=*) _BA_KEY="${line#id=}" ;;
            epoch=*) _BA_EPOCH_PIN="${line#epoch=}" ;;
        esac
    done < "$f" 2>/dev/null || true
    if ! _boot_admission_id_ok "$_BA_KEY"; then
        _BA_KEY=""
        _BA_EPOCH_PIN=""
        return 0
    fi
    case "$_BA_EPOCH_PIN" in ''|*[!0-9]*) _BA_EPOCH_PIN="" ;; esac
    return 0
}

# The pin write: a temp name and a `mv` into place -- the slots.max idiom --
# under with_lock, so concurrent FIRST writers produce ONE pin rather than one
# each. Reached only when the pin is absent or names a different boot, i.e.
# once per host per boot, never once per acquire.
_boot_admission_write_pin() {
    local root="${1:-}" id="${2:-}" epoch="${3:-}" tmp
    tmp="$root/.boot-id.$$.tmp"
    if printf 'id=%s\nepoch=%s\n' "$id" "$epoch" > "$tmp" 2>/dev/null; then
        mv "$tmp" "$root/.boot-id" 2>/dev/null || true
    fi
    rm -f "$tmp" 2>/dev/null || true
    return 0
}

# The ACQUIRE-time door: resolve the live boot id once, adopt the pin when it
# names the same boot, REPLACE it when it does not (a reboot happened, and every
# other tree is stale by construction). Sets _BA_KEY and _BA_EPOCH_PIN.
_boot_admission_pin() {
    local root="${1:-}" live prev_wl
    live="$(_boot_admission_boot_id)"
    mkdir -p "$root" 2>/dev/null || true
    _boot_admission_read_pin "$root"
    if [ -n "$_BA_KEY" ] && [ "$_BA_KEY" = "$live" ]; then
        return 0
    fi
    # TWO BOUNDS ON THE LOCK, because with_lock's mkdir spinlock proceeds
    # UNLOCKED after its budget rather than failing, and this is a boot path.
    # (1) An unwritable root can never hold the lock dir, so it would spin the
    # whole budget for a pin that cannot be written -- measured at 30s on the
    # unwritable-state-dir cell, i.e. 30s added to every boot on a host with a
    # misowned state dir, which is the opposite of "never blocks". (2) A stale
    # lock dir left by a SIGKILL inside the printf+mv would otherwise cost the
    # full budget on EVERY acquire, so the wait is scoped down for this call and
    # restored -- explicitly, because whether a prefix assignment survives a
    # FUNCTION call differs between bash's POSIX and default modes.
    if [ -w "$root" ]; then
        prev_wl="${WITH_LOCK_WAIT_S-__unset__}"
        WITH_LOCK_WAIT_S=5
        with_lock "$root/.boot-id.lock" _boot_admission_write_pin \
            "$root" "$live" "$(resolve_boot_epoch 2>/dev/null || true)" >/dev/null 2>&1 || true
        if [ "$prev_wl" = "__unset__" ]; then unset WITH_LOCK_WAIT_S; else WITH_LOCK_WAIT_S="$prev_wl"; fi
    fi
    # RE-READ and adopt whatever landed: that, not the write, is what makes
    # every waiter on the host agree about which tree is live.
    _boot_admission_read_pin "$root"
    if [ -z "$_BA_KEY" ]; then
        # An unwritable state dir. Keep the live values in memory so the path
        # below is still honest; _boot_admission_state_dir refuses next and the
        # verdict is `unavailable`.
        _BA_KEY="$live"
        _BA_EPOCH_PIN="$(resolve_boot_epoch 2>/dev/null || true)"
        case "$_BA_EPOCH_PIN" in ''|*[!0-9]*) _BA_EPOCH_PIN="" ;; esac
    fi
    return 0
}

# The state tree path for a boot key. Creates nothing -- the reaper must be able
# to ask where the tree is without bringing one into existence.
_boot_admission_state_path() {
    local e="${1:-}"
    if [ -n "$e" ]; then
        printf '%s' "$CLAUDLOBBY_ROOT/state/boot/$e"
    else
        printf '%s' "$CLAUDLOBBY_ROOT/state/boot/admission-noepoch"
    fi
}

# The state tree, created. Prints the path, or prints nothing and returns 1
# when it cannot be made or is not writable.
_boot_admission_state_dir() {
    local d
    d="$(_boot_admission_state_path "${1:-}")"
    if ! mkdir -p "$d/tickets" "$d/slots" 2>/dev/null || [ ! -w "$d" ]; then
        return 1
    fi
    printf '%s' "$d"
}

# ONE atomic rename to a name that cannot already exist (R5). mktemp -d makes
# the holder directory, so <holder>/slot is new by construction; two racers
# calling this at the same instant cannot both succeed, because rename(2) is
# atomic and the loser source is already gone. Returns 0 to the single winner.
_boot_admission_reclaim() {
    local s="${1:-}" d holder
    [ -d "$s" ] || return 1
    d="${s%/*}"
    holder="$(mktemp -d "$d/.reap.XXXXXXXX" 2>/dev/null || true)"
    [ -n "$holder" ] || return 1
    if mv "$s" "$holder/slot" 2>/dev/null; then
        rm -rf "$holder" 2>/dev/null || true
        return 0
    fi
    rm -rf "$holder" 2>/dev/null || true
    return 1
}

# THE LIVENESS PAIR, with the caller's <pid> and the caller's <now>.
#
# `kill -0` alone is NOT sufficient: a RE-USED pid blocked a bot indefinitely
# and was proven live within minutes of deploy (#1425, lib/keepalive.sh:88-99,
# the remedy this estate already ships). Freshness is the second half.
#
# Since PR B's fix round freshness carries a second meaning for the records a
# WAITER owns: a queued bot re-writes its ticket and its marker on every poll,
# so alive-AND-fresh reads as "alive AND STILL POLLING" there, while for a slot
# -- which nothing refreshes -- it reads as "granted, inside its budget". One
# predicate, two correct meanings, because the record is written by whoever the
# horizon is about.
#
# <pid> and <now> are PARAMETERS rather than reads so the reaper can take the
# clock once and the kv read once per file: this is called 43x per reap on a
# 21-bot host (21 tickets + 21 markers + 1 slot), and the predecessor pair it
# replaces spent one `date` fork inside marker_age_within on every one of those
# 43 calls -- 43 of the 213 execs measured above, all of them asking the same
# clock the same question. A clock step that makes the age negative reads as
# WITHIN -- the same fail-toward-HELD posture as everywhere else in this file.
_boot_admission_pid_fresh() {
    local p="${1:-}" f="${2:-}" ceiling="${3:-}" now="${4:-}" m
    case "$p" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$p" 2>/dev/null || return 1
    [ -e "$f" ] || return 1
    m="$(stat_mtime "$f" 2>/dev/null || true)"
    case "$m" in ''|*[!0-9]*) return 1 ;; esac
    [ "$(( now - m ))" -le "$ceiling" ]
}

# The age half on its own, for the pid-LESS slot directory: there is no pid to
# pair with, only the claim grace. marker_age_within refuses a directory
# ([ -f ]), hence the separate arithmetic; <now> is likewise the caller's.
_boot_admission_age_within() {
    local p="${1:-}" max="${2:-}" now="${3:-}" m
    [ -e "$p" ] || return 1
    m="$(stat_mtime "$p" 2>/dev/null || true)"
    case "$m" in ''|*[!0-9]*) return 1 ;; esac
    case "$now" in ''|*[!0-9]*) return 1 ;; esac
    [ "$(( now - m ))" -le "$max" ]
}

# The unit name is svc_unit_name, the ONE owner of that name. Sanitized for a
# filename the same way plugin_ensure sanitizes a plugin name.
_boot_admission_unit() {
    local u
    u="$(svc_unit_name "${1:-}" 2>/dev/null || true)"
    if [ -z "$u" ]; then printf ''; return 0; fi
    printf '%s' "$u" | tr -c 'A-Za-z0-9._-' '_'
}

# A ticket name is <priority: 1 digit>-<arrival: exactly 19 digits>-<unit>:
# 1 + 1 + 19 + 1 = 22 characters before the unit, in every ticket, always.
# That fixed width is the whole reason the name can be both a sort key and a
# parse; an off-by-one here reads `-<unit>` instead of `<unit>`, which silently
# matches no holder and retires no ticket at release.
_BOOT_ADMISSION_UNIT_OFFSET=22
_boot_admission_ticket_unit() {
    local n="${1:-}"
    printf '%s' "${n:$_BOOT_ADMISSION_UNIT_OFFSET}"
}

# A case glob over the newline-delimited list: no pipe, no fork and no file.
#
# No PIPE, because `printf | grep -q` is a pipefail trap -- grep -q exits on its
# first match, SIGPIPEs the writer, and under `set -o pipefail` the PIPELINE
# then reports failure on exactly the case that matched. The holders exclusion
# is what keeps the queue head moving, so a silently inverted answer here would
# strand every waiter behind a granted bot.
#
# No HERE-DOCUMENT either, which the pipe-free rewrite used: bash 3.2 backs
# every heredoc with a real temp file, and this runs once per queued ticket per
# poll per waiter. $holders is at most slots_max (1-4) lines, so a glob does the
# same job with neither.
#
# The needle is wrapped in newlines on BOTH sides so the match is whole-line. A
# bare *"$needle"* also matches a LONGER unit name that merely contains ours,
# which would exclude from the queue a bot that holds no slot.
_boot_admission_in_list() {
    local needle="${1:-}" nl='
'
    [ -n "$needle" ] || return 1
    case "$nl${2:-}$nl" in
        *"$nl$needle$nl"*) return 0 ;;
    esac
    return 1
}

# The unit names currently holding a slot.
_boot_admission_slot_units() {
    local d="${1:-}" sd
    [ -d "$d" ] || return 0
    for sd in "$d"/[0-9]*; do
        [ -d "$sd" ] || continue
        [ -f "$sd/unit" ] || continue
        _boot_admission_read_first "$sd/unit"
        printf '%s\n' "$_BA_LINE"
    done
    return 0
}

_boot_admission_held_count() {
    local d="${1:-}" n=0 sd
    if [ ! -d "$d" ]; then printf '0'; return 0; fi
    for sd in "$d"/[0-9]*; do
        [ -d "$sd" ] || continue
        n=$(( n + 1 ))
    done
    printf '%s' "$n"
}

_boot_admission_queue_len() {
    local d="${1:-}" n=0 f
    if [ ! -d "$d" ]; then printf '0'; return 0; fi
    for f in "$d"/*; do
        [ -f "$f" ] || continue
        n=$(( n + 1 ))
    done
    printf '%s' "$n"
}

# How many tickets sort before ours. With <holders> given, tickets whose unit
# already HOLDS a slot are skipped, which is what turns the sort into a queue
# rather than a permanent head: a granted bot keeps its ticket until release
# (one lifetime for both records), so without this the head would never move
# and a second slot could never be taken.
#
# A plain string compare, not `sort`: the name is fixed-width by construction
# -- one digit, a dash, nineteen digits, a dash -- so priority and arrival
# decide the order before the unit name is ever reached, and every waiter on
# the host compares the same way.
_boot_admission_count_before() {
    local d="${1:-}" me="${2:-}" holders="${3-}" n=0 f name
    if [ ! -d "$d" ]; then printf '0'; return 0; fi
    for f in "$d"/*; do
        [ -f "$f" ] || continue
        name="${f##*/}"
        if [ "$name" = "$me" ]; then continue; fi
        if [[ "$name" < "$me" ]]; then
            if [ -n "$holders" ] \
               && _boot_admission_in_list "$(_boot_admission_ticket_unit "$name")" "$holders"; then
                continue
            fi
            n=$(( n + 1 ))
        fi
    done
    printf '%s' "$n"
}

# `auto` is resolved on the host that runs the gate, with the composer own
# clamp, and the resolved count is written ONCE so every waiter on the host
# agrees about how many slots exist. Written to a temp name and moved into
# place; a waiter that finds the file reads it rather than re-deriving.
_boot_admission_slots_max() {
    local state="${1:-}" raw v f tmp
    raw="${BOOT_ADMISSION_SLOTS:-$_BOOT_ADMISSION_SLOTS_DEFAULT}"
    f="$state/slots.max"
    case "$raw" in
        auto)
            _boot_admission_read_first "$f"
            v="$_BA_LINE"
            case "$v" in ''|*[!0-9]*) v="" ;; esac
            if [ -z "$v" ]; then
                v="$(_boot_admission_derive_slots "$(_boot_admission_ncpu)")"
                tmp="$state/.slots.max.$$.$(_boot_admission_stamp)"
                if printf '%s\n' "$v" > "$tmp" 2>/dev/null; then
                    mv "$tmp" "$f" 2>/dev/null || true
                fi
                rm -f "$tmp" 2>/dev/null || true
                _boot_admission_read_first "$f"
                case "$_BA_LINE" in ''|*[!0-9]*) : ;; *) v="$_BA_LINE" ;; esac
            fi
            ;;
        ''|*[!0-9]*) v=$_BOOT_ADMISSION_SLOTS_DEFAULT ;;
        *) v="$raw" ;;
    esac
    if [ "$v" -lt 1 ]; then v=1; fi
    printf '%s' "$v"
}

# Take the lowest free slot. mkdir is the mutex; the pid lands immediately
# after, because the claim grace starts at the mkdir.
#
# THE PID WRITE IS VERIFIED, because the mkdir succeeding is not the same fact
# as holding the slot: a process descheduled past the claim grace between the
# two has its directory reclaimed under it, all three writes then fail silently,
# and reporting a grant anyway puts TWO holders on one slot. The window is
# microseconds against a 10s grace and it is R4's documented trade -- but it
# costs one `[ -s ]` to close, so it is closed. Moving to the next index rather
# than failing outright is deliberate: the slot we lost may well be free again,
# and the caller re-enters on its next poll either way.
_boot_admission_try_take() {
    local d="${1:-}" max="${2:-}" unit="${3:-}" i=0
    while [ "$i" -lt "$max" ]; do
        if mkdir "$d/$i" 2>/dev/null; then
            printf '%s\n' "$$" > "$d/$i/pid" 2>/dev/null || true
            printf '%s\n' "$unit" > "$d/$i/unit" 2>/dev/null || true
            printf '%s\n' "$(date +%s)" > "$d/$i/granted_at" 2>/dev/null || true
            if [ ! -s "$d/$i/pid" ]; then i=$(( i + 1 )); continue; fi
            printf '%s' "$i"
            return 0
        fi
        i=$(( i + 1 ))
    done
    return 1
}

_boot_admission_marker_path() {
    printf '%s' "${1:-}/data/.boot-queued"
}

# --- the plane doors --------------------------------------------------------

_boot_admission_event() {
    local etype="${1:-}" data="${2:-}" bot_dir="${3:-}"
    emit_fleet_event "$etype" "start-bot" "$data" "$bot_dir" "${BOT_ID:-}" || true
    return 0
}

# ADMISSION_GRANTED already carries the wait; recording it as a metric_sample
# is what lets the slot formula, the cap and the hold ceiling be revised FROM
# the reboot that was supposed to validate them. Without it the proof leaves
# nothing on disk and the constants stay unfalsifiable. The same door
# lib/keepalive.sh uses for bot.heartbeat, same instance-alias subject so the
# samples join registry keyframes with no glue.
#
# NON-BLOCKING AND BOUNDED, which are two different claims and both are needed.
# Non-blocking: the emit runs in a backgrounded subshell whose stdout and stderr
# are redirected at fork time, so it can never hold the caller's command
# substitution open -- start-bot.sh captures the verdict in one, and a child
# inheriting that pipe would wedge every boot on the host behind a plane emit.
# Bounded: start-bot.sh then EXITS while the child is still alive, and under a
# permanently wedged rung (this estate's documented D-state SD stall) an
# unbounded emit made "bounded pileup" a RATE rather than a ceiling -- ~720
# stuck processes/day where keepalive measured it. The wait-then-reap below is
# keepalive.sh:135-171's shape, and the kill is plane_kill_tree rather than a
# kill of $_w: a bare kill of the pipeline leader reaped the leader and ORPHANED
# the wedged CLI alive, which is the whole point defeated.
# $FLEET_NAME/$BOT_NAME are unguarded because plane_armed --require-fleet
# --require-bot returned 0 on the line above; that is the coupling.
_boot_admission_metric() {
    local metric="${1:-}" value="${2:-}" fleet_esc subj payload eto
    plane_armed boot-admission --require-fleet --require-bot || return 0
    case "$value" in ''|*[!0-9]*) return 0 ;; esac
    eto="${BOOT_ADMISSION_EMIT_TIMEOUT_S:-$_BOOT_ADMISSION_EMIT_TIMEOUT_DEFAULT_S}"
    case "$eto" in ''|*[!0-9]*) eto=$_BOOT_ADMISSION_EMIT_TIMEOUT_DEFAULT_S ;; esac
    fleet_esc="$(json_escape "$FLEET_NAME")"
    subj="$(json_escape "bot:$FLEET_NAME/$BOT_NAME")"
    payload='{"events":[{"event_type":"metric_sample","emitter":"start-bot","fleet":"'"$fleet_esc"'","payload":{"subject_kind":"bot_instance","subject":"'"$subj"'","metric":"'"$metric"'","value":'"$value"'}}]}'
    (
        printf '%s' "$payload" | plane_emit_events boot-admission >> "${LOG:-/dev/null}" 2>&1 &
        _w=$!
        _i=0
        while kill -0 "$_w" 2>/dev/null && [ "$_i" -lt "$eto" ]; do
            sleep 1
            _i=$(( _i + 1 ))
        done
        plane_kill_tree "$_w"
    ) >/dev/null 2>&1 &
    return 0
}

# --- the reaper -------------------------------------------------------------

# Runs at the top of every poll iteration of every waiter, UNLOCKED, and
# touches only tickets/, slots/ and data/.boot-queued markers. It deliberately
# does NOT garbage-collect PR A plugins-updated.<epoch>.* stamps: those are
# written under with_lock, and a delete landing just after a write re-arms
# `claude plugin update` for every later bot -- the amplifier PR A removed.
# Only all-DIGIT directory names under state/boot are epoch-tree candidates,
# so the stamps and their lock are never even looked at.
#
# <bots_dir> is optional and its absence means CANNOT LOOK, which is not the
# same answer as NOTHING TO REAP. With no readable bots dir the marker leg is
# skipped outright rather than run on a guess.
#
# <keep_tree> is the caller's OWN acquire-time tree path, and passing it is not
# belt-and-braces: acquire binds that path ONCE and then polls, so a reaper that
# could prune it would be deleting the live state of the process running it.
# The pin makes that nearly impossible; this makes it impossible.
#
# IT RE-RESOLVES NOTHING. The pin is read, never recomputed -- see "the boot
# key" above for the clock step that made a re-resolving reaper delete the tree
# its own waiter was queued in.
_boot_admission_reap() {
    local bots_dir="${1:-}" keep="${2:-}"
    local root state slots tickets grace ceiling now cur_key cur_epoch
    root="$CLAUDLOBBY_ROOT/state/boot"
    # ONCE per reap, not once per file: this was 43 `date` forks per reap on a
    # 21-bot host, inside marker_age_within, run by every waiter every poll.
    now="$(date +%s 2>/dev/null || true)"
    case "$now" in ''|*[!0-9]*) return 0 ;; esac
    grace="${BOOT_ADMISSION_CLAIM_GRACE_S:-$_BOOT_ADMISSION_CLAIM_GRACE_DEFAULT_S}"
    ceiling="${BOOT_HOLD_CEILING_S:-$_BOOT_HOLD_CEILING_DEFAULT_S}"
    case "$grace" in ''|*[!0-9]*) grace=$_BOOT_ADMISSION_CLAIM_GRACE_DEFAULT_S ;; esac
    case "$ceiling" in ''|*[!0-9]*) ceiling=$_BOOT_HOLD_CEILING_DEFAULT_S ;; esac

    _boot_admission_read_pin "$root"
    cur_key="$_BA_KEY"
    cur_epoch="$_BA_EPOCH_PIN"
    # No pin at all means the gate has not run on this host this boot, so there
    # is no answer to "which tree is live" -- and every leg below is addressed
    # by that answer. CANNOT LOOK is not NOTHING TO REAP: a reaper that guessed
    # would prune the live tree of whichever waiter pinned it next
    # (claudlobby/source_state.py has the rule; #1146 is the direction).
    [ -n "$cur_key" ] || return 0

    # 1. Trees under any OTHER boot id: stale UNCONDITIONALLY. Never by reading
    #    a pid a reboot has made meaningless (R14).
    if [ -d "$root" ]; then
        local d name
        for d in "$root"/*/; do
            [ -d "$d" ] || continue
            name="${d%/}"; name="${name##*/}"
            _boot_admission_id_ok "$name" || continue
            if [ "$name" = "$cur_key" ]; then continue; fi
            if [ -n "$keep" ] && [ "${d%/}" = "${keep%/}" ]; then continue; fi
            rm -rf "$d" 2>/dev/null || true
        done
    fi

    state="$(_boot_admission_state_path "$cur_key")"
    slots="$state/slots"
    tickets="$state/tickets"

    # 2. Slots. The GRANT horizon: a holder is doing its bring-up and cannot
    #    poll, so nothing refreshes these and the hold ceiling is the budget.
    if [ -d "$slots" ]; then
        local sd
        for sd in "$slots"/[0-9]*; do
            [ -d "$sd" ] || continue
            if [ ! -f "$sd/pid" ]; then
                # The mkdir landed, the pid is not written yet: HELD inside the
                # claim grace, reapable only past it (R4).
                if _boot_admission_age_within "$sd" "$grace" "$now"; then continue; fi
                _boot_admission_reclaim "$sd" || true
                continue
            fi
            _boot_admission_read_first "$sd/pid"
            if _boot_admission_pid_fresh "$_BA_LINE" "$sd/pid" "$ceiling" "$now"; then continue; fi
            _boot_admission_reclaim "$sd" || true
        done
    fi

    # 3. Tickets. The WAIT horizon, carried by the waiter REFRESHING this file
    #    on every poll -- so alive-and-fresh here means alive and still polling,
    #    and an abandoned ticket (live pid, nobody polling) still ages out.
    if [ -d "$tickets" ]; then
        local tf
        for tf in "$tickets"/*; do
            [ -f "$tf" ] || continue
            _boot_admission_read_kv "$tf"
            if _boot_admission_pid_fresh "$_BA_PID" "$tf" "$ceiling" "$now"; then continue; fi
            rm -f "$tf" 2>/dev/null || true
        done
    fi

    # 4. Markers. Same refresh, same meaning. A marker naming a DEAD launcher
    #    stops suppressing within one poll rather than for the whole grace
    #    window (R7) -- a tighter bound than any mtime, and the one the SIGKILL
    #    case needs.
    if [ -n "$bots_dir" ] && [ -d "$bots_dir" ]; then
        local md
        for md in "$bots_dir"/*/data/.boot-queued; do
            [ -f "$md" ] || continue
            # The epoch beside the pid is what tells a boot-queue from a
            # restart-queue: .boot-queued is written by EVERY start-bot.sh run,
            # so after a restart it is not missing -- it is a plausible
            # timestamp describing a DIFFERENT event, and a later reader gets a
            # confident wrong answer rather than an absent one
            # (lib/boot-capture.sh:19-25, the .spawn lesson verbatim). Both
            # sides of this comparison now come from the PIN, so the only way
            # they differ is the reboot it is meant to catch -- before the pin,
            # a clock step moved one side and wiped every marker on the host.
            _boot_admission_read_kv "$md"
            if [ -n "$cur_epoch" ] && [ "$_BA_EPOCH" != "$cur_epoch" ]; then
                rm -f "$md" 2>/dev/null || true
                continue
            fi
            if _boot_admission_pid_fresh "$_BA_PID" "$md" "$ceiling" "$now"; then continue; fi
            rm -f "$md" 2>/dev/null || true
        done
    fi
    return 0
}

# --- the doors --------------------------------------------------------------

boot_admission_acquire() {
    local bot_dir="${1:?Usage: boot_admission_acquire <bot_dir>}"
    # ${bot_dir%/*} below is how the reaper learns where the markers live, so a
    # trailing slash would hand it the BOT dir instead of the bots dir and the
    # marker leg would silently scan nothing.
    bot_dir="${bot_dir%/}"

    if [ "${BOOT_ADMISSION_DISABLED:-0}" = "1" ]; then
        _boot_admission_log "ADMISSION_DISABLED — proceeding ungated"
        printf 'disabled'
        return 0
    fi

    # R16: nine harness bot.confs and every pre-generate fleet carry
    # BOT_SERVICE="". Proceed ungated rather than share a ticket path with
    # another bot -- two bots on one ticket name is worse than no gate.
    local unit
    unit="$(_boot_admission_unit "$bot_dir")"
    if [ -z "$unit" ]; then
        _boot_admission_log "ADMISSION_UNAVAILABLE (unit name empty) — proceeding ungated"
        _boot_admission_event boot_admission_unavailable \
            "$(printf '{"reason":"unit name empty","dir":"%s"}' "$(json_escape "$bot_dir")")" \
            "$bot_dir"
        printf 'unavailable'
        return 0
    fi

    # The boot key and the boot epoch, PINNED once per host per boot and read
    # from the pin by every other door. Nothing below re-resolves either.
    local key epoch
    _boot_admission_pin "$CLAUDLOBBY_ROOT/state/boot"
    key="$_BA_KEY"
    epoch="$_BA_EPOCH_PIN"

    local state
    state="$(_boot_admission_state_dir "$key" || true)"
    if [ -z "$state" ]; then
        local want
        want="$(_boot_admission_state_path "$key")"
        _boot_admission_log "ADMISSION_UNAVAILABLE (state dir unwritable: $want) — proceeding ungated"
        _boot_admission_event boot_admission_unavailable \
            "$(printf '{"reason":"state dir unwritable","dir":"%s"}' "$(json_escape "$want")")" \
            "$bot_dir"
        printf 'unavailable'
        return 0
    fi

    # An unresolvable epoch is a DEGRADED gate, not a dead one: it queues
    # exactly as plugin_ensure falls back, and DISCLOSES rather than proceeding
    # silently. The verdict is unaffected, which is why the reason has to read
    # as degraded -- on a host where resolve_boot_epoch fails, every bot emits
    # this on every boot while the gate works fine, and a reader filtering on
    # the type alone cannot tell it from a gate that never ran. "Un-keyed" names
    # the EPOCH key specifically: the tree may still be keyed by a boot id, but
    # the marker cannot name its boot and the reaper loses the prior-boot marker
    # test with it.
    if [ -z "$epoch" ]; then
        _boot_admission_log "ADMISSION degraded (boot epoch unresolvable) — still gating under $state, marker cannot name its boot"
        _boot_admission_event boot_admission_unavailable \
            "$(printf '{"reason":"epoch unresolvable — gating un-keyed","dir":"%s"}' "$(json_escape "$state")")" \
            "$bot_dir"
    fi

    local slots_max prio cap ready poll
    slots_max="$(_boot_admission_slots_max "$state")"
    prio="${BOOT_PRIORITY:-1}"
    case "$prio" in [0-9]) : ;; *) prio=1 ;; esac
    cap="${BOOT_ADMISSION_WAIT_MAX_S:-$_BOOT_ADMISSION_WAIT_MAX_DEFAULT_S}"
    case "$cap" in ''|*[!0-9]*) cap=$_BOOT_ADMISSION_WAIT_MAX_DEFAULT_S ;; esac
    ready="${RC_READY_TIMEOUT_S:-$_BOOT_READY_TIMEOUT_DEFAULT_S}"
    case "$ready" in ''|*[!0-9]*) ready=$_BOOT_READY_TIMEOUT_DEFAULT_S ;; esac
    poll="${BOOT_ADMISSION_POLL_S:-$_BOOT_ADMISSION_POLL_DEFAULT_S}"

    # The marker is written at ACQUIRE and removed only at RELEASE. It carries
    # the launcher pid, so a later reader can tell a live bring-up from a
    # wedged one, and the boot epoch, so it can tell a boot-queue from a
    # restart-queue. Both records -- this and the ticket -- are written here
    # and removed there, and the reaper covers both.
    local marker
    marker="$(_boot_admission_marker_path "$bot_dir")"
    mkdir -p "$bot_dir/data" 2>/dev/null || true
    printf 'pid=%s\nepoch=%s\n' "$$" "${epoch:-unknown}" > "$marker" 2>/dev/null || true

    local ticket ticket_path rank eff
    ticket="$prio-$(_boot_admission_stamp)-$unit"
    ticket_path="$state/tickets/$ticket"
    printf 'pid=%s\n' "$$" > "$ticket_path" 2>/dev/null || true

    # The rank is computed ONCE, at acquire, over every ticket in the queue --
    # not the holder-filtered count the wait loop uses. It is the dispersion
    # index, and it must not move while we wait.
    rank="$(_boot_admission_count_before "$state/tickets" "$ticket")"
    eff="$(_boot_admission_effective_cap "$cap" "$rank" "$ready" "$slots_max")"
    # The caller cap NARROWS, never replaces: min(eff, caller_cap). A serial
    # restart driver scopes the wait PER BOT because it is walking the fleet one
    # at a time, and that is always a tightening of the composed policy. A
    # replacement could WIDEN it -- a driver exporting 600 against an effective
    # cap of 60 would triple the wait it was trying to bound, by arithmetic
    # coincidence rather than by anyone's decision. A cap of 0 (never wait)
    # therefore stays 0 whatever a caller passes.
    if [ -n "${BOOT_ADMISSION_CALLER_CAP_S:-}" ]; then
        case "$BOOT_ADMISSION_CALLER_CAP_S" in
            ''|*[!0-9]*) : ;;
            *)
                if [ "$BOOT_ADMISSION_CALLER_CAP_S" -lt "$eff" ]; then
                    eff="$BOOT_ADMISSION_CALLER_CAP_S"
                fi
                ;;
        esac
    fi

    local step=60
    if [ "$eff" -gt "$step" ]; then step="$eff"; fi

    local started last now waited slot holders ahead wait_logged=0
    started="$(date +%s)"
    last="$started"
    waited=0
    while :; do
        # REFRESH FIRST, before our own reap and before any peer can look. The
        # ticket NAME is untouched, so the sort key -- and therefore the queue
        # order and the rank -- cannot move; only the mtime advances. Two
        # fork-free redirects, and they are what let one ceiling mean "alive and
        # still polling" for a waiter and "granted inside its budget" for a
        # holder. Re-writing rather than touching also self-heals: a peer that
        # deleted either record a moment ago has it back on this poll.
        printf 'pid=%s\n' "$$" > "$ticket_path" 2>/dev/null || true
        printf 'pid=%s\nepoch=%s\n' "$$" "${epoch:-unknown}" > "$marker" 2>/dev/null || true
        _boot_admission_reap "${bot_dir%/*}" "$state"
        holders="$(_boot_admission_slot_units "$state/slots")"
        ahead="$(_boot_admission_count_before "$state/tickets" "$ticket" "$holders")"
        if [ "$ahead" -lt "$slots_max" ]; then
            slot="$(_boot_admission_try_take "$state/slots" "$slots_max" "$unit" || true)"
            if [ -n "$slot" ]; then
                now="$(date +%s)"
                waited=$(( now - started ))
                if [ "$waited" -lt 0 ]; then waited=0; fi
                _boot_admission_log "ADMISSION_GRANTED slot=$slot after ${waited}s"
                _boot_admission_metric boot.admission_wait_s "$waited"
                printf 'granted:%s' "$slot"
                return 0
            fi
        fi
        if [ "$wait_logged" -eq 0 ] && [ "$eff" -gt 0 ]; then
            _boot_admission_log "ADMISSION_WAIT queue=$(_boot_admission_queue_len "$state/tickets") slots=$(_boot_admission_held_count "$state/slots")/$slots_max priority=$prio"
            wait_logged=1
        fi
        now="$(date +%s)"
        started="$(_boot_admission_fold "$now" "$last" "$started" "$step")"
        last="$now"
        waited=$(( now - started ))
        if [ "$waited" -lt 0 ]; then waited=0; fi
        if [ "$waited" -ge "$eff" ]; then
            # R10: the OWN ticket goes FIRST, so the next waiter is head of
            # queue immediately rather than one poll later.
            rm -f "$ticket_path" 2>/dev/null || true
            _boot_admission_log "ADMISSION_TIMEOUT after ${waited}s (cap ${eff}s, rank ${rank}) — proceeding without a slot"
            # Recorded on THIS path too. A timeout is the single most
            # informative case for revising the cap and the slot formula from
            # the reboot that was supposed to validate them, and it was the one
            # case whose wait never reached metric_samples.
            _boot_admission_metric boot.admission_wait_s "$waited"
            _boot_admission_event boot_admission_timeout \
                "$(printf '{"queue":%s,"slots_held":%s,"slots_max":%s,"waited_s":%s,"priority":%s,"rank":%s}' \
                    "$(_boot_admission_queue_len "$state/tickets")" \
                    "$(_boot_admission_held_count "$state/slots")" \
                    "$slots_max" "$waited" "$prio" "$rank")" \
                "$bot_dir"
            printf 'timeout'
            return 0
        fi
        sleep "$poll" 2>/dev/null || sleep 1
    done
}

# IT SAYS WHAT IT RELEASED, and says so when it released NOTHING.
#
# The log line used to be unconditional, and that is exactly what hid a real
# defect for the length of a debugging session: every release logged
# ADMISSION_RELEASED while none of them had found a slot, because acquire and
# release were resolving DIFFERENT tree paths (a PATH rebuild between the two
# put sysctl out of reach, so one ran keyed and the other un-keyed). A line that
# cannot distinguish "freed slot 0" from "found nothing" cannot make the next
# instance of that class loud.
#
# The tree comes from the PIN, never from a fresh resolution -- which is the
# structural half of the same fix: there is now one answer per host per boot to
# "which tree", so acquire and release cannot disagree about it at all.
#
# NOTE for a reader of a healthy startup.log: start-bot.sh releases explicitly
# and again from its EXIT trap, so a clean boot logs one ADMISSION_RELEASED
# followed by one ADMISSION_RELEASE_NOOP. The NOOP *after* a RELEASED is that
# idempotent second call. A NOOP with no RELEASED before it is the defect this
# line exists to surface.
boot_admission_release() {
    local bot_dir="${1:?Usage: boot_admission_release <bot_dir>}"
    bot_dir="${bot_dir%/}"
    if [ "${BOOT_ADMISSION_DISABLED:-0}" = "1" ]; then return 0; fi

    local unit state held="" granted_at="" now hold removed=0 marker
    unit="$(_boot_admission_unit "$bot_dir")"
    _boot_admission_read_pin "$CLAUDLOBBY_ROOT/state/boot"
    state="$(_boot_admission_state_path "$_BA_KEY")"

    if [ -n "$unit" ]; then
        if [ -d "$state/slots" ]; then
            local sd
            for sd in "$state/slots"/[0-9]*; do
                [ -d "$sd" ] || continue
                _boot_admission_read_first "$sd/unit"
                if [ "$_BA_LINE" = "$unit" ]; then
                    held="${sd##*/}"
                    _boot_admission_read_first "$sd/granted_at"
                    granted_at="$_BA_LINE"
                    _boot_admission_reclaim "$sd" || true
                    removed=1
                fi
            done
        fi
        # Matched on the ticket own unit FIELD, never on a glob: a `*-<unit>`
        # pattern also matches a longer unit name that merely ends in ours, so
        # one bot release would retire another bot ticket.
        if [ -d "$state/tickets" ]; then
            local tf tname
            for tf in "$state/tickets"/*; do
                [ -f "$tf" ] || continue
                tname="${tf##*/}"
                if [ "$(_boot_admission_ticket_unit "$tname")" = "$unit" ]; then
                    rm -f "$tf" 2>/dev/null || true
                    removed=1
                fi
            done
        fi
    fi

    marker="$(_boot_admission_marker_path "$bot_dir")"
    if [ -e "$marker" ]; then
        rm -f "$marker" 2>/dev/null || true
        removed=1
    fi

    if [ -n "$held" ]; then
        case "$granted_at" in
            ''|*[!0-9]*) : ;;
            *)
                now="$(date +%s)"
                hold=$(( now - granted_at ))
                if [ "$hold" -lt 0 ]; then hold=0; fi
                _boot_admission_metric boot.admission_hold_s "$hold"
                ;;
        esac
    fi

    if [ -n "$held" ]; then
        _boot_admission_log "ADMISSION_RELEASED slot=$held"
    elif [ "$removed" -eq 1 ]; then
        _boot_admission_log "ADMISSION_RELEASED slot=none (ticket/marker only)"
    else
        _boot_admission_log "ADMISSION_RELEASE_NOOP (nothing held)"
    fi
    return 0
}
