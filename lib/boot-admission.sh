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
#                                       its marker; idempotent.
#   _boot_admission_reap [bots_dir]   — tickets, slots and markers whose holder
#                                       is dead or past its hold ceiling.
#
# THE GATE NEVER BLOCKS INDEFINITELY AND NEVER FAILS CLOSED. Every failure
# path -- unwritable state dir, unresolvable epoch, empty unit name, cap
# reached -- proceeds, logs, and where it is a verdict, events. A gate that
# could strand a bot forever would be the failure it replaces, not a fix.
#
# STATE lives under $CLAUDLOBBY_ROOT/state/boot/<boot-epoch>/{tickets,slots},
# keyed on resolve_boot_epoch exactly as this directory two existing tenants
# already are (plugins-updated.<epoch>.<plugin>; runtime/_host/boot-capture/
# <epoch>). A prior-epoch tree is stale UNCONDITIONALLY -- never by reading a
# pid that a reboot has made meaningless. An unresolvable epoch falls back to
# the un-keyed path with a log line, as plugin_ensure does.
#
# TAKE is `mkdir slots/<n>` -- the atomic mutex. `mv` is NOT one: `mv src dst`
# where dst is an existing directory moves src INSIDE it rather than failing
# (measured, R0). RECLAIM is one atomic rename to a name that cannot already
# exist -- mktemp -d makes the holder, the slot moves to <holder>/slot -- so
# exactly one racer wins and every loser sees its source already gone. Never
# rmdir + mkdir.
#
# LIVENESS is `kill -0 <pid>` PAIRED WITH marker_age_within on the file that
# records the pid. kill -0 alone is NOT sufficient: a RE-USED pid blocked a
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

# --- internals --------------------------------------------------------------

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

# The state tree path for an epoch. Creates nothing -- the reaper must be able
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

# The liveness PAIR, for a file whose first line is a bare pid (a slot).
_boot_admission_slot_alive() {
    local pidf="${1:-}" ceiling="${2:-}" p
    p="$(head -1 "$pidf" 2>/dev/null || true)"
    case "$p" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$p" 2>/dev/null || return 1
    marker_age_within "$pidf" "$ceiling" || return 1
    return 0
}

# The same pair, for a file carrying `pid=<n>` (a ticket, a marker).
_boot_admission_owner_alive() {
    local f="${1:-}" ceiling="${2:-}" p
    p="$(sed -n 's/^pid=//p' "$f" 2>/dev/null | head -1 || true)"
    case "$p" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$p" 2>/dev/null || return 1
    marker_age_within "$f" "$ceiling" || return 1
    return 0
}

# marker_age_within refuses a directory ([ -f ]), and the pid-less slot case
# has only a directory to age. Same arithmetic, same fail-toward-HELD posture:
# a clock step that makes the age negative reads as within.
_boot_admission_age_within() {
    local p="${1:-}" max="${2:-}" m now
    [ -e "$p" ] || return 1
    m="$(stat_mtime "$p" 2>/dev/null || true)"
    case "$m" in ''|*[!0-9]*) return 1 ;; esac
    now="$(date +%s)"
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

# No pipe, deliberately: `printf | grep -q` is a pipefail trap -- grep -q
# exits on its first match, SIGPIPEs the writer, and under `set -o pipefail`
# the PIPELINE then reports failure on exactly the case that matched. The
# holders exclusion is what keeps the queue head moving, so a silently
# inverted answer here would strand every waiter behind a granted bot.
_boot_admission_in_list() {
    local needle="${1:-}" line
    [ -n "$needle" ] || return 1
    while IFS= read -r line; do
        if [ "$line" = "$needle" ]; then return 0; fi
    done <<EOF
${2:-}
EOF
    return 1
}

# The unit names currently holding a slot.
_boot_admission_slot_units() {
    local d="${1:-}" sd
    [ -d "$d" ] || return 0
    for sd in "$d"/[0-9]*; do
        [ -d "$sd" ] || continue
        [ -f "$sd/unit" ] || continue
        head -1 "$sd/unit" 2>/dev/null || true
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
            v="$(head -1 "$f" 2>/dev/null || true)"
            case "$v" in ''|*[!0-9]*) v="" ;; esac
            if [ -z "$v" ]; then
                v="$(_boot_admission_derive_slots "$(_boot_admission_ncpu)")"
                tmp="$state/.slots.max.$$.$(_boot_admission_stamp)"
                if printf '%s\n' "$v" > "$tmp" 2>/dev/null; then
                    mv "$tmp" "$f" 2>/dev/null || true
                fi
                rm -f "$tmp" 2>/dev/null || true
                tmp="$(head -1 "$f" 2>/dev/null || true)"
                case "$tmp" in ''|*[!0-9]*) : ;; *) v="$tmp" ;; esac
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
_boot_admission_try_take() {
    local d="${1:-}" max="${2:-}" unit="${3:-}" i=0
    while [ "$i" -lt "$max" ]; do
        if mkdir "$d/$i" 2>/dev/null; then
            printf '%s\n' "$$" > "$d/$i/pid" 2>/dev/null || true
            printf '%s\n' "$unit" > "$d/$i/unit" 2>/dev/null || true
            printf '%s\n' "$(date +%s)" > "$d/$i/granted_at" 2>/dev/null || true
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
# samples join registry keyframes with no glue. Non-blocking: the emit runs in
# a backgrounded subshell whose stdout and stderr are redirected to LOG at
# fork time, so it can never hold the callers command substitution open.
_boot_admission_metric() {
    local metric="${1:-}" value="${2:-}" fleet_esc subj payload
    plane_armed boot-admission --require-fleet --require-bot || return 0
    case "$value" in ''|*[!0-9]*) return 0 ;; esac
    fleet_esc="$(json_escape "$FLEET_NAME")"
    subj="$(json_escape "bot:$FLEET_NAME/$BOT_NAME")"
    payload='{"events":[{"event_type":"metric_sample","emitter":"start-bot","fleet":"'"$fleet_esc"'","payload":{"subject_kind":"bot_instance","subject":"'"$subj"'","metric":"'"$metric"'","value":'"$value"'}}]}'
    (
        printf '%s' "$payload" | plane_emit_events boot-admission
    ) >> "${LOG:-/dev/null}" 2>&1 &
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
_boot_admission_reap() {
    local bots_dir="${1:-}"
    local epoch root state slots tickets grace ceiling
    epoch="$(resolve_boot_epoch 2>/dev/null || true)"
    root="$CLAUDLOBBY_ROOT/state/boot"
    grace="${BOOT_ADMISSION_CLAIM_GRACE_S:-$_BOOT_ADMISSION_CLAIM_GRACE_DEFAULT_S}"
    ceiling="${BOOT_HOLD_CEILING_S:-$_BOOT_HOLD_CEILING_DEFAULT_S}"
    case "$grace" in ''|*[!0-9]*) grace=$_BOOT_ADMISSION_CLAIM_GRACE_DEFAULT_S ;; esac
    case "$ceiling" in ''|*[!0-9]*) ceiling=$_BOOT_HOLD_CEILING_DEFAULT_S ;; esac

    # 1. Prior-epoch trees: stale UNCONDITIONALLY. Never by reading a pid a
    #    reboot has made meaningless (R14).
    if [ -n "$epoch" ] && [ -d "$root" ]; then
        local d name
        for d in "$root"/*/; do
            [ -d "$d" ] || continue
            name="${d%/}"; name="${name##*/}"
            case "$name" in ''|*[!0-9]*) continue ;; esac
            if [ "$name" = "$epoch" ]; then continue; fi
            rm -rf "$d" 2>/dev/null || true
        done
    fi

    state="$(_boot_admission_state_path "$epoch")"
    slots="$state/slots"
    tickets="$state/tickets"

    # 2. Slots.
    if [ -d "$slots" ]; then
        local sd
        for sd in "$slots"/[0-9]*; do
            [ -d "$sd" ] || continue
            if [ ! -f "$sd/pid" ]; then
                # The mkdir landed, the pid is not written yet: HELD inside the
                # claim grace, reapable only past it (R4).
                if _boot_admission_age_within "$sd" "$grace"; then continue; fi
                _boot_admission_reclaim "$sd" || true
                continue
            fi
            if _boot_admission_slot_alive "$sd/pid" "$ceiling"; then continue; fi
            _boot_admission_reclaim "$sd" || true
        done
    fi

    # 3. Tickets.
    if [ -d "$tickets" ]; then
        local tf
        for tf in "$tickets"/*; do
            [ -f "$tf" ] || continue
            if _boot_admission_owner_alive "$tf" "$ceiling"; then continue; fi
            rm -f "$tf" 2>/dev/null || true
        done
    fi

    # 4. Markers. A marker naming a DEAD launcher stops suppressing within one
    #    poll rather than for the whole grace window (R7) -- a tighter bound
    #    than any mtime, and the one the SIGKILL case needs.
    if [ -n "$bots_dir" ] && [ -d "$bots_dir" ]; then
        local md mepoch
        for md in "$bots_dir"/*/data/.boot-queued; do
            [ -f "$md" ] || continue
            # The epoch beside the pid is what tells a boot-queue from a
            # restart-queue: .boot-queued is written by EVERY start-bot.sh run,
            # so after a restart it is not missing -- it is a plausible
            # timestamp describing a DIFFERENT event, and a later reader gets a
            # confident wrong answer rather than an absent one
            # (lib/boot-capture.sh:19-25, the .spawn lesson verbatim).
            mepoch="$(sed -n 's/^epoch=//p' "$md" 2>/dev/null | head -1 || true)"
            if [ -n "$epoch" ] && [ "$mepoch" != "$epoch" ]; then
                rm -f "$md" 2>/dev/null || true
                continue
            fi
            if _boot_admission_owner_alive "$md" "$ceiling"; then continue; fi
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

    local epoch
    epoch="$(resolve_boot_epoch 2>/dev/null || true)"

    local state
    state="$(_boot_admission_state_dir "$epoch" || true)"
    if [ -z "$state" ]; then
        local want
        want="$(_boot_admission_state_path "$epoch")"
        _boot_admission_log "ADMISSION_UNAVAILABLE (state dir unwritable: $want) — proceeding ungated"
        _boot_admission_event boot_admission_unavailable \
            "$(printf '{"reason":"state dir unwritable","dir":"%s"}' "$(json_escape "$want")")" \
            "$bot_dir"
        printf 'unavailable'
        return 0
    fi

    # An unresolvable epoch is a DEGRADED gate, not a dead one: it queues on
    # the un-keyed path exactly as plugin_ensure falls back, and discloses the
    # fact rather than proceeding silently. The verdict is unaffected.
    if [ -z "$epoch" ]; then
        _boot_admission_log "ADMISSION unkeyed (boot epoch unresolvable) — queueing under $state"
        _boot_admission_event boot_admission_unavailable \
            "$(printf '{"reason":"epoch unresolvable","dir":"%s"}' "$(json_escape "$state")")" \
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
    if [ -n "${BOOT_ADMISSION_CALLER_CAP_S:-}" ]; then
        case "$BOOT_ADMISSION_CALLER_CAP_S" in
            ''|*[!0-9]*) : ;;
            *) eff="$BOOT_ADMISSION_CALLER_CAP_S" ;;
        esac
    fi

    local step=60
    if [ "$eff" -gt "$step" ]; then step="$eff"; fi

    local started last now waited slot holders ahead wait_logged=0
    started="$(date +%s)"
    last="$started"
    waited=0
    while :; do
        _boot_admission_reap "${bot_dir%/*}"
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

boot_admission_release() {
    local bot_dir="${1:?Usage: boot_admission_release <bot_dir>}"
    bot_dir="${bot_dir%/}"
    if [ "${BOOT_ADMISSION_DISABLED:-0}" = "1" ]; then return 0; fi

    local unit epoch state held="" granted_at="" now hold
    unit="$(_boot_admission_unit "$bot_dir")"
    epoch="$(resolve_boot_epoch 2>/dev/null || true)"
    state="$(_boot_admission_state_path "$epoch")"

    if [ -n "$unit" ]; then
        if [ -d "$state/slots" ]; then
            local sd u
            for sd in "$state/slots"/[0-9]*; do
                [ -d "$sd" ] || continue
                u="$(head -1 "$sd/unit" 2>/dev/null || true)"
                if [ "$u" = "$unit" ]; then
                    held="${sd##*/}"
                    granted_at="$(head -1 "$sd/granted_at" 2>/dev/null || true)"
                    _boot_admission_reclaim "$sd" || true
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
                fi
            done
        fi
    fi

    rm -f "$(_boot_admission_marker_path "$bot_dir")" 2>/dev/null || true

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

    _boot_admission_log "ADMISSION_RELEASED"
    return 0
}
