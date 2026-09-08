#!/usr/bin/env bash
# send-size-probe.sh — #1493 pane-send size probe: how much of a payload of N
# bytes actually reaches the reader, measured against the reader's own
# transcript, through the REAL pane_send_verified.
#
# WHY IT EXISTS. The production audit found that on macOS a `pane_submitted`
# send above 1 KB arrived whole 86 times in 180, and eighty-five times arrived
# TAIL ONLY — the head, the envelope and the task id gone. The amount lost was
# quantised at one or two multiples of 1024 bytes, which fits one mechanism: a
# single `send-keys` writes the whole payload into the pane's pty at once, the
# macOS pty input queue holds 1024 bytes (TTYHOG), and with IMAXBEL cleared
# (which Apple's cfmakeraw does, and `claude` runs raw) the BSD tty layer
# answers an overflow by FLUSHING the queue rather than dropping the incoming
# byte. That was a FIT, not a measurement. This is the measurement.
#
# It is an A/B against the primitive itself, not a re-derivation of it. The
# control arm drives the same pane_send_verified with PANE_SEND_CHUNK_BYTES=0,
# which is the pre-fix single-send shape byte for byte; the treatment arm drives
# it with the shipped default. A harness that reimplemented the send would
# certify its own copy — the failure class every sibling harness in this
# directory was written after.
#
# GROUND TRUTH IS THE TRANSCRIPT, never the pane (boot-strand-sampler.sh's
# rule). A pane capture is rendered, wrapped, and past a threshold collapsed to
# a placeholder; the session JSONL holds the user turn as submitted. Every
# verdict below is a comparison between the bytes handed to pane_send_verified
# and the bytes in that record.
#
# ── zero spend, zero fleet contact ───────────────────────────────────────────
# The probe boots a real `claude` — the stubbed binary validate-bot-change.sh
# uses cannot exhibit a pty race, which is the whole subject — into
#   * a throwaway CLAUDE_CONFIG_DIR seeded with onboarding/theme/trust ONLY, so
#     the operator's real config, history and project-trust map are never
#     touched and every transcript this writes lands inside the scratch tree;
#   * a scratch cwd, so no fleet, bot dir or repo is the session's workspace;
#   * ANTHROPIC_BASE_URL=http://127.0.0.1:9, so no API call can succeed.
# The seeded config carries NO credentials, so the session is not logged in and
# each turn ends in "Not logged in" in about a second. That is not a limitation:
# the user record is written before the model is reached, which is exactly the
# fact the probe reads, and it makes the run free and fast. The positive control
# PROVES that per host rather than assuming it — a host where the record is not
# written is a host this instrument cannot measure on, and it says so (rc 3)
# instead of reporting a fleet-wide data loss that is really a harness defect.
#
# ── the classification ───────────────────────────────────────────────────────
# Each payload is exactly N bytes, opens with <token>H and ends with T<token>,
# and is otherwise ASCII filler so a byte count and a character count agree (jq
# measures characters). The record is found by EITHER marker, so a payload that
# lost its head is still found by its tail and vice versa. Then:
#   whole      the record equals the payload
#   head-lost  the record is a strict SUFFIX of the payload (the #1493 symptom)
#   tail-lost  the record is a strict PREFIX of the payload
#   absent     no user record carries either marker before the deadline
#   other      a record exists and is neither — reported, never folded into any
#              of the four above, because an unexplained shape is a finding
#
# ── arms are interleaved, not run to completion ──────────────────────────────
# Reps are the outer loop and arms the inner one, so the two arms share the same
# minutes of ambient load rather than each being assigned its own. It is a race
# under measurement; an arm-sequential run would hand one arm a quieter host and
# call the difference a treatment effect (boot-strand-sampler.sh --arms, same
# reasoning, and the same reason it is not optional).
#
# Usage: send-size-probe.sh [--arm chunked|unchunked|both] [--n N]
#                           [--sizes "500 900 ..."] [--deadline SECS] [--keep]
#   --arm A          chunked (the shipped default), unchunked
#                    (PANE_SEND_CHUNK_BYTES=0, the pre-fix control), or both.
#                    Default both.
#   --n N            repetitions per (arm, size). Default 3.
#   --sizes "..."    payload sizes in BYTES, space- or comma-separated.
#                    Default "500 900 1100 1500 2100 3100 4200" — two below the
#                    1024 cliff, five above it.
#   --deadline SECS  per-send wait for the user record. Default 25.
#   --filler F       varied (default) or repeat. See probe_payload: `repeat` is
#                    a one-character filler that trips a SEPARATE, recipient-side
#                    defect, so it measures the TUI rather than the pty.
#   --keep           keep the scratch tree (config dir, transcripts, rows.tsv).
# Env: SEND_PROBE_REAL=1  REQUIRED. This boots a real `claude`; the sibling
#                    real-boot harnesses (BOOT_SAMPLER_REALBOOT,
#                    FRESHBOX_REALBOOT) gate the same way, so a test sweep can
#                    never spend one by accident.
#      CLAUDE_BIN    default `claude` — the real one is the point.
#      CLAUDLOBBY_SRC  checkout whose lib/ is under test (default: this script's).
set -uo pipefail

# ── pure helpers (sourceable for unit tests: guarded main at the bottom) ──────

# usage — the header block above, from "# Usage:" to the first non-comment line.
usage() {
    awk '/^# Usage:/ { f = 1 } f { if (!/^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
    return 0
}

# probe_payload <bytes> <token> [filler]
# A payload of EXACTLY <bytes> bytes: "<token>H" + filler + "T<token>".
# ASCII throughout so the byte length the caller asked for is also the character
# length jq reports, and so a truncation can be read off a length directly.
# Refuses (rc 2) rather than silently shortening when the markers do not fit — a
# payload shorter than requested would be compared against the wrong cliff.
#
# TWO FILLERS, and which one is the default is a measurement rather than taste.
#
#   varied (default)  repeated "<token><4-digit block>_" blocks, so every 16-ish
#                     bytes names its own offset. Two properties come free: any
#                     surviving fragment carries the token, so a record is
#                     findable however it was cut; and a loss is LOCALISABLE —
#                     the missing block numbers say where it happened, which is
#                     what turned an unexplained `other` into the finding below
#                     in one run.
#   repeat            one character, ~N times. This is what the probe shipped
#                     with first, and it measures something else.
#
# THE FINDING, measured here on macOS / claude 2.1.263, because it is the reason
# the default moved. With `cat` as the reader in raw mode — no TUI — a chunked
# 3100- and 8000-byte send arrived BYTE-EXACT under both fillers, so the chunker
# and tmux deliver everything. Against `claude`, at cap 900: the varied filler
# arrived whole at 3100, 4200 and 8000, while the repeat filler lost EXACTLY ONE
# 900-byte chunk out of its middle (3100 -> 2200, 4200 -> 3300), rendering a
# [Pasted text] placeholder as it did. The same repeat payload at cap 300
# arrived whole; at cap 1800 it lost 2044 bytes off its HEAD, which is the pty
# cliff and the reason the cap must stay under 1024.
#
# So the residual is RECIPIENT-SIDE — Claude Code's TUI dropping a chunk when
# consecutive chunks are byte-identical — and needs ~1800 consecutive identical
# bytes to fire, which a dispatch body does not contain and a one-character
# filler manufactures. It is not the #1493 defect and it is not the chunker; it
# is recorded here rather than fixed here, and `--filler repeat` is kept so it
# can be re-measured against a future binary rather than becoming folklore.
probe_payload() {
    local bytes="$1" token="$2" filler="${3:-varied}"
    local head="${token}H" tail="T${token}"
    local fill_n=$(( bytes - ${#head} - ${#tail} ))
    [ "$fill_n" -ge 0 ] || return 2
    local fill=""
    if [ "$fill_n" -gt 0 ]; then
        if [ "$filler" = "repeat" ]; then
            # One printf, not a per-byte loop: this builds a 4 KB string.
            fill=$(printf "%${fill_n}s" "" | tr ' ' 'z')
        else
            local blk i=0
            while [ "${#fill}" -lt "$fill_n" ]; do
                printf -v blk '%s%04d_' "$token" "$i"
                fill="$fill$blk"
                i=$(( i + 1 ))
            done
            fill=${fill:0:$fill_n}
        fi
    fi
    printf '%s%s%s' "$head" "$fill" "$tail"
    return 0
}

# classify_arrival <payload> <arrived>
# The four-way verdict, on bytes alone. Prints exactly one of
# whole / head-lost / tail-lost / other. "absent" is the CALLER's verdict — it
# is the state of having no record at all, which this function cannot be handed.
#
# Strictness matters in both directions. A suffix that is also the whole string
# is `whole`, not head-lost; and a record that is neither prefix nor suffix is
# `other` rather than being forced into the nearest bucket, because a shape
# nobody predicted is the interesting row and rounding it off would hide it.
classify_arrival() {
    local payload="$1" arrived="$2"
    if [ "$arrived" = "$payload" ]; then
        printf 'whole'
    elif [ "${payload%"$arrived"}" != "$payload" ]; then
        printf 'head-lost'
    elif [ "${payload#"$arrived"}" != "$payload" ]; then
        printf 'tail-lost'
    else
        printf 'other'
    fi
    return 0
}

# median <n...> — integer median of the numbers on argv, empty for no argv.
# Lower of the two middles for an even count: a reported figure that is one of
# the observations beats an average that is none of them, and every loss this
# measures is quantised, so an interpolated midpoint would name a byte count the
# instrument never saw.
median() {
    [ "$#" -gt 0 ] || { printf ''; return 0; }
    local mid=$(( ($# + 1) / 2 ))
    printf '%s\n' "$@" | sort -n | sed -n "${mid}p" | tr -d '\n'
    return 0
}

# parse_sizes <string> — space- or comma-separated byte sizes, one per line.
parse_sizes() {
    printf '%s' "$1" | tr ',' ' ' | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true
    return 0
}

# render_table <arm> <rows_file>
# The per-arm table. Rows are TSV: arm<TAB>size<TAB>rep<TAB>verdict<TAB>arrived.
# One awk pass rather than a shell loop per cell, and sizes are sorted
# NUMERICALLY: the whole question is where the cliff is, and a hash-order table
# hides it.
render_table() {
    local arm="$1" rows="$2"
    printf '  %-6s %4s %6s %10s %10s %7s %6s %16s\n' \
        size n whole head-lost tail-lost absent other "median arrived"
    awk -F'\t' -v arm="$arm" '
        $1 == arm {
            n[$2]++
            if ($4 == "whole")     w[$2]++
            if ($4 == "tail-lost") t[$2]++
            if ($4 == "absent")    a[$2]++
            if ($4 == "other")     o[$2]++
            if ($4 == "head-lost") { h[$2]++; arr[$2] = arr[$2] " " $5 }
            if (!($2 in seen)) { seen[$2] = 1; order[++k] = $2 }
        }
        END {
            for (i = 1; i <= k; i++) for (j = i + 1; j <= k; j++)
                if (order[j] + 0 < order[i] + 0) { s = order[i]; order[i] = order[j]; order[j] = s }
            for (i = 1; i <= k; i++) {
                sz = order[i]; med = "-"
                if (h[sz] > 0) {
                    # A single blank as the separator is the default-whitespace
                    # form, so the leading one this string starts with is not a
                    # field.
                    c = split(arr[sz], v, " ")
                    for (x = 1; x <= c; x++) for (y = x + 1; y <= c; y++)
                        if (v[y] + 0 < v[x] + 0) { s = v[x]; v[x] = v[y]; v[y] = s }
                    med = v[int((c + 1) / 2)] + 0
                }
                printf "  %-6s %4d %6d %10d %10d %7d %6d %16s\n",
                    sz, n[sz], w[sz] + 0, h[sz] + 0, t[sz] + 0, a[sz] + 0, o[sz] + 0, med
            }
        }' "$rows"
    return 0
}

# probe_canonical_dir <path> — the path with every symlink resolved, or the
# input unchanged when it cannot be resolved.
#
# Load-bearing, and it cost a run to learn. Claude Code keys workspace trust on
# the RESOLVED cwd, and on macOS $TMPDIR is /var/folders/... which is a symlink
# to /private/var/folders/... . Seeding the trust map under the unresolved form
# leaves the workspace untrusted, so the session opens the "Is this a project you
# trust?" wizard instead of an input box — whose "❯ No, exit" line satisfies the
# input-glyph readiness probe, so the send goes into a menu whose DEFAULT option
# quits, and the whole session disappears mid-measurement. `pwd -P`, not
# realpath(1), which is not on a stock macOS.
probe_canonical_dir() {
    (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"
    return 0
}

# probe_wizard_hint <pane_text>
# Name a KNOWN first-run blocker if the readiness pane is showing one. A hint on
# top of the positive control's refusal, never a replacement for it: the set of
# wizards is open-ended and the control is the sound gate, but the two blockers
# the seed exists to prevent are worth naming rather than leaving to a reader.
probe_wizard_hint() {
    case "$1" in
        *"trust this folder"*|*"Is this a project you"*)
            printf 'the workspace-trust wizard is up — the seeded trust key does not match the session cwd (symlink?)' ;;
        *"Choose the text style"*)
            printf 'the theme picker is up — the seeded config did not take' ;;
        *"Select login method"*|*"/login"*"to continue"*)
            printf 'a login wall is up' ;;
        *) printf '' ;;
    esac
    return 0
}

# probe_path_is_scratch <path> <marker>
# Refuse any destructive touch outside this run's own scratch tree. The probe
# removes a directory tree on every exit path, so the one thing it must never be
# able to do is remove something that is not its own — a fleet, a bot dir, the
# operator's config. Shape, not trust: the path has to carry this run's marker
# AND not look like fleet-owned territory (path_audit's convention).
probe_path_is_scratch() {
    local p="$1" marker="$2"
    case "$p" in
        *"$marker"*) ;;
        *) return 1 ;;
    esac
    case "$p" in
        */runtime/bots/*|*/local/*) return 1 ;;
    esac
    [ -d "$p" ]
}

# ── the run (everything below needs a real host) ─────────────────────────────

ARM=both
REPS=3
SIZES_RAW="500 900 1100 1500 2100 3100 4200"
DEADLINE=25
FILLER=varied
KEEP=0
# Globals, deliberately not locals of main: the EXIT trap fires after main has
# returned, when its locals are already out of scope, so a `local base` would
# leave the tmux server and the scratch tree behind exactly when the run failed.
PROBE_BASE=""
PROBE_MARKER=""
PROBE_SOCK=""
PROBE_SESSION=probe

probe_cleanup() {
    if [ -n "$PROBE_SOCK" ]; then
        # kill-server takes the pane's whole process tree, the real claude
        # included. It leaves the SOCKET FILE behind, though, and a directory of
        # dead sendprobe sockets is litter an operator has to tell apart from a
        # live server — so the file goes too. Never a glob: only this run's.
        bot_tmux "$PROBE_SOCK" kill-server 2>/dev/null
        rm -f "${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)/$PROBE_SOCK" 2>/dev/null
    fi
    [ -n "$PROBE_BASE" ] || return 0
    if [ "$KEEP" = "1" ]; then
        echo "kept: $PROBE_BASE"
    elif probe_path_is_scratch "$PROBE_BASE" "$PROBE_MARKER"; then
        rm -rf "$PROBE_BASE"
    else
        echo "send-size-probe: refusing to remove '$PROBE_BASE' (not this run's scratch tree)" >&2
    fi
    return 0
}

# send_one <socket> <session> <payload> <chunk_cap>
# One send through the REAL primitive. Not a reimplementation and not a wrapper
# that "does what it does": whatever pane_send_verified does — the readiness
# wait, the chunking, the settle, the Enter, the verify and its repair — is what
# is under measurement. The cap is a `local`, so it is in force for the call and
# gone afterwards; a bare assignment prefix on a function call is temporary in
# bash but POSIX-permanent, and the arm identity of a run must not rest on that.
send_one() {
    local PANE_SEND_CHUNK_BYTES="$4"
    pane_send_verified "$1" "$2" "$3" >>"$PROBE_BASE/send.log" 2>&1
    return 0
}

# await_record <projects_dir> <token> <deadline_s>
# Print the CONTENT of the user record carrying <token>, or nothing.
#
# Either marker finds it: a head-lost arrival keeps only T<token> and a tail-lost
# one keeps only <token>H, and searching for the whole payload would find
# neither — which would report the very losses being measured as `absent` and
# erase the distinction the instrument exists to draw.
#
# Assistant records are excluded so a model echo can never count (the sampler's
# rule). Under a dead API base URL there are no assistant turns at all, which is
# a reason to keep the filter rather than to drop it: the probe must stay correct
# on a host where the session IS authenticated.
await_record() {
    local proj="$1" token="$2" deadline="$3" i f hit
    i=0
    while [ "$i" -lt "$deadline" ]; do
        for f in "$proj"/*/*.jsonl; do
            [ -f "$f" ] || continue
            grep -q -- "$token" "$f" 2>/dev/null || continue
            hit=$(jq -rc --arg t "$token" \
                'select(.type=="user") | (.message.content | tostring)
                 | select(contains($t))' "$f" 2>/dev/null | head -1)
            if [ -n "$hit" ]; then printf '%s' "$hit"; return 0; fi
        done
        sleep 1
        i=$((i + 1))
    done
    return 1
}

main() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --arm)      ARM="${2:-}"; shift 2 ;;
            --n)        REPS="${2:-}"; shift 2 ;;
            --sizes)    SIZES_RAW="${2:-}"; shift 2 ;;
            --deadline) DEADLINE="${2:-}"; shift 2 ;;
            --filler)   FILLER="${2:-}"; shift 2 ;;
            --keep)     KEEP=1; shift ;;
            -h|--help)  usage; return 0 ;;
            *) echo "send-size-probe: unknown argument '$1'" >&2; usage >&2; return 2 ;;
        esac
    done

    case "$ARM" in chunked|unchunked|both) ;; *)
        echo "send-size-probe: --arm must be chunked, unchunked or both (got '$ARM')" >&2
        return 2 ;;
    esac
    case "$REPS" in ''|*[!0-9]*) echo "send-size-probe: --n must be a count" >&2; return 2 ;; esac
    [ "$REPS" -gt 0 ] || { echo "send-size-probe: --n must be > 0" >&2; return 2; }
    case "$DEADLINE" in ''|*[!0-9]*) echo "send-size-probe: --deadline must be seconds" >&2; return 2 ;; esac
    case "$FILLER" in varied|repeat) ;; *)
        echo "send-size-probe: --filler must be varied or repeat (got '$FILLER')" >&2
        return 2 ;;
    esac

    local sizes
    sizes=$(parse_sizes "$SIZES_RAW")
    [ -n "$sizes" ] || { echo "send-size-probe: --sizes parsed to nothing" >&2; return 2; }

    if [ "${SEND_PROBE_REAL:-}" != "1" ]; then
        echo "send-size-probe: refusing — this boots a REAL claude." >&2
        echo "  Set SEND_PROBE_REAL=1 to run it (BOOT_SAMPLER_REALBOOT precedent)." >&2
        return 2
    fi

    local src="${CLAUDLOBBY_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
    local claude_bin="${CLAUDE_BIN:-claude}"
    local dep miss=""
    for dep in tmux jq "$claude_bin"; do
        command -v "$dep" >/dev/null 2>&1 || miss="$miss $dep"
    done
    [ -z "$miss" ] || { echo "send-size-probe: missing dependencies:$miss" >&2; return 3; }

    # The harness exemption, and the only flag that silences a plane record
    # (lib/plane-emit.sh). A probe's synthetic send_blind / send_retry rows have
    # no business in the host's plane — #846's constructed-destination rule.
    export PLANE_EMIT_DISABLED=1
    # shellcheck source=./lib-common.sh
    . "$src/lib/lib-common.sh"
    # lib-common arms `set -e` at source time on its caller's behalf. A probe
    # measures failures rather than aborting on them, and a mid-run abort would
    # print a partial table that reads as a complete one.
    set +e

    PROBE_MARKER="sendprobe.$$"
    PROBE_SOCK="sendprobe$$"
    PROBE_BASE=$(mktemp -d "${TMPDIR:-/tmp}/${PROBE_MARKER}.XXXXXX") || {
        echo "send-size-probe: mktemp failed" >&2; return 3; }
    trap probe_cleanup EXIT INT TERM

    local cwd="$PROBE_BASE/cwd" cfg="$PROBE_BASE/config" rows="$PROBE_BASE/rows.tsv"
    mkdir -p "$cwd" "$cfg" || return 3
    # Resolved, because the trust key is matched against the resolved form; see
    # probe_canonical_dir. The config dir is not trust-keyed and stays as-is.
    cwd=$(probe_canonical_dir "$cwd")
    : > "$rows"
    : > "$PROBE_BASE/send.log"

    # Arm the readiness wait for the whole run. In production this is opt-in and
    # only the cold-boot injector arms it (a 45s block on a report-back path is a
    # hazard, not a safeguard); here every send targets a pane the probe owns and
    # nothing else is waiting on it, so waiting for the box before each send is
    # what keeps a send from being classified against a pane that is mid-render.
    export PANE_READY_TICKS="${PANE_READY_TICKS:-120}"

    # The throwaway config. Onboarding + theme so no wizard blocks the box — a
    # null theme opens the theme picker, whose "❯ 2. Dark mode" line even
    # satisfies the input-glyph probe, which is a readiness signal that is not a
    # box; trust so the workspace is not re-asked for. NO credentials are copied:
    # the probe must not be able to spend, and the record it reads is written
    # before the model is reached.
    local ver
    ver=$("$claude_bin" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    jq -n --arg cwd "$cwd" --arg ver "${ver:-0.0.0}" '{
        hasCompletedOnboarding: true,
        lastOnboardingVersion: $ver,
        theme: "dark",
        projects: { ($cwd): {
            hasTrustDialogAccepted: true,
            hasCompletedProjectOnboarding: true,
            allowedTools: [], history: [] } }
    }' > "$cfg/.claude.json" || { echo "send-size-probe: could not seed config" >&2; return 3; }

    echo "send-size-probe (#1493)"
    echo "  claude:   $claude_bin ${ver:-unknown}"
    echo "  lib:      $src/lib/lib-common.sh"
    echo "  scratch:  $PROBE_BASE"
    echo "  arms:     $ARM   reps: $REPS   deadline: ${DEADLINE}s   filler: $FILLER"
    echo "  sizes:    $(printf '%s ' $sizes)"
    echo "  chunking: cap=$_PANE_SEND_CHUNK_BYTES_DEFAULT settle=${PANE_SEND_CHUNK_SETTLE_S:-$_PANE_SEND_CHUNK_SETTLE_DEFAULT}s"
    echo ""

    bot_tmux "$PROBE_SOCK" new-session -d -s "$PROBE_SESSION" -x 200 -y 50 \
        "cd '$cwd' && CLAUDE_CONFIG_DIR='$cfg' ANTHROPIC_BASE_URL=http://127.0.0.1:9 exec '$claude_bin'" \
        || { echo "send-size-probe: could not start the probe session" >&2; return 3; }

    local box ready_pane
    box=$(pane_await_input_box "$PROBE_SOCK" "$PROBE_SESSION")
    # Kept on disk, because the state that explains a refusal is the state at
    # READINESS and by then the session may be gone: a first-run wizard whose
    # default option is "exit" takes the whole tmux server with it when the
    # control's Enter lands, so a refusal that captures the pane afterwards
    # prints "no server running" and names nothing.
    ready_pane=$(bot_tmux "$PROBE_SOCK" capture-pane -t "$PROBE_SESSION" -p 2>/dev/null)
    printf '%s\n' "$ready_pane" > "$PROBE_BASE/pane-at-ready.txt"
    if [ "$box" != "drawn" ]; then
        echo "send-size-probe: the TUI never drew an input box (verdict: $box) — cannot measure" >&2
        printf '%s\n' "$ready_pane" | tail -20 >&2
        return 3
    fi
    # The transcript directory is derived from the cwd the way Claude Code
    # derives it, but it is GLOBBED rather than computed: the slug rule is the
    # binary's, not ours, and a probe that hard-codes it would report a
    # fleet-wide data loss the day the rule changes.
    local proj="$cfg/projects"

    # ── positive control ──────────────────────────────────────────────────
    # A 100-byte payload, far below any cliff, through the same door. If ITS user
    # turn is not recorded then the instrument cannot see arrivals on this host,
    # and every verdict below would be a fabricated "absent". Refuse.
    local ctl_tok="SPCTL$$" ctl_pay ctl_got ctl_verdict
    ctl_pay=$(probe_payload 100 "$ctl_tok" "$FILLER")
    send_one "$PROBE_SOCK" "$PROBE_SESSION" "$ctl_pay" "$_PANE_SEND_CHUNK_BYTES_DEFAULT"
    ctl_got=$(await_record "$proj" "$ctl_tok" "$DEADLINE")
    if [ -z "$ctl_got" ]; then
        local hint
        hint=$(probe_wizard_hint "$ready_pane")
        echo "REFUSED (rc 3): the positive control was not recorded." >&2
        echo "  A 100-byte payload was submitted and no user record carrying its marker" >&2
        echo "  appeared under $proj within ${DEADLINE}s." >&2
        echo "  This instrument reads arrivals from the session transcript; without one it" >&2
        echo "  would report every send as absent. That is a property of this host, not of" >&2
        echo "  the send primitive, so the probe declines to measure rather than publish a" >&2
        echo "  loss it cannot attribute." >&2
        [ -z "$hint" ] || echo "  hint: $hint" >&2
        echo "  --- pane at readiness (before the control send) ---" >&2
        printf '%s\n' "$ready_pane" | grep -v '^[[:space:]]*$' | tail -20 >&2
        return 3
    fi
    ctl_verdict=$(classify_arrival "$ctl_pay" "$ctl_got")
    echo "positive control: 100-byte payload recorded, verdict=$ctl_verdict"
    if [ "$ctl_verdict" != "whole" ]; then
        echo "REFUSED (rc 3): the positive control did not arrive whole ($ctl_verdict)." >&2
        echo "  A payload this far below the 1024-byte queue cannot be lost to it, so" >&2
        echo "  something other than the mechanism under test is corrupting sends here." >&2
        return 3
    fi
    echo ""

    # ── the matrix ────────────────────────────────────────────────────────
    local arms
    case "$ARM" in
        both) arms="chunked unchunked" ;;
        *)    arms="$ARM" ;;
    esac

    local rep=1 size arm tok payload got verdict arrived cap
    while [ "$rep" -le "$REPS" ]; do
        for size in $sizes; do
            # Arms inner: the two share the same minute of host conditions.
            for arm in $arms; do
                tok="SP${size}${arm}${rep}z$$"
                payload=$(probe_payload "$size" "$tok" "$FILLER") || {
                    echo "  skip size=$size (too small for the markers)" >&2; continue; }
                case "$arm" in
                    unchunked) cap=0 ;;
                    *)         cap="$_PANE_SEND_CHUNK_BYTES_DEFAULT" ;;
                esac
                send_one "$PROBE_SOCK" "$PROBE_SESSION" "$payload" "$cap"
                got=$(await_record "$proj" "$tok" "$DEADLINE")
                if [ -z "$got" ]; then
                    verdict=absent
                    arrived=0
                    # Nothing was submitted, so the payload may still be sitting
                    # in the box; clear it, or the next send concatenates onto it
                    # and BOTH rows become `other`.
                    bot_tmux "$PROBE_SOCK" send-keys -t "$PROBE_SESSION" C-c 2>/dev/null
                    sleep 0.5
                else
                    verdict=$(classify_arrival "$payload" "$got")
                    arrived=$(printf '%s' "$got" | LC_ALL=C wc -c | tr -d ' ')
                fi
                printf '%s\t%s\t%s\t%s\t%s\n' "$arm" "$size" "$rep" "$verdict" "$arrived" >> "$rows"
                printf '  rep %d  %-9s %5s bytes -> %-9s arrived %s\n' \
                    "$rep" "$arm" "$size" "$verdict" "$arrived"
            done
        done
        rep=$((rep + 1))
    done

    echo ""
    for arm in $arms; do
        echo "=== arm: $arm ==="
        render_table "$arm" "$rows"
        echo ""
    done
    return 0
}

# Guarded so the pure helpers above can be sourced by tests/test_send_size_probe.py
# without booting anything.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
    exit $?
fi
