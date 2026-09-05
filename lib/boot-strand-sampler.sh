#!/usr/bin/env bash
# boot-strand-sampler.sh — #843 real-boot STARTUP_PROMPT strand sampler.
#
# Measures the post-#837 boot-strand rate by driving N GENUINE boots — the real
# `claude` TUI, started by the real lib/start-bot.sh, in a real tmux session on
# a private socket — of a disposable bot composed into a throwaway root
# (freshbox-boot-gate.sh precedent). validate-bot-change.sh cannot answer this
# question by construction: it stubs bin/claude with `exec cat`, replacing the
# exact component whose readiness timing causes the strand (#843).
#
# Per-boot classification, three independent evidence sources:
#   clean  — the session transcript (CLAUDE_CONFIG_DIR/projects/*/*.jsonl)
#            contains a USER-role record carrying the probe marker: the prompt
#            became a submitted message. Ground truth independent of pane
#            geometry, so a transcript echo can never fake a verdict.
#   strand — classification deadline passed with no submitted record AND the
#            input box still holds the payload, judged by pane_holds_unsubmitted
#            — the #837 primitive itself: anchored to the LAST prompt-glyph
#            line (a submitted command is echoed into the transcript with the
#            same glyph, so first-match reads a healthy pane as stranded), with
#            the collapsed-paste placeholder branch.
#   other  — neither (session died, auth wall, start-bot failure). Counted and
#            reported separately; NEVER folded into clean.
# A clean boot whose ledger gained a send_retry event is counted clean_via_retry
# — the #837 retry visibly doing its job on a send that would have stranded.
#
# ── What this measures vs production (the #843 acceptance criterion) ──────────
# The probe bot has MCP parity with a production worker (alex-shaped): the
# github MCP server with a real token when GITHUB_PAT is in the caller env, the
# telegram channel plugin in --channels (its MCP server spawns), the default
# plugins (claudna + superpowers, warm-copied from the host cache so versions
# match production), a wired claudron vault (the three session-loop hooks), and
# a software-engineering expertise CLAUDE.md. Divergences, stated plainly:
#   1. TOKENLESS CANARY. No spare Telegram bot token exists (every token in the
#      estate belongs to a production bot; sharing one steals its getUpdates).
#      The probe declares EXPECT_NO_TOKEN=1, so start-bot's readiness gate
#      short-circuits instead of waiting for poller-up (3–9s across production
#      startup.logs). STARTUP_PROMPT is therefore injected EARLIER into a
#      COLDER TUI than production — at least as hard on the send race under
#      test, so a strand-free sample is not explained by an easier condition.
#      The poller's own network phase is the one boot component not sampled,
#      and a tokenless bridge does not persist as a live process.
#   2. SERIAL BOOTS. Production strands were observed on one-at-a-time
#      restarts, which this reproduces; the mass-restart contention path
#      (BOOT_LOCK held by peers) is not sampled. --load N closes the CPU half
#      of that gap and stays pinned — as a RATE AMPLIFIER, which is what makes
#      a 30-45 min run yield any traced strands at all, NOT because the strand
#      fails to occur at low load. Read #933's boot-shape table by its strand
#      signature (box still holding at +25s): it stranded 5 of 5 at loadavg
#      18.7-30.7, and the clean-at-loadavg-~10 row it is usually cited for is
#      ONE boot at 10.3 — the only row in that table whose payload actually
#      ran. Its five idle boots at loadavg 1.7 are not a clean baseline
#      either: the payload never ran AND the box never held it, so they are
#      not valid trials of the send in either direction. So the low-load
#      evidence is a single boot: it supports "not observed in one sample",
#      never "does not occur". Per the pass-bar ratified on #1236
#      (Clopper-Pearson exact, 90% one-sided throughout — every n here moves
#      if you recompute on another basis, so the basis travels with the
#      number), a zero-observation bounds the rate at 90% for n=1 and still
#      31.9% at n=6, and ELIMINATION — an upper bound under 10% — is
#      unreachable below n=22. The strand has since been seen at low load
#      twice: a restart stranded at loadavg ~10, and a production strand lower
#      still. So the LOW-LOAD STRATUM IS UNSAMPLED, NOT EMPTY. A sample taken
#      without --load still says little about the contended boot; a
#      strand-free one bounds the low-load rate loosely rather than showing
#      it is zero.
#   3. The per-boot process ledger (parity_procs: every descendant of the pane)
#      is recorded so parity is EVIDENCED per boot, not asserted — the summary
#      prints the tree histogram.
#
# Summary statistics (lib/boot-strand-summary.py, stdlib-only): exact
# Clopper–Pearson 95% interval on the strand rate, printed next to the pre-fix
# baseline — which is itself only 2 strands in n=4, so the null is poorly
# estimated and no sample size makes the fix "proven"; the interval is the
# result, not a verdict.
#
# Usage: boot-strand-sampler.sh [-n N] [--arms "A B C"] [--seed K]
#                               [--deadline SECS] [--load N] [--keep]
#   -n N             sample size, default 20 (a warm-up boot runs first and is
#                    reported separately, never counted). With --arms this is
#                    the number of BLOCKS, so the run is N x len(arms) boots
#                    plus ONE warm-up for the whole run, not one per arm.
#   --arms "A B C"   INTERLEAVE MODE. Settle values (seconds) to sweep, space-
#                    or comma-separated. One block = one boot per arm, arm
#                    order randomized WITHIN each block, blocks run back to
#                    back. This is what pre-registration v2 §4 BASELINE
#                    requires and what running one arm to completion and then
#                    the next cannot give: ambient load on a shared host swings
#                    ±40% unaided, so an arm-sequential run assigns each arm a
#                    different hour of ambient conditions and calls the
#                    difference a treatment effect.
#                    Refuses if PANE_SEND_SETTLE_S is also set in the caller
#                    env — two sources for one fact is how a row comes to
#                    disagree with the condition it ran under.
#   --seed K         seed for the within-block arm order. Default: chosen per
#                    run and PRINTED, because an order nobody can restate is a
#                    covariate the artifact cannot evidence. Recorded in every
#                    row, so a re-run at the same seed repeats the sequence.
#   --deadline SECS  per-boot classification deadline, default 120. Healthy
#                    boots submit in seconds and strands never resolve (#843
#                    timing evidence), so the gap tolerates a generous value.
#   --load N         run the sample against N synthetic CPU burners, to sample
#                    the CONTENDED boot rather than the idle one (see divergence
#                    2 above). N is a burner count, not a target loadavg: on the
#                    4-core reference host N=20 settles near loadavg 25, which
#                    brackets the 23.7-31 measured during the incident. Burners
#                    are hard-timeout'd and killed by recorded PID, so neither a
#                    crash of this script nor a pattern match on its own command
#                    line can leave the host loaded.
#   --keep           keep $ROOT artifacts (secrets are scrubbed either way)
# Env: CLAUDLOBBY_SRC (checkout under test, default: this script's repo),
#      CLAUDE_BIN (default: real `claude` — the point), SAMPLER_MEM_FLOOR_MB
#      (default 1200; refuses to run on a starved host, which would both risk
#      the live fleet and bias readiness timing).
#      Pane knobs PANE_SEND_VERIFY_TICKS / PANE_SEND_SETTLE_S /
#      PANE_READY_TICKS forward into the boot when set; the "pane knobs:"
#      output line states each one's effective fate — forwarded, default,
#      INERT (PANE_READY_TICKS: start-bot arms its own value at the injection
#      sites), or SCRUBBED (a lib-common pane knob env -i drops).
#
# ARM IDENTITY. Every emitted row carries `arm_knobs` (each forwarded knob as
# {env, v, src}) plus the hoisted `settle_s`, resolved AT THE BOOT from the
# live environment rather than from a caller-supplied label. Without it a
# settle=0.3 row and a settle=6.0 row are byte-identical in shape, arm identity
# lives only in a filename, and a mislabelled arm is undetectable from the
# artifact afterwards — so boot-strand-summary.py refuses a sample it cannot
# attribute rather than pooling it.
#
# PAIRING (--arms). Without --arms one invocation samples ONE arm, and a ladder
# assembled by concatenating those rows files is arm-sequential — which
# pre-registration v2 §4 BASELINE forbids by name. --arms runs the whole ladder
# in one invocation as interleaved blocks instead.
#
# THE ROWS MUST EVIDENCE THE ALTERNATION, NOT ASSERT IT. `block`, `pos` and
# `arm_order` are written by the same loop that does the interleaving, so on
# their own they are a claim, not a measurement: if the interleave broke, the
# labels would still read exactly as they do now. What makes them checkable is
# that `settle_s` is resolved per boot FROM THE LIVE ENVIRONMENT (above), so
# the observed sequence of in-force arms over `i` is an independent record of
# what actually alternated. boot-strand-summary.py derives the pairing from
# that sequence and refuses a block record the in-force arms contradict — the
# same shape as `_covarying_knobs` guarding the CONDITION rather than the
# label. A check whose only evidence is the claim it is checking cannot fail,
# and this ladder exists to measure exactly that defect class.
#
# Exit: 0 sample completed (the summary is the product; strands do not fail
#         the run) · 1 harness failure (setup assertion failed, or zero boots
#         reached a clean/strand verdict — a sample of others-only must not
#         read as a measurement) · 2 precondition/dep missing (skip) ·
#         3 the sample cannot be attributed to arms, or its block record
#           contradicts the arms in force (summary refused).

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(dirname "$LIB_DIR")"
# shellcheck source=/dev/null
. "$LIB_DIR/lib-common.sh"

CLAUDLOBBY_SRC="${CLAUDLOBBY_SRC:-$SRC_ROOT}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
HOST_CREDS="${HOME}/.claude/.credentials.json"
HOST_PLUGINS="${HOME}/.claude/plugins"

BOOTS=20
DEADLINE=120
KEEP=""
POLL_S=2
LOAD_BURNERS=0
MEM_FLOOR_MB="${SAMPLER_MEM_FLOOR_MB:-1200}"

# Interleave mode: the ladder's arms, and the seed for within-block order.
# Empty ARMS keeps the single-arm behaviour (the caller sets the knob).
ARMS=""
# Which knob --arms values address. "settle" is the original ladder; "trace"
# (#1236) interleaves instrumentation ON against OFF at a FIXED settle, which is
# the control for "instrumenting a race can move it". Same shuffle, same
# blocking, same per-boot in-force recording -- only the knob differs.
ARM_AXIS="settle"
SEED=""

# PIDs of the synthetic-load burners, so teardown targets what this run started
# and nothing else. Killing by recorded PID rather than `pkill -f <pattern>` is
# deliberate: the pattern also matches the command line of whatever invoked the
# sampler, so a pattern kill can take out its own caller.
_LOAD_PIDS=""

# The arm record for the boot currently in flight — see arm_knobs_json. The
# boot loop sets it just before each boot; the row emitter reads it.
_ARM_KNOBS_JSON=""

# The probe marker is the submission ground truth: greppable in the session
# JSONL user record. It no longer needs to sit inside the first rendered line —
# that constraint came from the retired 60-char prefix probe, and #1082 replaced
# the prefix with reversed containment over the full payload.
MARKER="BSPROBE_843"
STARTUP_PROMPT_TEXT="Boot probe ${MARKER}: reply with exactly BSPROBE_ACK and nothing else. Do not use any tools. Do not post to any channel. Then wait silently."

# The Usage/Env/Exit block from the header, ended by the first non-comment line
# so a reworded header cannot make this print the whole file.
usage() {
    awk '/^# Usage:/ { f = 1 } f { if (!/^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
    return 0
}

# ── pure helpers (sourceable for unit tests: guarded main at the bottom) ──────

# list_descendants <root_pid> — "pid comm" lines for every live descendant.
# One ps pass + an awk closure walk: kill/parity logic must see the WHOLE tree
# (MCP servers are grandchildren via shims), and ppid==direct-child misses them.
# comm is rejoined from field 3 to NF and basenamed — macOS ps reports a full
# path that may contain spaces (the orphan-browser-reaper precedent).
list_descendants() {
    ps -e -ww -o pid=,ppid=,comm= | awk -v root="$1" '
        {
            pid[NR] = $1; ppid[NR] = $2
            c = $3; for (j = 4; j <= NF; j++) c = c " " $j
            sub(/.*\//, "", c); comm[NR] = c
        }
        END {
            found[root] = 1; changed = 1
            while (changed) {
                changed = 0
                for (i = 1; i <= NR; i++)
                    if (!(pid[i] in found) && (ppid[i] in found)) {
                        found[pid[i]] = 1; changed = 1
                    }
            }
            for (i = 1; i <= NR; i++)
                if (pid[i] in found && pid[i] != root) print pid[i], comm[i]
        }'
    return 0
}

# submitted_evidence <config_dir> <newer_than_file> <marker>
# rc 0 iff a session transcript newer than the boot marker holds a USER-role
# record containing <marker> — the prompt was genuinely submitted. Assistant
# records are excluded so a model echo of the marker can never count.
# Glob + builtin -nt, not find(1): this runs every poll tick, and fork churn
# during the boot under measurement is the perturbation lib-common warns about.
# The jq re-parse per tick is an accepted O(file-size x ticks) bound — offset
# bookkeeping is not worth it under a deadline-bounded loop.
submitted_evidence() {
    local cfg="$1" newer="$2" marker="$3" f hit=""
    for f in "$cfg"/projects/*/*.jsonl; do
        [ -f "$f" ] && [ "$f" -nt "$newer" ] || continue
        grep -q -- "$marker" "$f" 2>/dev/null || continue
        hit="$(jq -rc --arg m "$marker" \
            'select(.type=="user") | (.message.content | tostring) | select(contains($m)) | "hit"' \
            "$f" 2>/dev/null | head -1)" || true
        [ "$hit" = "hit" ] && return 0
    done
    return 1
}

# final_verdict <pane_text> <probe> — the no-submission-evidence outcomes.
# Reached only after the classification loop found no submitted record (the
# clean verdict is decided there, by transcript ground truth): the #837
# unsubmitted-payload judgment maps to strand, anything else to other.
final_verdict() {
    if pane_holds_unsubmitted "$1" "$2"; then
        printf 'strand'
    else
        printf 'other:no_evidence'
    fi
    return 0
}

# mem_available_mb — MemAvailable in MB on Linux; empty (no check) elsewhere.
mem_available_mb() {
    awk '/^MemAvailable:/ { printf "%d", $2 / 1024 }' /proc/meminfo 2>/dev/null
    return 0
}

# loadavg_1m — 1-minute load average, or empty where /proc is absent (macOS).
# Recorded per boot rather than assumed from the burner count: burners are an
# INPUT, loadavg is what the host actually experienced, and on a shared host the
# live fleet contributes too. A sample that reports only "--load 20" cannot be
# compared against an incident that reported a loadavg.
loadavg_1m() {
    awk '{ print $1 }' /proc/loadavg 2>/dev/null
    return 0
}

# start_load_burners <n> <max_secs> — spawn N hard-timeout'd CPU burners.
# The timeout is a backstop, not the mechanism: teardown kills by PID. It exists
# so a SIGKILL of the sampler (which skips the EXIT trap) still cannot leave a
# shared host loaded indefinitely.
start_load_burners() {
    local n="$1" max="$2" i
    [ "$n" -gt 0 ] 2>/dev/null || return 0
    for i in $(seq 1 "$n"); do
        "$_TIMEOUT_BIN" "$max" bash -c 'while :; do :; done' >/dev/null 2>&1 &
        _LOAD_PIDS="$_LOAD_PIDS $!"
    done
    return 0
}

# stop_load_burners — kill exactly the PIDs this run recorded. Idempotent, and
# safe to call from a trap that may fire more than once.
stop_load_burners() {
    local p
    for p in $_LOAD_PIDS; do
        kill "$p" 2>/dev/null || true
    done
    _LOAD_PIDS=""
    return 0
}

# count_send_retries <bot_dir> — the probe bot's send_retry events on the PLANE
# (F18 closure R2b-2: start-bot.sh lands them through emit_fleet_event; the
# per-bot event files are gone). The root is the probe root the bot dir sits in
# and the fleet is the composed bot.conf FLEET_NAME. Prints the count. Prints
# NOTHING and returns 1 when the plane cannot answer, with the reason on stderr:
# an outage must never read as "no retry fired" — the caller records the boot's
# retry as UNKNOWN (null in the row) rather than 0, and the summarizer says so.
count_send_retries() {
    local bot_dir="$1" root fleet rows _rc=0
    root="${bot_dir%/runtime/bots/*}"
    fleet="$(sed -n 's/^FLEET_NAME="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$bot_dir/bot.conf" 2>/dev/null | head -1)"
    fleet="${fleet:-${PROBE_FLEET:-boot-sampler}}"
    rows="$(mktemp "${TMPDIR:-/tmp}/bss-retries.XXXXXX")" || return 1
    python3 -S -E "$root/lib/plane-lookup.py" --root "$root" --events --fleet "$fleet" \
        --bot "$(basename "$bot_dir")" --type send_retry > "$rows" 2>"$rows.err" || _rc=$?
    if [ "$_rc" -ne 0 ]; then
        printf 'count_send_retries: the plane could not answer (rc=%s): %s -- retry count UNKNOWN for this boot\n' \
            "$_rc" "$(tail -1 "$rows.err" 2>/dev/null | cut -c1-160)" >&2
        rm -f "$rows" "$rows.err"
        return 1
    fi
    wc -l < "$rows" | tr -d ' '
    rm -f "$rows" "$rows.err"
    return 0
}

# ── #1109 knob contract: what the constructed child env forwards ──────────────
# The pane knobs lib-common reads from the environment, split by whether
# run_start_bot forwards them. tests/test_boot_strand_sampler.py pins the UNION
# of these lists against lib-common's actual `${PANE_*}` env reads, so a new
# knob fails CI here instead of joining the scrubbed-silently class (#1084).
#
# Forwarded — the measurement knobs. PANE_SEND_VERIFY_TICKS bounds the
# stuck-payload verify loop (#1084); PANE_SEND_SETTLE_S bounds the repaint
# window between send-keys and Enter (the #1109 hypothesis knob);
# PANE_READY_TICKS governs the pre-send box wait, forwarded so an arming change
# in start-bot.sh starts honoring it with no sampler change — but INERT today:
# start-bot.sh arms its own value at both pane_send_verified call sites
# (start-bot.sh:371,381 as of #1109), so a forwarded override cannot reach the
# injection path. knob_disclosure says so in the output.
#
# Unforwarded — no pre-registered ladder sweeps them. Set in the caller env
# they are dropped by env -i, and knob_disclosure prints them as SCRUBBED
# rather than letting the run silently measure defaults.
_FORWARDED_PANE_KNOBS="PANE_SEND_VERIFY_TICKS PANE_SEND_SETTLE_S PANE_READY_TICKS PANE_VERIFY_TRACE"
_UNFORWARDED_PANE_KNOBS="PANE_READY_POLL_S PANE_RECOVER_TICKS"

# Field separator for the fate records below: ASCII unit separator, NOT a tab.
# Tab is an IFS-whitespace character, so `IFS=<tab> read` collapses adjacent
# delimiters and an unset knob (an empty field) silently shifts every field
# after it left — which read a default-valued knob as UNRESOLVED. \037 is not
# IFS whitespace, so empty fields survive, and it cannot occur in a knob value.
_FS=$'\037'

# pane_knob_fate <knob> — one TSV line: knob, the value put in the CHILD ENV
# (empty when unset), the value ACTUALLY IN FORCE on the measured path, and the
# fate that produced it. The single source both consumers read: knob_disclosure
# renders it for a human, arm_knobs_json records it in the row. Two renderings
# of one resolution cannot disagree, which is the same reason forwarding is
# derived from _FORWARDED_PANE_KNOBS rather than restated (#1084, #1109).
#
# Three fates, and the third is why "what the caller set" is the wrong thing to
# record:
#   forwarded   caller set it, env -i carries it, lib-common reads it
#   default     caller left it unset; the lib-common default governs
#   boot-armed  start-bot.sh arms its own value at BOTH injection sites, so
#               neither of the above reaches the measured path
# Recording caller intent would label a PANE_READY_TICKS=200 run as a 200 arm
# when every boot in it ran at _PANE_READY_TICKS_BOOT.
#
# In-force values come from the lib-common constants, never a literal copied
# here: a default that moves in lib-common must move the recorded arm with it,
# or every historical row silently re-describes a condition that changed.
# The case decides ONLY what differs per knob — its default constant, or that
# it has no reachable default at all. The forwarded-vs-default rule itself is
# written once, below the case, so it cannot come to mean two things.
pane_knob_fate() {
    local knob="$1" set_val default_val="" inforce="" src=""
    eval "set_val=\${$knob:-}"
    case "$knob" in
        # start-bot.sh arms its own value at BOTH injection sites, so neither
        # the caller value nor the lib-common default reaches the measured path.
        PANE_READY_TICKS)       inforce="$_PANE_READY_TICKS_BOOT"; src="boot-armed" ;;
        PANE_SEND_SETTLE_S)     default_val="$_PANE_SEND_SETTLE_DEFAULT" ;;
        PANE_SEND_VERIFY_TICKS) default_val="$_PANE_SEND_VERIFY_TICKS_DEFAULT" ;;
        # #1236. This knob has no default VALUE: unset IS off, and off is the
        # production condition. Recording it as `default` would assert a
        # fallback constant lib-common does not have, and
        # test_default_fate_tracks_the_constant_lib_common_ACTUALLY_READS is
        # right to reject that -- so it gets its own fate instead of borrowing
        # one that would be a small lie about where the value came from.
        PANE_VERIFY_TRACE)
            # In-force is recorded as 1/0, NOT the path. The experimental
            # condition is trace-on-ness; the path is an implementation detail
            # that differs per boot by construction (it is per-boot), so
            # recording it as the value would make every trace-on boot its own
            # arm and trip the covarying-knob guard forever. The path is still
            # carried in `env` for provenance.
            if [ -n "$set_val" ]; then inforce="1"; src="forwarded"
            else inforce="0"; src="off"; fi
            ;;
        # A knob joining _FORWARDED_PANE_KNOBS without a branch here would
        # otherwise be recorded as if it had no fate — the scrubbed-silently
        # class one level up. Loud, and pinned by the test suite.
        *)                      src="UNRESOLVED" ;;
    esac
    if [ -z "$src" ]; then
        if [ -n "$set_val" ]; then
            inforce="$set_val"; src="forwarded"
        else
            inforce="$default_val"; src="default"
        fi
    fi
    printf '%s\n' "$knob$_FS$set_val$_FS$inforce$_FS$src"
    return 0
}

# arm_knobs_json — the per-boot ARM RECORD: every forwarded knob as
# {env, v, src}. Its SHAPE is derived from _FORWARDED_PANE_KNOBS, so a knob
# added to the list appears in the row automatically; its FATE still needs a
# branch in pane_knob_fate, and a knob without one lands as UNRESOLVED and
# fails CI (test_every_forwarded_knob_has_a_declared_fate) rather than
# recording a null in-force value that reads like a measurement.
#
# This is what closes "the row cannot evidence its own IV": without it a
# settle=0.3 boot and a settle=6.0 boot are byte-identical in shape, arm
# identity lives only in a filename, and a mislabelled arm is undetectable
# from the artifact afterwards. No pipefail suppression: a row that cannot
# record its arm must abort the run, never be written bare.
arm_knobs_json() {
    local k
    for k in $_FORWARDED_PANE_KNOBS; do
        pane_knob_fate "$k"
    done | jq -R -s --arg fs "$_FS" '
        split("\n") | map(select(length > 0)) | map(split($fs))
        | map({key: .[0], value: {
              env: (if .[1] == "" then null else .[1] end),
              v:   (.[2] | if . == "" then null else (try tonumber catch .) end),
              src: .[3]}})
        | from_entries'
}

# ── interleaved blocks (--arms) ───────────────────────────────────────────────
#
# Pre-registration v2 §4 BASELINE: "Pairing: interleaved blocks, randomized arm
# order within block. One block = one boot per arm." What it rules out by name
# is running every boot of arm A and then every boot of arm B, because ambient
# load on a shared host swings 9.7 -> 17.7 within minutes unaided, so that
# design hands each arm a different hour of conditions and reports the
# difference as a treatment effect.

# _lcg_next — advance the generator IN PLACE.
#
# It mutates _LCG_STATE rather than echoing, and that is load-bearing rather
# than style: command substitution forks a subshell, so `j=$(_lcg_next)` would
# discard every advance and hand back one fixed value forever. The shuffle
# would then return a single permutation for every block while block/pos went
# on labelling the rows interleaved — a broken interleave wearing a correct
# label, which is the exact defect class this mode exists to make detectable.
# No caller may wrap it in $( ).
#
# A specified LCG rather than bash $RANDOM, also deliberate: bash changed its
# own PRNG at 5.1, so a recorded seed would replay one order on this host and a
# different one anywhere else, and an arm order that cannot be restated from
# the artifact is a covariate the run cannot evidence. Constants are the glibc
# ones; the modulus keeps every intermediate inside signed 64-bit.
_LCG_STATE=0
_lcg_next() {
    _LCG_STATE=$(( (_LCG_STATE * 1103515245 + 12345) % 2147483648 ))
    return 0
}

# shuffle_arms <arm>... — Fisher-Yates over the arguments, driven by _lcg_next.
# Result lands in _SHUFFLED (space separated) for the same subshell reason.
_SHUFFLED=""
shuffle_arms() {
    local a=("$@")
    local n=${#a[@]} i j tmp
    i=$((n - 1))
    while [ "$i" -gt 0 ]; do
        _lcg_next
        j=$(( _LCG_STATE % (i + 1) ))
        tmp="${a[$i]}"; a[$i]="${a[$j]}"; a[$j]="$tmp"
        i=$((i - 1))
    done
    _SHUFFLED="${a[*]}"
    return 0
}

# build_boot_plan <blocks> <arm>... — the whole run as one ordered list, in
# _BOOT_PLAN. Each entry is kind, block, pos, arm, arm-order, _FS separated;
# an empty arm means "do not touch the knob", which is what keeps single-arm
# mode byte-identical to its pre-interleave behaviour.
#
# One loop drives both modes rather than two loops that could drift apart: the
# mode difference is entirely in the plan, so classification, teardown and row
# emission cannot come to mean two things.
_BOOT_PLAN=()
build_boot_plan() {
    local blocks="$1"; shift
    local arms=("$@")
    local b=0 p n=1 a ord
    _BOOT_PLAN=()
    if [ "${#arms[@]}" -eq 0 ]; then
        _BOOT_PLAN+=("warmup$_FS$_FS$_FS$_FS")
        while [ "$n" -le "$blocks" ]; do
            _BOOT_PLAN+=("sample$_FS$_FS$_FS$_FS")
            n=$((n + 1))
        done
        return 0
    fi
    # ONE warm-up for the whole run. It absorbs first-boot cost (plugin
    # install, cold caches), which is arm-independent — paying it per
    # invocation is exactly what makes a per-block driver cost 6 boots for 3
    # samples and doubles the matrix. It runs at the first arm and is excluded
    # from the sample, so the choice cannot bias a rate.
    _BOOT_PLAN+=("warmup$_FS$_FS$_FS${arms[0]}$_FS")
    while [ "$b" -lt "$blocks" ]; do
        shuffle_arms "${arms[@]}"
        ord="$_SHUFFLED"
        p=0
        for a in $ord; do
            _BOOT_PLAN+=("sample$_FS$b$_FS$p$_FS$a$_FS$ord")
            p=$((p + 1))
        done
        b=$((b + 1))
    done
    return 0
}

# knob_disclosure — one line stating the effective fate of every pane knob the
# child boot could consume, so a run artifact self-documents which knob a
# ladder actually varied. An override whose fate is knowable only by reading
# two sources is how a refutation gets manufactured (#1084, #1109); this line
# is the output-visible half of the fix. Printed before the precondition
# gates, so even an aborted run carries it.
knob_disclosure() {
    local k v line="pane knobs:" knob envv inforce src
    while IFS="$_FS" read -r knob envv inforce src; do
        case "$src" in
            boot-armed)
                if [ -n "$envv" ]; then
                    line="$line $knob=$envv(forwarded-but-INERT:start-bot-arms-$inforce)"
                else
                    line="$line $knob=$inforce(default:boot-armed)"
                fi ;;
            forwarded) line="$line $knob=$envv(forwarded)" ;;
            default)   line="$line $knob=$inforce(default)" ;;
            *)         line="$line $knob=$envv(UNRESOLVED:no-fate-declared)" ;;
        esac
    done <<EOF
$(for k in $_FORWARDED_PANE_KNOBS; do pane_knob_fate "$k"; done)
EOF
    for k in $_UNFORWARDED_PANE_KNOBS; do
        eval "v=\${$k:-}"
        if [ -n "$v" ]; then
            line="$line $k=$v(SCRUBBED:not-on-the-env-allowlist)"
        fi
    done
    printf '%s\n' "$line"
    return 0
}

# run_start_bot <timeout_s> <root> <bot_dir> — start-bot under a CONSTRUCTED
# child env: built from an explicit base, never inherit-and-subtract (the #846
# principle; a shared lib-common seam for the estate's six hand-rolled copies
# is a named follow-up). Production start-bot runs under systemd with a clean
# environment; a bot-session caller instead carries its OWN exported
# TELEGRAM_BOT_TOKEN + TELEGRAM_TOKEN_ENV_NAME, and inherited they make the
# probe resolve a PRODUCTION token — the readiness gate then waits the full
# ceiling for a poller that must never exist, and the probe bridge could steal
# a live bot's getUpdates (caught live on this sampler's first smoke run).
# PATH is the one deliberate inheritance (host tools); everything else the boot
# needs comes from bot.conf and the .env tiers, exactly as production sources
# them.
run_start_bot() {
    local timeout_s="$1" root="$2" bot_dir="$3"
    # Forwarding is DERIVED from _FORWARDED_PANE_KNOBS (rationale at the list),
    # so list membership IS forwarding and knob_disclosure can never call a
    # knob forwarded while this function scrubs it — that drift is the
    # manufactured-refutation class itself (#1084, #1109). Set-and-nonempty
    # only: an empty value would shadow the lib-common default and reach an
    # arithmetic test as a non-integer.
    local k v
    local knob_env=()
    for k in $_FORWARDED_PANE_KNOBS; do
        eval "v=\${$k:-}"
        if [ -n "$v" ]; then
            knob_env+=("$k=$v")
        fi
    done
    # timeout wraps env(1), a real command — with_timeout cannot exec a shell
    # function, so the bound lives here rather than at the call site.
    with_timeout "$timeout_s" env -i \
        HOME="$HOME" \
        PATH="$PATH" \
        LANG="C.UTF-8" \
        TERM="${TERM:-xterm-256color}" \
        USER="${USER:-$(id -un)}" \
        LOGNAME="${LOGNAME:-$(id -un)}" \
        TMPDIR="${TMPDIR:-/tmp}" \
        CLAUDLOBBY_ROOT="$root" \
        "${knob_env[@]+"${knob_env[@]}"}" \
        bash "$root/lib/start-bot.sh" "$bot_dir"
}

# emit_summary <rows> — print the summary and EXIT with the summarizer's status.
#
# Extracted from main solely to be TESTABLE (#1141). The refusal it carries is
# the safety property of the whole arm-identity mechanism: boot-strand-summary.py
# exits 3 rather than pool a sample it cannot attribute to arms, and that refusal
# protects nothing unless a caller notices it. Swallowing the status here turns
# "this sample is not attributable and no rate is printed" into a silent success —
# the exact failure the refusal exists to prevent, one layer down. Reaching this
# code through main means passing every dependency gate and a real boot loop, so
# without a seam the propagation was unreachable by any test, and a mutation of
# it left the whole suite green.
#
# Exit code PROPAGATED, not flattened to 1: the summarizer distinguishes
# "measured nothing" (1) from "refuses to pool a mislabelled sample" (3), and
# collapsing them would hide the one that means the artifact is wrong rather
# than empty.
#
# The `|| rc=$?` is load-bearing under `set -e` (:102) and must not be
# simplified to a bare call plus `exit $?`: a legitimate refusal would then
# abort through the ERR path instead of returning its own verdict.
emit_summary() {
    local rc=0
    # The axis the run varied has to be the axis the analysis groups on, or the
    # attribution guard reports every boot as one mislabelled arm -- which is
    # exactly what it did, correctly, on the first #1236 control attempt.
    local _iv=()
    [ "$ARM_AXIS" = "trace" ] && _iv=(--iv trace)
    python3 "$LIB_DIR/boot-strand-summary.py" ${_iv[@]+"${_iv[@]}"} "$1" || rc=$?
    exit "$rc"
}

# ── the sampler ───────────────────────────────────────────────────────────────

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -n)         BOOTS="${2:?-n needs a value}"; shift 2 ;;
            --arms)     ARMS="${2:?--arms needs a value}"; shift 2 ;;
            --trace-arms)
                        ARMS="${2:?--trace-arms needs a value}"; ARM_AXIS="trace"; shift 2 ;;
            --seed)     SEED="${2:?--seed needs a value}"; shift 2 ;;
            --deadline) DEADLINE="${2:?--deadline needs a value}"; shift 2 ;;
            --load)     LOAD_BURNERS="${2:?--load needs a value}"; shift 2 ;;
            --keep)     KEEP=1; shift ;;
            -h|--help)  usage; exit 0 ;;
            *)          printf 'unknown arg: %s\n' "$1" >&2; usage >&2; exit 1 ;;
        esac
    done
    case "$BOOTS" in ''|*[!0-9]*) printf 'bad -n: %s\n' "$BOOTS" >&2; exit 1 ;; esac
    case "$DEADLINE" in ''|*[!0-9]*) printf 'bad --deadline: %s\n' "$DEADLINE" >&2; exit 1 ;; esac
    case "$LOAD_BURNERS" in ''|*[!0-9]*) printf 'bad --load: %s\n' "$LOAD_BURNERS" >&2; exit 1 ;; esac

    # ── interleave mode: validate the ladder before anything expensive ────────
    local arm_list=() a b
    if [ -n "$ARMS" ]; then
        # One fact, two sources. In interleave mode the boot loop OWNS
        # PANE_SEND_SETTLE_S; a caller value would be overwritten on every boot
        # while knob_disclosure still called it forwarded — a disclosure that
        # describes a number governing nothing is the #1084 class.
        # The refusal names the knob THIS axis owns. On the trace axis the loop
        # owns PANE_VERIFY_TRACE and leaves settle alone, so guarding settle
        # there would refuse a legitimate run and miss the real collision.
        if [ "$ARM_AXIS" = "trace" ]; then
            if [ -n "${PANE_VERIFY_TRACE:-}" ]; then
                printf 'REFUSED: --trace-arms sweeps PANE_VERIFY_TRACE, but it is also set in the environment (%s).\n' \
                    "$PANE_VERIFY_TRACE" >&2
                printf '  One fact, two sources. Unset it, or drop --trace-arms.\n' >&2
                exit 1
            fi
        elif [ -n "${PANE_SEND_SETTLE_S:-}" ]; then
            printf 'REFUSED: --arms sweeps PANE_SEND_SETTLE_S, but it is also set in the environment (%s).\n' \
                "$PANE_SEND_SETTLE_S" >&2
            printf '  One fact, two sources. Unset it, or drop --arms to sample that single arm.\n' >&2
            exit 1
        fi
        for a in $(printf '%s' "$ARMS" | tr ',' ' '); do
            if [ "$ARM_AXIS" = "trace" ]; then
                case "$a" in
                    on|off) ;;
                    *) printf 'bad --trace-arms value: %s (expected "on off")\n' "$a" >&2; exit 1 ;;
                esac
            else
            case "$a" in
                ''|*[!0-9.]*|*.*.*|.)
                    printf 'bad --arms value: %s (settle seconds, e.g. "0.3 1.5 6.0")\n' "$a" >&2; exit 1 ;;
            esac
            # Normalised through the same numeric reading the summary uses, so
            # "6 6.0" cannot be two arms here and one arm in the analysis.
            a="$(awk -v v="$a" 'BEGIN { printf "%g", v + 0 }')"
            fi
            for b in ${arm_list[@]+"${arm_list[@]}"}; do
                [ "$b" = "$a" ] || continue
                printf 'REFUSED: --arms repeats %s. One block is one boot per arm, so a repeat makes that unverifiable.\n' "$a" >&2
                exit 1
            done
            arm_list+=("$a")
        done
        if [ "${#arm_list[@]}" -lt 2 ]; then
            printf 'REFUSED: --arms needs at least 2 arms — there is nothing to interleave with one.\n' >&2
            printf '  Drop --arms and set PANE_SEND_SETTLE_S to sample a single arm.\n' >&2
            exit 1
        fi
        # A seed is always recorded, chosen or not: an arm order nobody can
        # restate is a covariate the artifact cannot evidence.
        [ -n "$SEED" ] || SEED=$(( RANDOM * 32768 + RANDOM ))
        case "$SEED" in ''|*[!0-9]*) printf 'bad --seed: %s\n' "$SEED" >&2; exit 1 ;; esac
        _LCG_STATE="$SEED"
    fi
    build_boot_plan "$BOOTS" ${arm_list[@]+"${arm_list[@]}"}

    # Before the gates: an aborted run must still carry its knob state (#1109).
    knob_disclosure

    # Preconditions — skip (2), never fail, when a heavy dep is absent.
    for dep in "$CLAUDE_BIN" jq python3 tmux claudron; do
        command -v "$dep" >/dev/null 2>&1 || { printf 'SKIP: %s not found\n' "$dep"; exit 2; }
    done
    [ -f "$HOST_CREDS" ] || { printf 'SKIP: no host auth at %s to seed\n' "$HOST_CREDS"; exit 2; }
    [ -n "$_TIMEOUT_BIN" ] || { printf 'SKIP: no timeout(1)/gtimeout to bound boots\n'; exit 2; }
    local mem; mem="$(mem_available_mb)"
    if [ -z "$mem" ]; then
        # No /proc/meminfo (macOS): the floor cannot be enforced. Disclose
        # rather than silently proceed as if it were.
        printf 'NOTE: memory-floor check unavailable on this host (no /proc/meminfo) — proceeding unguarded\n'
    elif [ "$mem" -lt "$MEM_FLOOR_MB" ]; then
        printf 'SKIP: MemAvailable %sMB below floor %sMB — a starved host risks the live fleet and biases readiness timing\n' \
            "$mem" "$MEM_FLOOR_MB"; exit 2
    fi

    ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-bsampler.XXXXXX")"
    CONFIG_DIR="$ROOT/config"
    BOT="bsprobe"
    BOT_DIR="$ROOT/runtime/bots/$BOT"
    PROBE_FLEET="boot-sampler"
    VAULT="$ROOT/vault"
    ART="$ROOT/artifacts"
    ROWS="$ART/rows.jsonl"
    LOG="$ART/sampler.log"
    SOCKET=""   # resolved from the composed bot.conf after generate

    cleanup() {
        _lc_cleanup
        # Synthetic load dies first: it is the only thing this harness does that
        # degrades a SHARED host, so it must not outlive any later teardown step
        # that could itself hang.
        stop_load_burners
        # Kill the probe tmux server + any surviving descendants, always.
        if [ -n "$SOCKET" ]; then
            bot_tmux "$SOCKET" kill-server 2>/dev/null || true
        fi
        # Remove the known credential and identity carriers. This is a DENYLIST and so
        # incomplete by construction: the next file Claude Code writes into
        # CLAUDE_CONFIG_DIR is not covered until someone adds it here. Inverting it to an
        # allowlist of what may SURVIVE is the real fix (#1231) and is deliberately out of
        # scope for this change.
        #
        # .claude.json is here because Claude Code writes an oauthAccount object into it
        # (account uuid, email address, organization uuid) plus a top-level userID. It was
        # absent from this list, so every --keep run left operator identifiers on disk
        # while the message below announced that it had not.
        rm -f "$ROOT/.env" \
              "$CONFIG_DIR/.credentials.json" \
              "$CONFIG_DIR/.claude.json" 2>/dev/null || true
        if [ -n "$KEEP" ]; then
            # Name what was removed instead of asserting a category. The previous wording
            # was "secrets scrubbed", a claim a denylist cannot support, and a false
            # assurance is worse than a visible gap because nobody re-checks it. It is
            # also the path from host-local to external: a reader who believes the
            # directory is clean attaches it to an issue or hands it to a colleague.
            printf 'kept artifacts at %s (removed: .env, .credentials.json, .claude.json; other files NOT audited)\n' "$ROOT"
            return
        fi
        rm -rf "$ROOT" 2>/dev/null || true
    }
    trap cleanup EXIT

    mkdir -p "$CONFIG_DIR" "$ART"
    : > "$LOG"

    # ── compose the probe into the throwaway root (freshbox pattern) ──────────
    ln -s "$CLAUDLOBBY_SRC/library" "$ROOT/library"
    ln -s "$CLAUDLOBBY_SRC/lib" "$ROOT/lib"
    ln -s "$CLAUDLOBBY_SRC/templates" "$ROOT/templates"

    # Throwaway vault so the claudron session-loop hooks compose — production
    # bots are vault-wired, and SessionStart hook work is part of boot weight.
    mkdir -p "$VAULT/_shared/knowledge"
    printf '# Conventions\n\nboot-sampler throwaway vault.\n' > "$VAULT/_shared/CONVENTIONS.md"

    # GITHUB_PAT (if the caller env has one) gives the github MCP server a real
    # token — same server, same handshake as production. Runtime-only: written
    # 600 into the throwaway root, scrubbed on exit.
    local pat_note="github MCP token: absent (server still spawns; degraded parity, recorded)"
    (umask 177; : > "$ROOT/.env")
    if [ -n "${GITHUB_PAT:-}" ]; then
        printf 'export GITHUB_PAT=%q\n' "$GITHUB_PAT" >> "$ROOT/.env"
        pat_note="github MCP token: present"
    fi

    cat > "$ROOT/fleet.yaml" <<YAML
fleet:
  name: $PROBE_FLEET
  service_prefix: bsampler
  accounts:
    default: ~/.claude
    sampler: $CONFIG_DIR
  bots:
    $BOT:
      name: $BOT
      account: sampler
      claudron_vault_path: $VAULT
      expertise:
        - software-engineering
      mcp:
        - github
      # The channel plugin production pins (the claudfather fork), NOT the
      # config-level default — the bridge under test must be the bridge the
      # fleet runs.
      channels:
        - "plugin:telegram@claudfather-plugins"
      telegram:
        handle: bsprobe_probe_bot
      startup_prompt: "$STARTUP_PROMPT_TEXT"
      env:
        EXPECT_NO_TOKEN: "1"
YAML

    printf 'composing probe bot with %s ...\n' "$CLAUDLOBBY_SRC"
    CLAUDLOBBY_ROOT="$ROOT" PYTHONPATH="$CLAUDLOBBY_SRC" python3 -m claudlobby generate >> "$LOG" 2>&1

    pass=0
    fail=0
    [ -f "$BOT_DIR/bot.conf" ] || { printf 'ERROR: compose produced no bot.conf (see %s)\n' "$LOG"; exit 1; }

    # Composed-output assertions: prove the probe carries the parity surfaces
    # BEFORE burning boots on it (fleet-yaml lesson: verify composed output,
    # never the edit).
    harness_check "composer pinned CLAUDE_CONFIG_DIR at the throwaway dir" \
        "$([ "$(bot_conf_get "$BOT_DIR" CLAUDE_CONFIG_DIR "")" = "$CONFIG_DIR" ] && echo yes || echo no)"
    harness_check "composed CLAUDE_FLAGS carry --channels (telegram plugin will spawn)" \
        "$(bot_conf_get "$BOT_DIR" CLAUDE_FLAGS "" | grep -q -- '--channels' && echo yes || echo no)"
    harness_check "composed STARTUP_PROMPT carries the probe marker" \
        "$(bot_conf_get "$BOT_DIR" STARTUP_PROMPT "" | grep -qF "$MARKER" && echo yes || echo no)"
    harness_check "probe declares EXPECT_NO_TOKEN=1 (tokenless canary, no readiness burn)" \
        "$([ "$(bot_conf_get "$BOT_DIR" EXPECT_NO_TOKEN "")" = "1" ] && echo yes || echo no)"
    harness_check "composed .mcp.json wires the github server" \
        "$(jq -e '.mcpServers.github' "$BOT_DIR/.mcp.json" >/dev/null 2>&1 && echo yes || echo no)"
    if [ "$fail" -gt 0 ]; then
        printf 'ERROR: composed probe failed %d parity assertions — not sampling on an unrepresentative bot\n' "$fail"
        exit 1
    fi

    SOCKET="$(tmux_socket_for_bot "$BOT_DIR")"
    # The sampler's own bot_tmux calls must resolve the SAME socket dir the
    # probe composes, or classification would watch a server that is not there.
    local probe_tmux_tmpdir
    probe_tmux_tmpdir="$(bot_conf_get "$BOT_DIR" TMUX_TMPDIR "")"
    [ -n "$probe_tmux_tmpdir" ] && export TMUX_TMPDIR="$probe_tmux_tmpdir"
    STARTUP_PROMPT_COMPOSED="$(bot_conf_get "$BOT_DIR" STARTUP_PROMPT "")"
    # The EXACT probe pane_send_verified uses: first _PANE_PROBE_MAX_CHARS
    # (sourced from lib-common) of the sent text, which start-bot.sh:366
    # prefixes with the history-expansion guard. COUPLING: this must mirror
    # start-bot's payload construction; if that prefix changes, stranded boots
    # reclassify as other:no_evidence (loudly counted, never silently clean) —
    # a pane_send_probe SSOT beside pane_send_verified is a named follow-up.
    # The FULL sent payload, not a prefix (#1082). pane_shows_payload reverses the
    # containment — it asks whether the rendering is part of what we sent — so a
    # truncated probe would silently reintroduce the interior-window half of the
    # bug here even after lib-common was fixed.
    PROBE="set +H; $STARTUP_PROMPT_COMPOSED"

    # ── seed the persistent throwaway config dir (warm ≈ a production restart) ─
    seed_claude_auth_and_trust "$CONFIG_DIR" "$BOT_DIR" "$CLAUDE_BIN" "$HOST_CREDS"
    # Warm plugin copy from the host cache: production bots restart onto
    # installed plugins, so a cold marketplace clone per boot would sample a
    # different (slower) condition — and versions match production exactly.
    local plugins_note="plugins: cold (no host cache found — warm-up boot installs)"
    if [ -d "$HOST_PLUGINS" ]; then
        cp -a "$HOST_PLUGINS" "$CONFIG_DIR/plugins"
        plugins_note="plugins: warm-copied from host cache"
    fi

    # A stale probe server from a killed prior run would collide on the socket.
    bot_tmux "$SOCKET" kill-server 2>/dev/null || true

    printf 'probe composed. %s; %s\n' "$plugins_note" "$pat_note"
    # Sample size is the plan minus its single warm-up, so the banner cannot
    # drift from what actually runs.
    local plan_n=${#_BOOT_PLAN[@]}
    if [ -n "$ARMS" ]; then
        printf 'sampling: %d blocks x %d arms = %d boots (+1 warm-up), arms [%s], seed %s\n' \
            "$BOOTS" "${#arm_list[@]}" "$((plan_n - 1))" "${arm_list[*]}" "$SEED"
        printf 'pairing: INTERLEAVED blocks, arm order randomized within each block (pre-registration v2 §4 BASELINE)\n'
    else
        printf 'sampling: %d boots (+1 warm-up) — SINGLE ARM. A ladder built by concatenating\n' "$((plan_n - 1))"
        printf '  single-arm runs is arm-sequential, which v2 §4 BASELINE forbids; use --arms.\n'
    fi
    printf 'deadline %ss, poll %ss, socket %s\n' "$DEADLINE" "$POLL_S" "$SOCKET"

    # ── contended-boot arm ────────────────────────────────────────────────────
    # Started here rather than before compose: compose is not a boot, and loading
    # the host through it would slow the setup without sampling anything. The
    # ceiling is the whole remaining run plus slack, so the backstop can only
    # fire after the sample is over.
    # Ceiling is derived from the PLAN, not from -n: under --arms the run is
    # blocks x arms boots, so a -n-based ceiling would expire the burners
    # partway through a 3-arm ladder and silently move the load arm mid-sample.
    if [ "$LOAD_BURNERS" -gt 0 ]; then
        start_load_burners "$LOAD_BURNERS" $(( (plan_n + 2) * (DEADLINE + 120) ))
        sleep 15   # let loadavg climb to steady state before boot 0
        printf 'load arm: %d burners, loadavg now %s (contended boot — see divergence 2)\n' \
            "$LOAD_BURNERS" "$(loadavg_1m)"
    else
        printf 'load arm: OFF — this samples the IDLE boot, which is not the condition that strands (#933)\n'
    fi

    # ── boot loop ─────────────────────────────────────────────────────────────
    local i=0 kind session outcome t_startbot t_submit rc pane pids p
    local events_before events_after="" retry_fired parity boot_art
    local glyph_at_inject t_glyph boot_la
    local entry blk pos arm ord
    session="$(tmux_session_name "$BOT_DIR")"
    while [ "$i" -lt "$plan_n" ]; do
        entry="${_BOOT_PLAN[$i]}"
        IFS="$_FS" read -r kind blk pos arm ord <<<"$entry"
        boot_art="$ART/boot-$(printf '%02d' "$i")"
        mkdir -p "$boot_art"

        # Per-boot resets: a session handoff written by a prior probe session
        # would flip the next boot onto the RESUME path and change the condition
        # mid-sample.
        rm -f "$BOT_DIR/.claude/session.md" 2>/dev/null || true
        outcome=""; t_submit=""; pane=""; pids=""; glyph_at_inject=""; t_glyph=""
        # Carry the ledger count forward — boot i's "before" is boot i-1's
        # "after"; only the first boot scans cold.
        if [ -n "${events_after:-}" ]; then
            events_before="$events_after"
        else
            events_before="$(count_send_retries "$BOT_DIR")" || events_before=""
        fi
        touch "$ROOT/.boot-marker"
        sleep 1  # -nt needs the marker strictly older than new transcripts
        # Sampled at the boot that experienced it, not once for the run: the
        # live fleet on a shared host moves under us, so a per-run figure would
        # attribute one boot's contention to all of them.
        boot_la="$(loadavg_1m)"
        # The arm is resolved per boot, from the live environment — never from
        # a caller-supplied label, and never hoisted out of the loop, so a
        # runner that varies the knob between boots is recorded truthfully
        # rather than stamped with whatever was set at startup.
        #
        # Interleave mode is now that runner, and it sets the knob HERE rather
        # than passing the arm into the row alongside it. The row must record
        # what governed the boot, so the plan sets the variable and the same
        # in-force resolution every other mode uses reads it back — no second
        # path by which a label could be written without the condition
        # changing. An empty arm is single-arm mode and must not touch the
        # variable at all: an empty value would shadow the lib-common default.
        if [ "$ARM_AXIS" = "trace" ]; then
            # The trace axis leaves settle alone and moves only the
            # instrumentation, so a difference between arms is attributable to
            # the instrument and to nothing else.
            case "$arm" in
                on)  PANE_VERIFY_TRACE="$ART/trace-boot-$i" ;;
                # off AND the empty arm (the warm-up) both clear it. Leaving it
                # set would point the next boot at the previous boot's trace
                # dir -- the warm-up did exactly that on the first attempt.
                *)   unset PANE_VERIFY_TRACE ;;
            esac
        else
            [ -z "$arm" ] || PANE_SEND_SETTLE_S="$arm"
        fi
        # Set BEFORE the
        # child runs, so a boot that fails outright still carries its arm, and
        # before SECONDS=0, so its jq fork (measured 66ms on the reference
        # host) stays outside the window t_startbot_s reports: SECONDS is
        # integer-valued, so a constant overhead there rounds ~7% of boots up
        # by a whole second. The instrument must not add noise to its own
        # reading.
        _ARM_KNOBS_JSON="$(arm_knobs_json)"

        rc=0
        SECONDS=0
        run_start_bot $((DEADLINE + 120)) "$ROOT" "$BOT_DIR" >> "$LOG" 2>&1 || rc=$?
        t_startbot="$SECONDS"

        if [ "$rc" -ne 0 ]; then
            outcome="other:startbot_rc_$rc"
        else
            # Pane state the moment injection finished: was the input box even
            # drawn? pane_send_verified treats a glyph-less pane as "nothing
            # stuck" (its verify cannot fire before the box exists), so this
            # field is what lets the sample resolve WHERE strands live — it
            # conditions the rate on TUI-drawn-at-inject, the #843
            # readiness-tracking hypothesis.
            pane="$(bot_tmux "$SOCKET" capture-pane -t "$session" -p 2>/dev/null || true)"
            printf '%s\n' "$pane" > "$boot_art/pane-at-inject.txt"
            if [ -n "$(pane_input_region "$pane")" ]; then
                glyph_at_inject=1
                # Box existed by injection-return: t_glyph is left-censored at
                # t_startbot (the draw happened at or before it), so the loop's
                # glyph poll handles only the not-yet-drawn population.
                t_glyph="$t_startbot"
            else
                glyph_at_inject=0
            fi
            # Classification poll: submission evidence decides immediately; the
            # pane is consulted per tick only until the first prompt glyph
            # appears (t_glyph — when the TUI actually drew its input box,
            # measured against the 3-9s production injection window), then only
            # at the deadline. SECONDS (bash builtin, still counting from the
            # pre-boot reset) replaces per-tick date(1) forks.
            while [ "$SECONDS" -lt "$DEADLINE" ]; do
                if ! check_tmux_session "$session" "$SOCKET"; then
                    outcome="other:session_died"
                    break
                fi
                if [ -z "$t_glyph" ]; then
                    pane="$(bot_tmux "$SOCKET" capture-pane -t "$session" -p 2>/dev/null || true)"
                    [ -n "$(pane_input_region "$pane")" ] && t_glyph="$SECONDS"
                fi
                if submitted_evidence "$CONFIG_DIR" "$ROOT/.boot-marker" "$MARKER"; then
                    t_submit="$SECONDS"
                    outcome="clean"
                    break
                fi
                sleep "$POLL_S"
            done
            pids="$(bot_tmux "$SOCKET" display -p -t "$session" '#{pane_pid}' 2>/dev/null || true)"
            [ -n "$pids" ] && list_descendants "$pids" > "$boot_art/procs.txt" 2>/dev/null || true
            pane="$(bot_tmux "$SOCKET" capture-pane -t "$session" -p 2>/dev/null || true)"
            printf '%s\n' "$pane" > "$boot_art/pane.txt"
            if [ -z "$outcome" ]; then
                outcome="$(final_verdict "$pane" "$PROBE")"
            fi
        fi

        # Per-boot evidence beyond the verdict: did the #837 retry fire, and
        # the live process tree (parity). startup.log detail lives in the tail
        # artifact rather than a row field.
        events_after="$(count_send_retries "$BOT_DIR")" || events_after=""
        # UNKNOWN (empty -> null in the row) when either count could not be read:
        # a plane outage is not "the retry did not fire".
        if [ -n "$events_after" ] && [ -n "$events_before" ]; then
            retry_fired=$(( events_after - events_before ))
        else
            retry_fired=""
        fi
        parity="$(awk '{ $1 = ""; sub(/^ /, ""); print }' "$boot_art/procs.txt" 2>/dev/null | sort | uniq -c | awk '{ c = $1; $1 = ""; sub(/^ /, ""); printf "%s:%s ", $0, c }')" || true
        tail -40 "$BOT_DIR/logs/startup.log" > "$boot_art/startup.log.tail" 2>/dev/null || true

        # settle_s is HOISTED from arm_knobs in the same expression rather than
        # passed alongside it: two independently-supplied copies of one fact is
        # how a row comes to disagree with itself, and the summarizer refuses a
        # row whose hoisted IV does not match its arm record.
        jq -nc --arg i "$i" --arg kind "$kind" --arg outcome "$outcome" \
            --arg t_startbot "$t_startbot" --arg t_submit "${t_submit:-}" \
            --arg retry "$retry_fired" --arg parity "${parity:-}" \
            --arg glyph "${glyph_at_inject:-}" --arg t_glyph "${t_glyph:-}" \
            --arg burners "$LOAD_BURNERS" --arg la "${boot_la:-}" \
            --arg blk "${blk:-}" --arg pos "${pos:-}" \
            --arg ord "${ord:-}" --arg seed "${SEED:-}" \
            --argjson arm "${_ARM_KNOBS_JSON:-null}" \
            '{i: ($i|tonumber), kind: $kind, outcome: $outcome,
              t_startbot_s: ($t_startbot|tonumber),
              t_submit_s: (if $t_submit == "" then null else ($t_submit|tonumber) end),
              retry_fired: (if $retry == "" then null else ($retry|tonumber) end), parity_procs: $parity,
              glyph_at_inject: (if $glyph == "" then null else ($glyph|tonumber) end),
              t_glyph_s: (if $t_glyph == "" then null else ($t_glyph|tonumber) end),
              load_burners: ($burners|tonumber),
              loadavg_1m: (if $la == "" then null else ($la|tonumber) end),
              arm_knobs: $arm,
              settle_s: ($arm | if . == null then null else .["PANE_SEND_SETTLE_S"].v end),
              # Hoisted the same way and for the same reason: the summariser
              # groups on a row field and cross-checks it against the knob
              # record, so both must come from the one in-force resolution.
              trace_on: ($arm | if . == null then null else .["PANE_VERIFY_TRACE"].v end),
              block: (if $blk == "" then null else ($blk|tonumber) end),
              pos: (if $pos == "" then null else ($pos|tonumber) end),
              # try/catch, not bare tonumber: the settle axis has numeric arms
              # and the #1236 trace axis has "on"/"off", and a bare coercion
              # aborts the whole row on the latter -- which is how the first
              # control run died after its warm-up boot.
              arm_order: (if $ord == "" then null else ($ord | split(" ") | map(try tonumber catch .)) end),
              arm_seed: (if $seed == "" then null else ($seed|tonumber) end)}' >> "$ROWS"
        printf 'boot %02d (%s)%s: %s%s%s%s\n' "$i" "$kind" \
            "${blk:+ block $blk pos $pos $ARM_AXIS=$arm}" "$outcome" \
            "${t_submit:+ submit=${t_submit}s}" \
            "${boot_la:+ la=${boot_la}}" \
            "$({ [ -n "$retry_fired" ] && [ "$retry_fired" -gt 0 ] && printf ' [send_retry fired]'; } || { [ -z "$retry_fired" ] && printf ' [send_retry UNKNOWN: plane unreachable]'; } || true)"

        # Teardown: kill-server takes the pane's tree; one liveness-gated KILL
        # pass sweeps recorded survivors (MCP servers can outlive the pane).
        # Gated on kill -0 so a recycled pid on this shared host is never hit
        # (orphan-browser-reaper precedent); no TERM grace — the root is
        # rm -rf'd at exit, there is nothing for a survivor to flush.
        bot_tmux "$SOCKET" kill-server 2>/dev/null || true
        sleep 1
        if [ -s "$boot_art/procs.txt" ]; then
            while read -r p _; do
                kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null || true
            done < "$boot_art/procs.txt"
        fi

        i=$((i + 1))
    done

    # ── summary (the product) ─────────────────────────────────────────────────
    printf '\n'
    emit_summary "$ROWS"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
