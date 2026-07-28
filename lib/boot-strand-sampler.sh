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
#      The poller's own network phase is the one boot component not sampled.
#   2. SERIAL BOOTS. Production strands were observed on one-at-a-time
#      restarts, which this reproduces; the mass-restart contention path
#      (BOOT_LOCK held by peers) is not sampled.
#   3. The per-boot process ledger (parity_procs: every descendant of the pane)
#      is recorded so parity is EVIDENCED per boot, not asserted.
#
# Summary statistics (lib/boot-strand-summary.py, stdlib-only): exact
# Clopper–Pearson 95% interval on the strand rate, printed next to the pre-fix
# baseline — which is itself only 2 strands in n=4, so the null is poorly
# estimated and no sample size makes the fix "proven"; the interval is the
# result, not a verdict.
#
# Usage: boot-strand-sampler.sh [-n BOOTS] [--deadline SECS] [--keep]
#   -n BOOTS         sample size, default 20 (a warm-up boot runs first and is
#                    reported separately, never counted)
#   --deadline SECS  per-boot classification deadline, default 120. Healthy
#                    boots submit in seconds and strands never resolve (#843
#                    timing evidence), so the gap tolerates a generous value.
#   --keep           keep $ROOT artifacts (secrets are scrubbed either way)
# Env: CLAUDLOBBY_SRC (checkout under test, default: this script's repo),
#      CLAUDE_BIN (default: real `claude` — the point), SAMPLER_MEM_FLOOR_MB
#      (default 1200; refuses to run on a starved host, which would both risk
#      the live fleet and bias readiness timing).
#
# Exit: 0 sample completed (the summary is the product; strands do not fail
#         the run) · 1 harness failure (setup assertion failed, or zero boots
#         reached a clean/strand verdict — a sample of others-only must not
#         read as a measurement) · 2 precondition/dep missing (skip).

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
MEM_FLOOR_MB="${SAMPLER_MEM_FLOOR_MB:-1200}"

# The probe marker is the submission ground truth: greppable in the session
# JSONL user record, and inside the first pane-rendered line of the payload so
# the strand probe (first 60 chars, the pane_send_verified cap) carries it.
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
list_descendants() {
    ps -e -ww -o pid=,ppid=,comm= | awk -v root="$1" '
        { pid[NR] = $1; ppid[NR] = $2; comm[NR] = $3 }
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
submitted_evidence() {
    local cfg="$1" newer="$2" marker="$3" f hit=""
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        grep -q -- "$marker" "$f" 2>/dev/null || continue
        hit="$(jq -rc --arg m "$marker" \
            'select(.type=="user") | (.message.content | tostring) | select(contains($m)) | "hit"' \
            "$f" 2>/dev/null | head -1)" || true
        [ "$hit" = "hit" ] && return 0
    done < <(find "$cfg/projects" -name '*.jsonl' -newer "$newer" 2>/dev/null)
    return 1
}

# final_verdict <submitted 0|1> <pane_text> <probe>
# The outcome precedence in one testable place: submission evidence is decisive
# (a transcript echo of the probe in the pane must not override it); otherwise
# the #837 unsubmitted-payload judgment; otherwise other.
final_verdict() {
    local submitted="$1" pane="$2" probe="$3"
    if [ "$submitted" = "1" ]; then
        printf 'clean'
    elif pane_holds_unsubmitted "$pane" "$probe"; then
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

# run_start_bot <root> <bot_dir> — start-bot under a CONSTRUCTED child env
# (#846: built from nothing, never inherit-and-subtract). Production start-bot
# runs under systemd with a clean environment; a bot-session caller instead
# carries its OWN exported TELEGRAM_BOT_TOKEN + TELEGRAM_TOKEN_ENV_NAME, and
# inherited they make the probe resolve a PRODUCTION token — the readiness
# gate then waits the full ceiling for a poller that must never exist, and the
# probe bridge could steal a live bot's getUpdates (caught live on this
# sampler's first smoke run). PATH is the one deliberate inheritance (host
# tools); everything else the boot needs comes from bot.conf and the .env
# tiers, exactly as production sources them.
run_start_bot() {
    local timeout_s="$1" root="$2" bot_dir="$3"
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
        bash "$root/lib/start-bot.sh" "$bot_dir"
}

# ── the sampler ───────────────────────────────────────────────────────────────

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -n)         BOOTS="${2:?-n needs a value}"; shift 2 ;;
            --deadline) DEADLINE="${2:?--deadline needs a value}"; shift 2 ;;
            --keep)     KEEP=1; shift ;;
            -h|--help)  usage; exit 0 ;;
            *)          printf 'unknown arg: %s\n' "$1" >&2; usage >&2; exit 1 ;;
        esac
    done
    case "$BOOTS" in ''|*[!0-9]*) printf 'bad -n: %s\n' "$BOOTS" >&2; exit 1 ;; esac
    case "$DEADLINE" in ''|*[!0-9]*) printf 'bad --deadline: %s\n' "$DEADLINE" >&2; exit 1 ;; esac

    # Preconditions — skip (2), never fail, when a heavy dep is absent.
    for dep in "$CLAUDE_BIN" jq python3 tmux claudron; do
        command -v "$dep" >/dev/null 2>&1 || { printf 'SKIP: %s not found\n' "$dep"; exit 2; }
    done
    [ -f "$HOST_CREDS" ] || { printf 'SKIP: no host auth at %s to seed\n' "$HOST_CREDS"; exit 2; }
    [ -n "$_TIMEOUT_BIN" ] || { printf 'SKIP: no timeout(1)/gtimeout to bound boots\n'; exit 2; }
    local mem; mem="$(mem_available_mb)"
    if [ -n "$mem" ] && [ "$mem" -lt "$MEM_FLOOR_MB" ]; then
        printf 'SKIP: MemAvailable %sMB below floor %sMB — a starved host risks the live fleet and biases readiness timing\n' \
            "$mem" "$MEM_FLOOR_MB"; exit 2
    fi

    ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-bsampler.XXXXXX")"
    CONFIG_DIR="$ROOT/config"
    BOT="bsprobe"
    BOT_DIR="$ROOT/runtime/bots/$BOT"
    VAULT="$ROOT/vault"
    ART="$ROOT/artifacts"
    ROWS="$ART/rows.jsonl"
    LOG="$ART/sampler.log"
    SOCKET=""   # resolved from the composed bot.conf after generate

    cleanup() {
        _lc_cleanup
        # Kill the probe tmux server + any surviving descendants, always.
        if [ -n "$SOCKET" ]; then
            bot_tmux "$SOCKET" kill-server 2>/dev/null || true
        fi
        # Secrets never outlive the run, --keep included.
        rm -f "$ROOT/.env" "$CONFIG_DIR/.credentials.json" 2>/dev/null || true
        if [ -n "$KEEP" ]; then printf 'kept artifacts (secrets scrubbed): %s\n' "$ROOT"; return; fi
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
  name: boot-sampler
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
        "$(grep -q -- '--channels' "$BOT_DIR/bot.conf" && echo yes || echo no)"
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
    # The EXACT probe pane_send_verified uses: first _PANE_PROBE_MAX_CHARS of
    # the sent text, which start-bot prefixes with the history-expansion guard.
    PROBE="$(printf '%s' "set +H; $STARTUP_PROMPT_COMPOSED" | cut -c1-"${_PANE_PROBE_MAX_CHARS:-60}")"

    # ── seed the persistent throwaway config dir (warm ≈ a production restart) ─
    cp "$HOST_CREDS" "$CONFIG_DIR/.credentials.json"
    chmod 600 "$CONFIG_DIR/.credentials.json"
    local ver
    ver="$("$CLAUDE_BIN" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)" || true
    jq -n --arg cwd "$BOT_DIR" --arg ver "${ver:-0.0.0}" '{
        hasCompletedOnboarding: true,
        lastOnboardingVersion: $ver,
        projects: { ($cwd): { hasTrustDialogAccepted: true, hasCompletedProjectOnboarding: true } }
    }' > "$CONFIG_DIR/.claude.json"
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
    printf 'sampling: %d boots (+1 warm-up), deadline %ss, poll %ss, socket %s\n' \
        "$BOOTS" "$DEADLINE" "$POLL_S" "$SOCKET"

    # ── boot loop ─────────────────────────────────────────────────────────────
    local i=0 kind session outcome t0 t_startbot t_submit rc pane pids
    local events_before events_after retry_fired parity ready_variant boot_art
    local glyph_at_inject t_glyph
    session="$(tmux_session_name "$BOT_DIR")"
    while [ "$i" -le "$BOOTS" ]; do
        if [ "$i" -eq 0 ]; then kind="warmup"; else kind="sample"; fi
        boot_art="$ART/boot-$(printf '%02d' "$i")"
        mkdir -p "$boot_art"

        # Per-boot resets: a session handoff written by a prior probe session
        # would flip the next boot onto the RESUME path and change the condition
        # mid-sample.
        rm -f "$BOT_DIR/.claude/session.md" 2>/dev/null || true
        events_before="$(cat "$BOT_DIR"/data/events/*.jsonl 2>/dev/null | grep -c '"type":"send_retry"' || true)"
        touch "$ROOT/.boot-marker"
        sleep 1  # -newer needs the marker strictly older than new transcripts

        t0="$(date +%s)"
        rc=0
        run_start_bot $((DEADLINE + 120)) "$ROOT" "$BOT_DIR" >> "$LOG" 2>&1 || rc=$?
        t_startbot=$(( $(date +%s) - t0 ))

        outcome=""
        t_submit=""
        pane=""
        pids=""
        # Pane state the moment injection finished: was the input box even
        # drawn? pane_send_verified treats a glyph-less pane as "nothing stuck"
        # (its verify cannot fire before the box exists), so this field is what
        # lets the sample resolve WHERE strands live — it conditions the rate
        # on TUI-drawn-at-inject, the readiness-tracking hypothesis in #843.
        glyph_at_inject=""
        if [ "$rc" -eq 0 ]; then
            pane="$(bot_tmux "$SOCKET" capture-pane -t "$session" -p 2>/dev/null || true)"
            printf '%s\n' "$pane" > "$boot_art/pane-at-inject.txt"
            if [ -n "$(pane_input_region "$pane")" ]; then glyph_at_inject=1; else glyph_at_inject=0; fi
        fi
        t_glyph=""
        if [ "$rc" -ne 0 ]; then
            outcome="other:startbot_rc_$rc"
        else
            # Classification poll: submission evidence decides immediately; the
            # pane is consulted per tick only until the first prompt glyph
            # appears (t_glyph — when the TUI actually drew its input box,
            # measured against the 3-9s production injection window), then only
            # at the deadline.
            while [ $(( $(date +%s) - t0 )) -lt "$DEADLINE" ]; do
                if ! bot_tmux "$SOCKET" has-session -t "$session" 2>/dev/null; then
                    outcome="other:session_died"
                    break
                fi
                if [ -z "$t_glyph" ]; then
                    pane="$(bot_tmux "$SOCKET" capture-pane -t "$session" -p 2>/dev/null || true)"
                    [ -n "$(pane_input_region "$pane")" ] && t_glyph=$(( $(date +%s) - t0 ))
                fi
                if submitted_evidence "$CONFIG_DIR" "$ROOT/.boot-marker" "$MARKER"; then
                    t_submit=$(( $(date +%s) - t0 ))
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
                outcome="$(final_verdict 0 "$pane" "$PROBE")"
            fi
        fi

        # Per-boot evidence beyond the verdict: did the #837 retry fire, which
        # READY variant gated injection, and the live process tree (parity).
        events_after="$(cat "$BOT_DIR"/data/events/*.jsonl 2>/dev/null | grep -c '"type":"send_retry"' || true)"
        retry_fired=$(( ${events_after:-0} - ${events_before:-0} ))
        ready_variant="$(grep -E 'READY|TIMEOUT' "$BOT_DIR/logs/startup.log" 2>/dev/null | tail -1 | cut -d' ' -f2- | cut -c1-70 || true)"
        parity="$(awk '{ print $2 }' "$boot_art/procs.txt" 2>/dev/null | sort | uniq -c | awk '{ printf "%s:%s ", $2, $1 }')" || true
        tail -40 "$BOT_DIR/logs/startup.log" > "$boot_art/startup.log.tail" 2>/dev/null || true

        jq -nc --arg i "$i" --arg kind "$kind" --arg outcome "$outcome" \
            --arg t_startbot "$t_startbot" --arg t_submit "${t_submit:-}" \
            --arg retry "$retry_fired" --arg parity "${parity:-}" \
            --arg ready "${ready_variant:-}" --arg glyph "${glyph_at_inject:-}" \
            --arg t_glyph "${t_glyph:-}" \
            '{i: ($i|tonumber), kind: $kind, outcome: $outcome,
              t_startbot_s: ($t_startbot|tonumber),
              t_submit_s: (if $t_submit == "" then null else ($t_submit|tonumber) end),
              retry_fired: ($retry|tonumber), parity_procs: $parity,
              ready_variant: $ready,
              glyph_at_inject: (if $glyph == "" then null else ($glyph|tonumber) end),
              t_glyph_s: (if $t_glyph == "" then null else ($t_glyph|tonumber) end)}' >> "$ROWS"
        printf 'boot %02d (%s): %s%s%s\n' "$i" "$kind" "$outcome" \
            "${t_submit:+ submit=${t_submit}s}" \
            "$([ "$retry_fired" -gt 0 ] && printf ' [send_retry fired]' || true)"

        # Teardown: the private server first, then any survivors of the tree
        # (MCP servers can outlive the pane; orphan-reaper lesson — kill the
        # whole descendant tree, not the direct child).
        bot_tmux "$SOCKET" kill-server 2>/dev/null || true
        sleep 1
        if [ -s "$boot_art/procs.txt" ]; then
            local p
            while read -r p _; do
                kill -TERM "$p" 2>/dev/null || true
            done < "$boot_art/procs.txt"
            sleep 1
            while read -r p _; do
                kill -KILL "$p" 2>/dev/null || true
            done < "$boot_art/procs.txt"
        fi

        i=$((i + 1))
    done

    # ── summary (the product) ─────────────────────────────────────────────────
    printf '\n'
    python3 "$LIB_DIR/boot-strand-summary.py" "$ROWS" || exit 1
    exit 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
