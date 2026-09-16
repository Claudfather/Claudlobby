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
#   * a CONSTRUCTED environment, never an inherited one (see below);
#   * a throwaway CLAUDE_CONFIG_DIR seeded with onboarding/theme/trust ONLY, so
#     the operator's real config, history and project-trust map are never
#     touched and every transcript this writes lands inside the scratch tree;
#   * a throwaway HOME, so nothing the session writes outside its config dir
#     lands in the operator's;
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
# ── the environment is BUILT, not inherited (chunk O fold, F4) ────────────────
# The first cut set two variables on the child command line and let
# `tmux new-session` pass the rest of the caller's environment through, which
# made three of the claims above conditional on who ran it. A bot session
# exports BOT_DIR / CLAUDLOBBY_ROOT / FLEET_NAME (the #846 vector — a harness
# resolving onto the operator's live estate); ANTHROPIC_API_KEY reaches the
# child directly; and CLAUDE_CODE_USE_BEDROCK / CLAUDE_CODE_USE_VERTEX route
# AROUND ANTHROPIC_BASE_URL entirely, so "it cannot spend" was not true for a
# caller who had either set — the exact shape of a safety claim that holds only
# in the environment its author happened to test in.
#
# So the child gets `env -i` plus an explicit base — boot-strand-sampler.sh's
# run_start_bot ladder, same reasoning (this is a third copy of that ladder and
# its forbidden-name set; the estate's copies are the tracked #846 seam — when
# that seam lands, this and probe_env_leaks fold into it) — and the isolation is
# ASSERTED rather
# than asserted-about: the pane dumps its own environment before exec'ing
# `claude`, and a dump carrying anything from the forbidden set REFUSES (rc 3).
# An UNREADABLE dump refuses too. A canary that silently fell back to the real
# environment would pass by coincidence (rehearse-env-cascade.sh's rule), and a
# dump the probe could not read is exactly that fallback wearing a green tick.
# `ps eww` was measured and rejected: on macOS it prints the command and no
# environment at all, so a check built on it reads clean by construction.
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
#                           [--sizes "500 900 ..."] [--deadline SECS]
#                           [--filler varied|repeat|ident2] [--via-hook]
#                           [--keep] [--reap]
#   --arm A          chunked (the shipped default), unchunked
#                    (PANE_SEND_CHUNK_BYTES=0, the pre-fix control), or both.
#                    Default both.
#   --n N            repetitions per (arm, size). Default 3.
#   --sizes "..."    payload sizes in BYTES, space- or comma-separated.
#                    Default "500 900 1100 1500 2100 3100 4200" — two below the
#                    1024 cliff, five above it.
#   --deadline SECS  per-send wait for the user record. Default 25.
#   --filler F       varied (default), repeat or ident2. See probe_payload:
#                    `repeat` and `ident2` both build a payload whose adjacent
#                    900-byte chunks are BYTE-IDENTICAL, which trips a separate,
#                    recipient-side defect — so they measure the TUI rather than
#                    the pty. `ident2` is the realistic form of it (ordinary
#                    numbered lines, not one repeated character).
#   --via-hook       chunk P (#1501): after each send, run the RECEIVER hook
#                    (plane-dispatch-in.sh) against the arrived text and assert
#                    the `received` fact it emits AGREES with this probe's own
#                    transcript verdict (whole -> sha matches; head/tail-lost ->
#                    fewer bytes than sent). The instrument checking the
#                    instrument. Any disagreement fails the run (rc 1); a host
#                    with no plane recorder degrades to `unavailable`, never a
#                    false disagreement. OFF by default; zero cost when unset.
#   --keep           keep the scratch tree (config dir, transcripts, rows.tsv).
#   --reap           kill any leftover sendprobe tmux server and remove its
#                    socket, then exit. Needs no gate: it destroys only this
#                    harness's own litter (coldstart-harness.sh `reap`).
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
# THREE FILLERS, and which one is the default is a measurement rather than taste.
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
#   ident2            ORDINARY numbered-line text laid out in 900-byte blocks
#                     that repeat on the cap, so chunk N and chunk N+1 are
#                     byte-identical while the text is nothing like degenerate.
#                     This is the shape that actually names the residual.
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
# THE TRIGGER, correctly stated (chunk O fold, F2). An earlier version of this
# note said the residual needs ~1800 consecutive identical bytes and so cannot
# reach a real dispatch. That was wrong, and reading a one-character filler as
# the cause is what made it look true. Measured on the same binary: two
# identical 900-byte blocks of ORDINARY numbered-line text lost 900 bytes out of
# the middle; 1200 identical bytes followed by a varied tail arrived WHOLE; and
# thirty identical 60-byte lines whose phase did not align with the cap arrived
# whole. So the trigger is exactly `chunk[i] == chunk[i-1]` — nothing about how
# long the identical run is — and any repeated region whose period divides the
# cap and starts on a boundary reaches it. Thirty-one identical 60-byte log
# lines is enough, which a dispatch quoting a log or a table gets to without
# trying.
#
# It is still RECIPIENT-SIDE — Claude Code's TUI dropping a chunk when
# consecutive chunks are byte-identical — and still not the #1493 defect. But it
# is no longer only recorded: `_pane_split_bytes` now guarantees no two adjacent
# chunks are ever identical (one byte shorter breaks the tie), so the splitter
# prevents the trigger from arising. The three fillers stay so the finding can be
# re-measured against a future binary rather than becoming folklore.
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
        elif [ "$filler" = "ident2" ]; then
            # A 900-byte block that STARTS AT THE PAYLOAD'S FIRST BYTE, repeated.
            # The head marker has to be inside the block or the repeat would be
            # offset by its length and no two chunks would align — the alignment
            # is the whole point, so the block is built as head + body and the
            # filler emitted as body, block, block, ... The payload is then
            # block-periodic from byte 0, and at cap 900 chunk 0 and chunk 1 are
            # byte-identical, which is the failing shape.
            #
            # NO NEWLINE in the unit, and that is not cosmetic: send-keys -l
            # types a newline as a newline, so the TUI SUBMITS there and the
            # payload arrives as several turns. Caught by the positive control
            # on the first real run of this filler — it reported `tail-lost` on
            # a 100-byte payload, which is a shape no pty queue can produce.
            local blk="" ln i=0
            while [ "${#blk}" -lt 900 ]; do
                printf -v ln '%s%04d ordinary numbered line of dispatch-like text | ' "$token" "$i"
                blk="$blk$ln"
                i=$(( i + 1 ))
            done
            blk="${head}${blk}"
            blk=${blk:0:900}
            fill=${blk:${#head}}
            while [ "${#fill}" -lt "$fill_n" ]; do fill="$fill$blk"; done
            fill=${fill:0:$fill_n}
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

# --- chunk P (#1501): the receiver-hook cross-check (--via-hook) --------------
# via_hook_agrees <verdict> <payload_sha> <payload_bytes> <received_sha> <received_bytes>
# The instrument checking the instrument: does the RECEIVER hook's `received`
# fact agree with THIS probe's independent transcript verdict? Prints exactly
# one of agree / disagree / skip.
#   whole      -> the hook must hash the arrival identical to the sent payload
#   head-lost  -> a strict suffix, so FEWER bytes than sent
#   tail-lost  -> a strict prefix, also FEWER bytes (in production the lost tail
#                 takes the trailer with it and NO received fact lands — the
#                 honest UNCONFIRMED; here the probe hands the hook the arrived
#                 text directly, so shorter-than-sent is the checkable invariant)
#   absent / other -> no claim (skip)
via_hook_agrees() {
    local verdict="$1" psha="$2" pbytes="$3" rsha="$4" rbytes="$5"
    case "$verdict" in
        whole)
            [ "$rsha" = "$psha" ] && printf 'agree' || printf 'disagree' ;;
        head-lost|tail-lost)
            case "$rbytes" in ''|*[!0-9]*) printf 'disagree'; return 0 ;; esac
            [ "$rbytes" -lt "$pbytes" ] && printf 'agree' || printf 'disagree' ;;
        *)  printf 'skip' ;;
    esac
    return 0
}

# payload_sha256 <text> -- "sha256:<hex>" over the UTF-8 bytes, the form the
# plane stores (contracts.cap_body). Neither BSD `shasum` nor GNU `sha256sum` is
# guaranteed, so a missing tool prints nothing + returns 1 and the caller treats
# the cross-check as unavailable rather than manufacturing a disagreement.
payload_sha256() {
    local hex=""
    if command -v shasum >/dev/null 2>&1; then
        hex=$(printf '%s' "$1" | shasum -a 256 2>/dev/null | awk '{print $1}')
    elif command -v sha256sum >/dev/null 2>&1; then
        hex=$(printf '%s' "$1" | sha256sum 2>/dev/null | awk '{print $1}')
    fi
    [ -n "$hex" ] && printf 'sha256:%s' "$hex" || return 1
}

# hook_received_fact <arrived_text> <msg_id> <emit_root>
# Run the SHIPPED receiver hook on a prompt built from the arrived text plus a
# routing trailer, then read back the `received` fact it emitted. Prints
# `<received_sha256> <received_bytes>` on success, nothing on any failure (no
# hook, no CLI to record with, no row) so the caller degrades to "unavailable"
# rather than a false disagreement. Exercises the real hook, never a copy.
hook_received_fact() {
    local arrived="$1" msgid="$2" root="$3"
    local hookp; hookp="$(dirname "${BASH_SOURCE[0]}")/plane-dispatch-in.sh"
    [ -f "$hookp" ] || return 1
    mkdir -p "$root/state/plane" 2>/dev/null || return 1
    printf '{"*": "full"}' > "$root/state/plane/capture.json" 2>/dev/null || return 1
    local prompt json
    prompt="set +H; ${arrived}"$'\n'"⟦plane:${msgid}⟧"
    json=$(printf '%s' "$prompt" | python3 -S -E -c \
        'import json,sys; print(json.dumps({"prompt": sys.stdin.read()}))' 2>/dev/null) || return 1
    printf '%s' "$json" | \
        CLAUDLOBBY_ROOT="$root" FLEET_NAME="sendprobe" BOT_ID="probe" \
        PLANE_EMIT_DISABLED="" bash "$hookp" >/dev/null 2>&1 || true
    local db="$root/state/plane/plane.db"
    [ -f "$db" ] || return 1
    python3 -S -E - "$db" "$msgid" <<'PYQ' 2>/dev/null || return 1
import json, sqlite3, sys
db, msgid = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
r = conn.execute("SELECT detail FROM events WHERE kind='transmission'"
                 " AND event='received' AND msg_id=?", (msgid,)).fetchone()
if not r or not r[0]:
    sys.exit(1)
d = json.loads(r[0])
print(d.get("received_sha256", ""), d.get("received_bytes", ""))
PYQ
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

# probe_child_command <claude_bin> <cwd> <cfg> <home> <env_dump>
# The shell command tmux runs in the probe pane, on stdout (chunk O fold, F4).
#
# BUILT FROM AN EXPLICIT BASE, never inherit-and-subtract — the #846 principle,
# and boot-strand-sampler.sh's run_start_bot ladder. Six variables cross into
# the child and nothing else does:
#   PATH                 the one deliberate inheritance (host tools; `claude`
#                        itself is resolved through it)
#   HOME                 a throwaway, so a stray write is not the operator's
#   TERM                 the TUI needs one to draw a box at all
#   LANG / LC_ALL        forwarded, with a UTF-8 default, so a pane launched
#                        from a C-locale shell still renders its box
#   CLAUDE_CONFIG_DIR    the seeded throwaway config
#   ANTHROPIC_BASE_URL   dead, so no API call can succeed
#
# Everything else is DROPPED, and three families are why: BOT_DIR /
# CLAUDLOBBY_ROOT / FLEET_NAME (a bot-session caller resolving the harness onto
# the live estate), ANTHROPIC_API_KEY (spend), and CLAUDE_CODE_USE_BEDROCK /
# CLAUDE_CODE_USE_VERTEX, which route around ANTHROPIC_BASE_URL and so made
# "this cannot spend" false for anyone who had one set.
#
# The pane dumps its environment before exec'ing, because the constructed
# ladder is a CLAIM until something reads back what the child actually got.
probe_child_command() {
    local bin="$1" cwd="$2" cfg="$3" home="$4" dump="$5"
    printf "cd '%s' && exec env -i PATH='%s' HOME='%s' TERM='%s' LANG='%s' LC_ALL='%s' CLAUDE_CONFIG_DIR='%s' ANTHROPIC_BASE_URL='%s' /bin/sh -c 'env > \"%s\"; exec \"%s\"'" \
        "$cwd" "$PATH" "$home" "${TERM:-xterm-256color}" \
        "${LANG:-C.UTF-8}" "${LC_ALL:-C.UTF-8}" "$cfg" \
        "http://127.0.0.1:9" "$dump" "$bin"
    return 0
}

# probe_env_leaks <env_text>
# The forbidden names that appear as assignments in <env_text>, one per line.
# Empty output means the isolation held.
#
# NAME PREFIXES as well as exact names: CLAUDE_CODE_USE_* is a family (Bedrock
# and Vertex today, whatever routes around the base URL next), and CLAUDLOBBY_*
# likewise. A list of exact names would go stale in the direction that reads
# clean, which is the only direction that matters here.
probe_env_leaks() {
    local text="$1" tok name restore_glob=""
    # Word-splitting without globbing: a value containing `*` must not expand
    # into this directory's filenames.
    case "$-" in *f*) ;; *) restore_glob=1 ;; esac
    set -f
    for tok in $text; do
        case "$tok" in *=*) ;; *) continue ;; esac
        name=${tok%%=*}
        case "$name" in
            ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|BOT_DIR|BOT_ID|FLEET_NAME|\
            TELEGRAM_BOT_TOKEN|GITHUB_PAT|CLAUDE_CODE_USE_*|CLAUDLOBBY_*)
                printf '%s\n' "$name" ;;
        esac
    done
    [ -z "$restore_glob" ] || set +f
    return 0
}

# probe_reap [socket_dir] — kill every leftover sendprobe tmux server and remove
# its socket file (chunk O fold, F7; coldstart-harness.sh `reap` precedent).
#
# The EXIT trap covers the ordinary paths, and SIGHUP is trapped now as well —
# a terminal closing on a 15-minute matrix used to leave a server holding a live
# `claude` and a socket an operator then has to tell apart from a bot's. This is
# the door for the ones that got away. Scoped to the `sendprobe` prefix by glob,
# which is this harness's own namespace and nothing else's.
probe_reap() {
    local dir="${1:-${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)}" sock base n=0
    for sock in "$dir"/sendprobe*; do
        [ -e "$sock" ] || continue
        base=$(basename "$sock")
        tmux -L "$base" kill-server 2>/dev/null
        rm -f "$sock" 2>/dev/null
        echo "  reaped: $base"
        n=$((n + 1))
    done
    echo "send-size-probe: reaped $n leftover probe server(s) under $dir"
    return 0
}

# ── the run (everything below needs a real host) ─────────────────────────────

ARM=both
REPS=3
SIZES_RAW="500 900 1100 1500 2100 3100 4200"
DEADLINE=25
FILLER=varied
KEEP=0
# --via-hook (chunk P, #1501): after each send, run the RECEIVER hook
# (plane-dispatch-in.sh) against the arrived text and assert the `received`
# fact it emits AGREES with this probe's own transcript verdict — the
# instrument checking the instrument. OFF by default; zero cost when unset.
VIA_HOOK=0
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
            --via-hook) VIA_HOOK=1; shift ;;
            --keep)     KEEP=1; shift ;;
            # Before every gate below: reaping needs no real `claude`, no
            # SEND_PROBE_REAL, and no dependency it might be waiting on — it
            # exists precisely for the run that did not finish.
            --reap)     probe_reap; return 0 ;;
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
    case "$FILLER" in varied|repeat|ident2) ;; *)
        echo "send-size-probe: --filler must be varied, repeat or ident2 (got '$FILLER')" >&2
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
    # HUP as well as EXIT/INT/TERM (chunk O fold, F7): a terminal closing on a
    # 15-minute matrix left a tmux server holding a live `claude` and a socket
    # file behind. `--reap` is the door for the ones that still get away.
    trap probe_cleanup EXIT INT TERM HUP

    local cwd="$PROBE_BASE/cwd" cfg="$PROBE_BASE/config" rows="$PROBE_BASE/rows.tsv"
    local home="$PROBE_BASE/home" envdump="$PROBE_BASE/child-env.txt"
    mkdir -p "$cwd" "$cfg" "$home" || return 3
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
    echo "  home:     $home (throwaway)"
    echo "  arms:     $ARM   reps: $REPS   deadline: ${DEADLINE}s   filler: $FILLER"
    echo "  sizes:    $(printf '%s ' $sizes)"
    echo "  chunking: cap=$_PANE_SEND_CHUNK_BYTES_DEFAULT settle=${PANE_SEND_CHUNK_SETTLE_S:-$_PANE_SEND_CHUNK_SETTLE_DEFAULT}s"
    echo ""

    bot_tmux "$PROBE_SOCK" new-session -d -s "$PROBE_SESSION" -x 200 -y 50 \
        "$(probe_child_command "$claude_bin" "$cwd" "$cfg" "$home" "$envdump")" \
        || { echo "send-size-probe: could not start the probe session" >&2; return 3; }

    # ── isolation, ASSERTED (chunk O fold, F4) ────────────────────────────
    # The pane writes its own environment before exec'ing `claude`, so this is
    # what the child GOT rather than what the ladder above intended. Read it
    # back, or refuse: a dump the probe cannot read is indistinguishable from an
    # isolation that silently did not happen, and only one of those two readings
    # is safe to act on.
    local waited=0
    while [ "$waited" -lt 10 ] && [ ! -s "$envdump" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    local child_env leaks
    child_env=$(cat "$envdump" 2>/dev/null || printf '')
    case "$child_env" in
        *ANTHROPIC_BASE_URL=*) ;;
        *)  echo "REFUSED (rc 3): could not read the probe child's environment." >&2
            echo "  The pane writes it to $envdump before exec'ing claude, and after" >&2
            echo "  ${waited}s it is absent or carries none of the variables the ladder sets." >&2
            echo "  Without it the isolation is a claim, not a measurement — and an" >&2
            echo "  unverified claim here is exactly the fallback it exists to catch." >&2
            return 3 ;;
    esac
    leaks=$(probe_env_leaks "$child_env")
    if [ -n "$leaks" ]; then
        echo "REFUSED (rc 3): the caller's environment reached the probe child." >&2
        echo "  Leaked: $(printf '%s' "$leaks" | tr '\n' ' ')" >&2
        echo "  This probe claims it cannot spend and cannot touch a fleet, and both" >&2
        echo "  claims rest on the constructed env in probe_child_command. A key or a" >&2
        echo "  CLAUDE_CODE_USE_* routing variable getting through makes the first" >&2
        echo "  false; a BOT_DIR or CLAUDLOBBY_ROOT makes the second." >&2
        return 3
    fi
    echo "isolation: constructed child env verified, no caller variables leaked"

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
    local vh_msgid vh_root vh_fact vh_psha vh_rsha vh_rbytes vh_agree
    local via_hook_disagree=0 via_hook_checked=0
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
                # chunk P: the receiver hook cross-check. Only when armed and
                # when there is an arrival to hand the hook.
                if [ "$VIA_HOOK" = "1" ] && [ "$verdict" != "absent" ]; then
                    vh_msgid="msg_$(printf '%s' "$tok" | { shasum -a 256 2>/dev/null || sha256sum 2>/dev/null; } | cut -c1-32)"
                    vh_root="$PROBE_BASE/viahook/$tok"
                    vh_fact=$(hook_received_fact "$got" "$vh_msgid" "$vh_root" || true)
                    vh_psha=$(payload_sha256 "$payload" || true)
                    if [ -n "$vh_fact" ] && [ -n "$vh_psha" ]; then
                        vh_rsha=${vh_fact%% *}; vh_rbytes=${vh_fact##* }
                        vh_agree=$(via_hook_agrees "$verdict" "$vh_psha" "$size" "$vh_rsha" "$vh_rbytes")
                        via_hook_checked=$((via_hook_checked + 1))
                        [ "$vh_agree" = "disagree" ] && via_hook_disagree=$((via_hook_disagree + 1))
                        printf '        via-hook: %-8s (hook received %s bytes of %s sent)\n' \
                            "$vh_agree" "$vh_rbytes" "$size"
                    else
                        printf '        via-hook: unavailable (no recorder or no fact)\n'
                    fi
                fi
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
    if [ "$VIA_HOOK" = "1" ]; then
        printf 'via-hook cross-check: %s checked, %s disagreed\n' \
            "$via_hook_checked" "$via_hook_disagree"
        # A disagreement is a real finding — the hook read something other than
        # what the transcript verdict says arrived — so the run fails loud.
        [ "$via_hook_disagree" -eq 0 ] || return 1
    fi
    return 0
}

# Guarded so the pure helpers above can be sourced by tests/test_send_size_probe.py
# without booting anything.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
    exit $?
fi
