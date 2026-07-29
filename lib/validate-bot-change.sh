#!/bin/bash
# validate-bot-change.sh — the Deliver → Add config → Recompose → Observe loop,
# made runnable for the observability/trust-loop behaviors.
#
# Unit tests prove COMPOSITION (the env var lands in bot.conf). Only running the
# code proves BEHAVIOR (the event actually fires). This harness stands up a
# throwaway bot + tmux sessions in a temp root, drives the observability sweep,
# and ASSERTS that activity_stuck + overdue_dispatch events are emitted and that
# the manager is notified — no Claude auth or real fleet required.
#
# Use it as the "Observe" step when changing observability behavior, and as the
# worked example of the loop for other bot-behavior changes (see
# documentation/validating-bot-changes.md).
#
# Usage: bash lib/validate-bot-change.sh
#   Exit 0 = all observations matched intent, 1 = a behavior did not fire.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

if ! command -v tmux >/dev/null 2>&1; then
    echo "validate-bot-change: tmux required (it backs the observe step)" >&2
    exit 2
fi

# --- Per-bot socket isolation (mirror production) ----------------------------
# Each bot now runs its OWN tmux server. The scripts under test resolve a bot's
# socket via tmux_socket_for_bot(); for these hand-written bot.confs (BOT_SERVICE
# left empty) that resolves to the F2 test-harness fallback "tmux-<dir-slug>",
# which only applies when FLEET_NAME is unset — so unset it here. Then shadow
# `tmux` so every session op in THIS harness lands on that session's private
# server (-L tmux-<name>), matching what the scripts resolve. The scripts call
# "$_TMUX_BIN" in their own subprocesses, so this function never affects them.
unset FLEET_NAME
# Also drop an ambient CLAUDE_FLAGS: the fixture bot.confs deliberately omit it
# (minimal composed-looking confs), so a dev's exported CLAUDE_FLAGS would leak
# into the start-bot.sh boots and MASK a regression that CI (clean env) exposes.
unset CLAUDE_FLAGS

# Compress the #860 input-box readiness budget. This harness stubs `claude` with
# `exec cat`, so those panes never draw a box BY CONSTRUCTION — the gate would
# sit out its full 45s per send waiting for a TUI that the stub cannot render,
# across 18 start-bot invocations. Nothing about the behaviour under test
# changes; only the wait for a box that will never appear. The real budget is
# exercised against real boots in lib/boot-strand-sampler.sh, which is where it
# belongs, and the unit contract is pinned in tests/test_pane_send_verified.sh.
export PANE_READY_POLL_S=0.05 PANE_READY_TICKS=4
# Pin the escalation chat id for the WHOLE run (#846). fleet-pulse's critical
# alerts do NOT travel through MANAGER_TMUX — the isolation this harness provides
# by shadowing tmux — they go straight out via tg-post.sh, keyed on this var. So
# every fleet-pulse invocation below inherits the real fleet chat unless it is
# overridden, and the send is `2>/dev/null || true`, so a harness run that reaches
# production succeeds silently. It has: a two-throwaway-bot scenario crosses the
# session_missing threshold of 2 and paged a human.
#
# Run-wide, not per call site: the harness drives fleet-pulse from ten places and
# only the one scenario that deliberately tests escalation used to set this.
# Isolation is the default here; that scenario re-exports the same fake id
# explicitly, which is the shape it should have had from the start.
export FLEET_PULSE_ESCALATION_CHAT_ID="-100999"
vsock() { printf 'tmux-%s' "$1"; }
tmux() {
    local i sock=""
    local -a a=("$@")
    for ((i = 0; i < ${#a[@]}; i++)); do
        case "${a[i]}" in
            -t | -s) [ $((i + 1)) -lt ${#a[@]} ] && sock="$(vsock "${a[i + 1]}")"; break ;;
        esac
    done
    if [ -n "$sock" ]; then
        command tmux -L "$sock" "$@"
    else
        command tmux "$@"
    fi
}

FLEET="valfleet"
BOT="valbot"
MGR="valmgr"
IBOT="validle"
BUSY="valbusy"
SBOT="valsubmit"
MBOT="valmarker"
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-validate.XXXXXX")"
# #586: a per-run tmux namespace. Concurrent runs (parallel reviews, pytest)
# otherwise share ${TMUX_TMPDIR:-/tmp}/tmux-<uid>/ and collide on the fixed
# per-session socket names (tmux-<name>) — duplicate-session aborts, and one
# run's cleanup kill-server tears down the other's live sessions. Exporting
# TMUX_TMPDIR moves every socket — the harness's own clients AND the scripts
# under test, which inherit the env when resolving the same names — into a
# run-private dir. Literal /tmp, not $TMPDIR: socket paths must stay within
# sun_path (108 bytes) on hosts with long TMPDIRs (macOS /var/folders,
# per-session scratch dirs). Contract for the fixture bot.confs below: they
# must NOT carry the composed "export TMUX_TMPDIR=/tmp" pin (composer.py) —
# keepalive/start-bot SOURCE bot.conf, and a sourced pin would yank the
# scripts under test back into the shared namespace mid-run.
TMUX_TMPDIR="$(mktemp -d /tmp/claudlobby-validate-sock.XXXXXX)"
export TMUX_TMPDIR
# fleet-pulse resolves bots via resolve_bots_dir <fleet> = local/<fleet>/runtime/bots.
BOT_DIR="$ROOT/local/$FLEET/runtime/bots/$BOT"
install_error_trap "$BOT_DIR"
EVENTS="$BOT_DIR/data/events"

cleanup() {
    # Per-bot servers must be torn down with kill-server, or empty servers leak.
    for _s in "$BOT" "$MGR" "$IBOT" "$BUSY" "$SBOT" "$MBOT" "${HBOT:-}" "${RB_SESSION:-}" "${MP_SESSION:-}" "${IDLEK:-}" "${SOCKB:-}" "${BUSYM:-}" "${BUSYP:-}" "${BRIEF:-}" "${BRIEFBUSY:-}" "${SINK:-}"; do
        [ -n "$_s" ] && command tmux -L "$(vsock "$_s")" kill-server 2>/dev/null || true
    done
    # Bridge-hijack pollers are plain bun processes, not tmux panes — TERM any
    # still-alive ones so a mid-scenario abort never leaks a poller.
    # shellcheck disable=SC2086
    for _p in ${BH_PIDS:-}; do kill -TERM "$_p" 2>/dev/null || true; done
    rm -rf "$ROOT" "${RB_ROOT:-}" "${WR_ROOT:-}" "$TMUX_TMPDIR"
}
trap cleanup EXIT

mkdir -p "$BOT_DIR/data" "$ROOT/state"

# --- Add config: a composed-looking bot.conf with low thresholds for a fast loop ---
cat > "$BOT_DIR/bot.conf" <<CONF
BOT_NAME="$BOT"
BOT_ID="$BOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD=1
OBSERVABILITY_REAP_DAYS=7
TELEGRAM_BOT_HANDLE="$BOT"
OBSERVABILITY_BRIDGE_DOWN_GRACE=0
CONF

# --- Run: stand up a non-idle worker pane + a manager session to receive alerts ---
tmux new-session -d -s "$MGR" "sleep 600"
tmux new-session -d -s "$BOT" 'printf "\n⠹ Cogitating (esc to interrupt)\n"; sleep 600'
sleep 1  # let panes render

# Worker made a tool call a moment ago, then went silent (gap will exceed 1s).
touch "$BOT_DIR/data/.last-tool-call"

# A task was dispatched and is already past its deadline, with no report.
now=$(date +%s)
printf '{"ts":"2026-05-27T10:00:00Z","manager":"%s","bot":"%s","task_id":"t-1-aaaa","task":"do x","dispatched_at":%s,"expected_by":%s}\n' \
    "$MGR" "$BOT" "$((now - 600))" "$((now - 10))" > "$ROOT/state/dispatch-log.jsonl"

sleep 2  # ensure activity gap > threshold (1s)

# --- Observe: run the real pulse against the scratch fleet ---
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/fleet-pulse.sh" "$FLEET" >/dev/null 2>&1 || true

# --- Assert ---
pass=0; fail=0
events_file=$(ls "$EVENTS"/fleet-*.jsonl 2>/dev/null | head -1 || true)
mgr_pane=$(tmux capture-pane -t "$MGR" -p 2>/dev/null || true)

echo "=== validate-bot-change: observe the trust-loop behaviors ==="
[ -n "$events_file" ] && grep -q '"type":"activity_stuck"' "$events_file" && r=yes || r=no
harness_check "activity_stuck event emitted (animated-but-hung worker)" "$r"
[ -n "$events_file" ] && grep -q '"type":"overdue_dispatch"' "$events_file" && r=yes || r=no
harness_check "overdue_dispatch event emitted (deadline passed, no report)" "$r"
printf '%s' "$mgr_pane" | grep -q '\[FLEET-PULSE\]' && r=yes || r=no
harness_check "manager notified via [FLEET-PULSE] push" "$r"
[ -n "$events_file" ] && grep -q '"type":"bridge_down"' "$events_file" && r=yes || r=no
harness_check "bridge_down event emitted (live session, Telegram poller not delivering)" "$r"

# #460: a never-closing dispatch must age out of the overdue set so fleet-pulse
# stops re-emitting overdue_dispatch every cycle. Drive the real matcher (the CLI
# fleet-pulse consumes) with a 25h-old, never-reported dispatch and assert nothing.
VAL_REPORT_LEDGER="$ROOT/local/$FLEET/runtime/report-back.jsonl"
aged_log="$ROOT/state/dispatch-log-aged.jsonl"
printf '{"ts":"t","manager":"%s","bot":"%s","task":"x","dispatched_at":%s,"expected_by":%s}\n' \
    "$MGR" "$BOT" "$((now - 90000))" "$((now - 89400))" > "$aged_log"
aged_out=$(python3 "$LIB_DIR/dispatch-overdue.py" --all "$aged_log" "$VAL_REPORT_LEDGER" "$now" 2>/dev/null || true)
[ -z "$aged_out" ] && r=yes || r=no
harness_check "overdue_dispatch expires past max age (#460 — no re-emit for a 25h-old dispatch)" "$r"

# ===========================================================================
# Task-id end-to-end (goal-aware plan P4) — the dispatch row seeded above
# carries task_id t-1-aaaa; the REAL fleet-pulse run consumed it. Assert the
# id made it through the pipeline: into the emitted overdue event, and into
# the manager nudge with the self-heal echo instruction. (Join-matrix unit
# semantics live in tests/test_dispatch_overdue.py — not re-run here.)
echo ""
echo "=== validate task-id end-to-end (P4: event + nudge carry the id) ==="
[ -n "$events_file" ] && grep -q '"type":"overdue_dispatch"' "$events_file" \
    && grep '"type":"overdue_dispatch"' "$events_file" | grep -q '"task_id":"t-1-aaaa"' && r=yes || r=no
harness_check "overdue_dispatch event carries the dispatch task_id" "$r"
printf '%s' "$mgr_pane" | grep -q 'no report has closed' && printf '%s' "$mgr_pane" | grep -q 't-1-aaaa' && r=yes || r=no
harness_check "manager nudge names the open id (for the manager to act on)" "$r"

# ===========================================================================
# #835 — the two halves that stop the watchdog crying wolf over finished work.
# Both drive the REAL scripts: report-back.sh for the resolve, fleet-pulse.sh
# for the orphan split. Unit semantics are in tests/test_dispatch_overdue.py;
# what only running the code can prove is that the id actually lands in the
# ledger and that the pulse actually stops emitting.
# ===========================================================================
echo ""
echo "=== validate #835: an id-less report closes its dispatch; a respawn orphan goes quiet ==="

# --- Half 1: report-back.sh with NO --task must resolve the open dispatch. ---
T835_BOT="valrb835"
T835_DIR="$ROOT/local/$FLEET/runtime/bots/$T835_BOT"
mkdir -p "$T835_DIR/data"
cat > "$T835_DIR/bot.conf" <<CONF
BOT_NAME="$T835_BOT"
BOT_ID="$T835_BOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
t835_dispatch="$ROOT/state/dispatch-log.jsonl"
printf '{"ts":"2026-05-27T10:00:00Z","manager":"%s","bot":"%s","task_id":"t-835-open","task":"do y","dispatched_at":%s,"expected_by":%s}\n' \
    "$MGR" "$T835_BOT" "$((now - 600))" "$((now - 10))" >> "$t835_dispatch"

# Deliberately NO --task, the way every worker actually calls it.
CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$FLEET" MANAGER_TMUX="$MGR" \
    "$LIB_DIR/report-back.sh" "$T835_BOT" completed "finished the thing" >/dev/null 2>&1 || true

t835_ledger="$VAL_REPORT_LEDGER"
grep -q '"bot":"'"$T835_BOT"'"' "$t835_ledger" 2>/dev/null \
    && grep '"bot":"'"$T835_BOT"'"' "$t835_ledger" | grep -q '"task_id":"t-835-open"' && r=yes || r=no
harness_check "#835 report-back without --task stamps the resolved task id into the ledger" "$r"

# The join is unchanged — so the row closing is proof the id is the RIGHT one.
t835_left=$(python3 "$LIB_DIR/dispatch-overdue.py" --all "$t835_dispatch" "$t835_ledger" "$now" 2>/dev/null \
    | grep -c "^$T835_BOT " || true)
[ "${t835_left:-1}" -eq 0 ] && r=yes || r=no
harness_check "#835 the resolved id actually closes the dispatch (watchdog join untouched)" "$r"

# A second id-less report with nothing open must stay id-less, not grab a peer's.
CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$FLEET" MANAGER_TMUX="$MGR" \
    "$LIB_DIR/report-back.sh" "$T835_BOT" completed "and again" >/dev/null 2>&1 || true
t835_blank=$(grep '"bot":"'"$T835_BOT"'"' "$t835_ledger" | tail -1 | grep -c '"task_id":""' || true)
[ "${t835_blank:-0}" -eq 1 ] && r=yes || r=no
harness_check "#835 nothing open -> report stays id-less (no scavenging a peer's row)" "$r"

# --- Half 2: a respawn orphan must stop reaching the pulse's overdue path. ---
# Same bot dir, but .spawn is now NEWER than the dispatch: the session that
# received the id is gone, so it can never be echoed.
OR_BOT="valor835"
OR_DIR="$ROOT/local/$FLEET/runtime/bots/$OR_BOT"
mkdir -p "$OR_DIR/data/events"
cat > "$OR_DIR/bot.conf" <<CONF
BOT_NAME="$OR_BOT"
BOT_ID="$OR_BOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
printf '{"ts":"2026-05-27T10:00:00Z","manager":"%s","bot":"%s","task_id":"t-835-orphan","task":"do z","dispatched_at":%s,"expected_by":%s}\n' \
    "$MGR" "$OR_BOT" "$((now - 600))" "$((now - 10))" >> "$t835_dispatch"
touch "$OR_DIR/data/.spawn"   # respawned just now, i.e. after the dispatch

CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/fleet-pulse.sh" "$FLEET" >/dev/null 2>&1 || true

# Scope the match to overdue_dispatch specifically: the orphan DOES get a
# dispatch_orphaned event in this same file, and a bare task-id grep would
# match that and read a correctly-silenced alarm as a firing one.
or_overdue=$(grep -h '"type":"overdue_dispatch"' "$OR_DIR"/data/events/fleet-*.jsonl 2>/dev/null | grep -c 't-835-orphan' || true)
[ "${or_overdue:-1}" -eq 0 ] && r=yes || r=no
harness_check "#835 respawn orphan emits NO overdue_dispatch from the real pulse" "$r"

or_listed=$(python3 "$LIB_DIR/dispatch-overdue.py" --orphans "$t835_dispatch" "$t835_ledger" "$now" \
    --bots-dir "$ROOT/local/$FLEET/runtime/bots" 2>/dev/null | grep -c 't-835-orphan' || true)
[ "${or_listed:-0}" -ge 1 ] && r=yes || r=no
harness_check "#835 the orphan is still listable (evidence kept, not reaped away)" "$r"

# Inert for the ALARM, but recorded once — a task lost to a restart is
# actionable, and silence would trade this issue's noise for #826/#831/#833's.
or_ev=$(grep -h '"type":"dispatch_orphaned"' "$OR_DIR"/data/events/fleet-*.jsonl 2>/dev/null | grep -c 't-835-orphan' || true)
[ "${or_ev:-0}" -eq 1 ] && r=yes || r=no
harness_check "#835 the orphan is recorded once as dispatch_orphaned" "$r"

# Latched on id-set membership, so a second sweep must not re-record it.
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/fleet-pulse.sh" "$FLEET" >/dev/null 2>&1 || true
or_ev2=$(grep -h '"type":"dispatch_orphaned"' "$OR_DIR"/data/events/fleet-*.jsonl 2>/dev/null | grep -c 't-835-orphan' || true)
[ "${or_ev2:-0}" -eq 1 ] && r=yes || r=no
harness_check "#835 a second sweep does NOT re-record the same orphan (latch holds)" "$r"

# ===========================================================================
# Mechanism 1 (fleet update lifecycle) — daily plugin/skill live reload.
# Stubs claude/claudlobby on PATH so this needs no Claude auth or real fleet.
# ===========================================================================
echo ""
echo "=== validate reload-fleet (Mechanism 1: daily live reload) ==="

STUB_BIN="$ROOT/stubbin"
mkdir -p "$STUB_BIN"
printf '#!/bin/bash\nexit 0\n' > "$STUB_BIN/claude"
printf '#!/bin/bash\nexit 0\n' > "$STUB_BIN/claudlobby"
chmod +x "$STUB_BIN/claude" "$STUB_BIN/claudlobby"
# reload-fleet reads the plugin list from a bot.conf (fleet-global value).
echo 'FLEET_PLUGINS_REQUIRED="claudna@Claudfather"' >> "$BOT_DIR/bot.conf"

# Happy path: download + generate succeed -> .reload-pending dropped on the running bot.
rm -f "$BOT_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" PATH="$STUB_BIN:$PATH" "$LIB_DIR/reload-fleet.sh" "$FLEET" >/dev/null 2>&1 || true
[ -f "$BOT_DIR/data/.reload-pending" ] && r=yes || r=no
harness_check "reload-fleet marks a running bot with .reload-pending (happy path)" "$r"

# Loud-fail: a failing 'claude plugin update' must be LOUD, never silent.
printf '#!/bin/bash\necho boom >&2; exit 1\n' > "$STUB_BIN/claude"
chmod +x "$STUB_BIN/claude"
rm -f "$BOT_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" PATH="$STUB_BIN:$PATH" "$LIB_DIR/reload-fleet.sh" "$FLEET" >/dev/null 2>&1 || true
fleet_events=$(ls "$ROOT/state/events"/fleet-*.jsonl 2>/dev/null | head -1 || true)
[ -n "$fleet_events" ] && grep -q '"type":"reload_failed"' "$fleet_events" && r=yes || r=no
harness_check "reload-fleet emits reload_failed event on failure (loud, not silent)" "$r"
mgr_pane=$(tmux capture-pane -t "$MGR" -p 2>/dev/null || true)
printf '%s' "$mgr_pane" | grep -q 'reload_failed' && r=yes || r=no
harness_check "reload-fleet alerts the manager on failure (shared emit_failure_alert)" "$r"
[ ! -f "$BOT_DIR/data/.reload-pending" ] && r=yes || r=no
harness_check "reload-fleet does not half-reload (no marker when download fails)" "$r"

# ===========================================================================
# F2(b) consolidated activation — keepalive performs the live reload at idle.
# ===========================================================================
echo ""
echo "=== validate keepalive reload consumer (idle-gated activation) ==="

IBOT_DIR="$ROOT/local/$FLEET/runtime/bots/$IBOT"
mkdir -p "$IBOT_DIR/data"
cat > "$IBOT_DIR/bot.conf" <<CONF
BOT_NAME="$IBOT"
BOT_ID="$IBOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
# Idle pane: last line ends in a prompt glyph '>' (matches the idle base pattern).
# Draw the prompt with chrome BELOW it, as Claude Code does — this is the pane
# that exercises keepalive's send_reload_command, so a prompt-as-last-line shape
# here would validate the reload path against a geometry production never has.
# classify_pane captures the whole pane (not a tail), so idle detection is
# unaffected by the extra lines.
tmux new-session -d -s "$IBOT" 'printf -- "\n--------\n> \n--------\n\n  auto mode on\n"; sleep 600'
sleep 1
touch "$IBOT_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/keepalive.sh" "$IBOT_DIR" >/dev/null 2>&1 || true
sleep 1
ibot_pane=$(tmux capture-pane -t "$IBOT" -p 2>/dev/null || true)
printf '%s' "$ibot_pane" | grep -q '/reload-plugins' && r=yes || r=no
harness_check "keepalive sends /reload-plugins to an idle bot with .reload-pending" "$r"
printf '%s' "$ibot_pane" | grep -q '/reload-skills' && r=yes || r=no
harness_check "keepalive sends /reload-skills to an idle bot with .reload-pending" "$r"
[ ! -f "$IBOT_DIR/data/.reload-pending" ] && r=yes || r=no
harness_check "keepalive clears .reload-pending after firing the reload" "$r"

# WS-1 (#7) long-think path: a bot mid-thought (or a long single tool call) with
# NO recent tool-call marker — the pane's "esc to interrupt" active-turn affordance
# is the only active signal. classify_pane must read it BUSY so the reload is NOT
# injected (ghost-text into a working bot). No .last-tool-call → exercises the
# pane fallback, not the marker.
BUSY_DIR="$ROOT/local/$FLEET/runtime/bots/$BUSY"
mkdir -p "$BUSY_DIR/data"
cat > "$BUSY_DIR/bot.conf" <<CONF
BOT_NAME="$BUSY"
BOT_ID="$BUSY"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
tmux new-session -d -s "$BUSY" 'printf "⠋ Thinking… (esc to interrupt)\n"; sleep 600'
sleep 1
touch "$BUSY_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/keepalive.sh" "$BUSY_DIR" >/dev/null 2>&1 || true
[ -f "$BUSY_DIR/data/.reload-pending" ] && r=yes || r=no
harness_check "keepalive long-think: esc-to-interrupt pane (no recent marker) → BUSY, NOT reloaded (#7)" "$r"

# WS-1 (#7) marker path: a bot whose pane looks IDLE (a bare prompt glyph, no
# active-turn affordance) but made a tool call moments ago — a long op between
# calls. Only the fresh data/.last-tool-call says active; pane parsing alone would
# call it IDLE and inject reload keystrokes. The marker must classify it BUSY.
MBOT_DIR="$ROOT/local/$FLEET/runtime/bots/$MBOT"
mkdir -p "$MBOT_DIR/data"
cat > "$MBOT_DIR/bot.conf" <<CONF
BOT_NAME="$MBOT"
BOT_ID="$MBOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
# Identical idle-looking pane to the idle bot above — the ONLY difference is the
# fresh marker, isolating the marker's effect.
tmux new-session -d -s "$MBOT" 'printf "\n❯ \n"; sleep 600'
sleep 1
touch "$MBOT_DIR/data/.reload-pending"
touch "$MBOT_DIR/data/.last-tool-call"   # fresh marker = active recently
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/keepalive.sh" "$MBOT_DIR" >/dev/null 2>&1 || true
[ -f "$MBOT_DIR/data/.reload-pending" ] && r=yes || r=no
harness_check "keepalive marker path: idle-looking pane + fresh .last-tool-call → BUSY, NOT reloaded (#7)" "$r"
[ ! -f "$MBOT_DIR/data/.idle" ] && r=yes || r=no
harness_check "keepalive marker path: .idle marker not set (fleet-pulse stays consistent)" "$r"

# ===========================================================================
# #453 Phase 5 — Telegram bridge auto-heal (Tier-2, flag-gated F6b). Proves the
# heal ladder in keepalive.sh end-to-end: a DARK poller (no bot.pid → no_bridge)
# on an IDLE bot, with the heal flag on, triggers the restart ladder — the ONLY
# respawn (a claude bounce; the bun poller is an MCP stdio child of claude). The
# ladder's start-bot.sh fallback is a RECORDER stub, so nothing is really
# restarted. Also asserts the gates: flag-off is a no-op; no_token never bounces;
# the attempt budget caps then escalates (F3 escalate-only).
# CAVEAT: this exercises the heal MACHINERY on a deterministically-dark bridge.
# Recovery from genuine upstream nondeterministic non-spawn is measured by the
# production bounce→recovery telemetry that gates Tier-2 rollout (F6b), not here.
echo ""
echo "=== validate bridge heal (#453 Phase 5: keepalive respawns a dark poller) ==="
HBOT="valheal"
HDIR="$ROOT/local/$FLEET/runtime/bots/$HBOT"
HSTATE="$ROOT/ch/telegram-$HBOT"   # TELEGRAM_STATE_DIR — no bot.pid ⇒ bridge_state=no_bridge
HREC="$HDIR/data/.heal-restart-count"
mkdir -p "$HDIR/data" "$HSTATE"

# BOT_SERVICE="" so (a) the socket resolves to the harness fallback tmux-valheal
# (matching the session below) and (b) the restart ladder falls through to the
# start-bot.sh recorder rather than any real systemd unit.
_heal_conf() {   # $1 = OBSERVABILITY_BRIDGE_HEAL (0/1)   $2 = token present (y/n)
    cat > "$HDIR/bot.conf" <<CONF
BOT_NAME="$HBOT"
BOT_ID="$HBOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
TELEGRAM_BOT_HANDLE="$HBOT"
TELEGRAM_STATE_DIR="$HSTATE"
TELEGRAM_TOKEN_ENV_NAME=VALHEAL_TG_TOKEN
OBSERVABILITY_BRIDGE_DOWN_GRACE=0
OBSERVABILITY_BRIDGE_HEAL=$1
BRIDGE_HEAL_MAX_ATTEMPTS=2
CONF
    if [ "$2" = "y" ]; then
        printf 'VALHEAL_TG_TOKEN=8888888:AAAAAAAAAAAAAAAAAAAA\n' > "$HDIR/.env"
    else
        rm -f "$HDIR/.env"
    fi
}

# Stub lib dir: REAL keepalive + lib-common (symlinked), but a recorder start-bot.sh
# so the heal's restart ladder is observed, never executed. keepalive resolves
# LIB_DIR from its own path, so both the sourced lib-common and the invoked
# start-bot come from here.
HLIB="$ROOT/stublib"
mkdir -p "$HLIB"
ln -sf "$LIB_DIR/keepalive.sh" "$HLIB/keepalive.sh"
ln -sf "$LIB_DIR/lib-common.sh" "$HLIB/lib-common.sh"
cat > "$HLIB/start-bot.sh" <<'REC'
#!/bin/bash
# Recorder stub for the heal restart ladder (start-bot.sh fallback). Counts the
# bounce against the bot dir passed as $1 instead of restarting a service.
d="$1"
c=0; [ -f "$d/data/.heal-restart-count" ] && c=$(cat "$d/data/.heal-restart-count" 2>/dev/null)
printf '%s' "$((c + 1))" > "$d/data/.heal-restart-count"
REC
chmod +x "$HLIB/start-bot.sh"

# Idle pane (bare prompt glyph) so keepalive reaches the IDLE branch where the heal
# runs — the BUSY-gate is implicit in that placement.
tmux new-session -d -s "$HBOT" 'printf "\n> \n"; sleep 600'
sleep 1
_heal_reset() {   # clear the recorder + heal-ladder state between phases
    printf '%s' "0" > "$HREC"
    rm -f "$HDIR/data/.bridge-heal" "$HDIR/data/.bridge-heal-escalated"
}
run_heal() { CLAUDLOBBY_ROOT="$ROOT" "$HLIB/keepalive.sh" "$HDIR" >/dev/null 2>&1 || true; }

# --- Phase A: flag ON, token present, dark poller → bounce, retry, cap, escalate ---
_heal_conf 1 y
_heal_reset
run_heal   # tick 1 → bounce #1
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "1" ] && r=yes || r=no
harness_check "heal bounces a dark poller on an idle bot (restart ladder invoked)" "$r"
hev=$(ls "$HDIR/data/events"/keepalive-*.jsonl 2>/dev/null | head -1 || true)
[ -n "$hev" ] && grep -q '"state":"BRIDGE_HEAL"' "$hev" && r=yes || r=no
harness_check "heal emits a BRIDGE_HEAL keepalive event" "$r"
run_heal   # tick 2 → bounce #2 (reaches the cap of 2)
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "2" ] && r=yes || r=no
harness_check "heal retries up to the attempt cap (2nd bounce)" "$r"
run_heal   # tick 3 → budget exhausted → escalate-only, NO 3rd bounce
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "2" ] && r=yes || r=no
harness_check "heal stops bouncing at the cap (no 3rd bounce — F3 escalate-only)" "$r"
[ -f "$HDIR/data/.bridge-heal-escalated" ] && r=yes || r=no
harness_check "heal escalates once when the budget is exhausted" "$r"
hev=$(ls "$HDIR/data/events"/keepalive-*.jsonl 2>/dev/null | head -1 || true)
[ -n "$hev" ] && grep -q 'budget exhausted' "$hev" && r=yes || r=no
harness_check "heal logs budget-exhausted escalation" "$r"

# --- Phase B: flag OFF, same dark poller → never bounces (the F6b gate) ---
_heal_conf 0 y
_heal_reset
run_heal
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "0" ] && r=yes || r=no
harness_check "heal is a no-op when OBSERVABILITY_BRIDGE_HEAL!=1 (F6b gate holds)" "$r"

# --- Phase C: flag ON but token unresolvable → no_token, never bounce (F2/5e) ---
_heal_conf 1 n
_heal_reset
run_heal
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "0" ] && r=yes || r=no
harness_check "heal never bounces on no_token (a bounce cannot conjure a missing token)" "$r"

# ===========================================================================
# #608 — a tokenless canary/throwaway (EXPECT_NO_TOKEN=1) must NOT fire the
# no_token alert at BRING-UP or via FLEET-PULSE; a REAL bot with no token still
# MUST. Both hit the SAME classification (handle set, token unresolvable →
# no_token); the ONLY difference is the marker, so any divergence below IS the
# exemption. Drive the REAL bringup + down-state deciders (the exact functions
# that choose to alert), so the fix is OBSERVED end-to-end — not just composed.
echo ""
echo "=== validate no_token canary exemption (#608: EXPECT_NO_TOKEN gates the alert) ==="
NTBOTS="$ROOT/local/$FLEET/runtime/bots"
NTC="$NTBOTS/valcanary"   # EXPECT_NO_TOKEN=1 → exempt throwaway
NTR="$NTBOTS/valreal"     # no marker → real bot, a missing token is a fault
NTEV="$ROOT/state/events"
_nt_conf() {   # $1 = bot dir   $2 = y|n (carry the canary marker?). Handle set, token absent.
    local d="$1" b; b="$(basename "$1")"
    mkdir -p "$d/data"
    cat > "$d/bot.conf" <<CONF
BOT_NAME="$b"
BOT_ID="$b"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
TELEGRAM_BOT_HANDLE="$b"
TELEGRAM_TOKEN_ENV_NAME=NT_ABSENT_TOKEN
CONF
    [ "$2" = y ] && printf 'EXPECT_NO_TOKEN=1\n' >> "$d/bot.conf" || true
}
_nt_conf "$NTC" y
_nt_conf "$NTR" n

# Sanity: identical token-absent input — both classify as no_token.
{ [ "$(bridge_state "$NTC" 2>/dev/null || true)" = "no_token" ] \
  && [ "$(bridge_state "$NTR" 2>/dev/null || true)" = "no_token" ]; } && r=yes || r=no
harness_check "#608 canary + real both classify no_token (same token-absent input)" "$r"

# --- Bring-up path (bridge_bringup_verify): canary silent, real escalates ---
ntv="$(CLAUDLOBBY_ROOT="$ROOT" bridge_bringup_verify "$NTC" "$(dirname "$NTC")" 0 2>/dev/null || true)"
[ "$ntv" = "expected:no_token" ] && r=yes || r=no
harness_check "#608 canary bring-up verdict is expected:no_token (marker exempts)" "$r"
grep -q 'valcanary Telegram bridge no_token' "$NTEV"/fleet-*.jsonl 2>/dev/null && r=no || r=yes
harness_check "#608 canary bring-up emits NO bridge_down alert (no fleet event)" "$r"

ntv="$(CLAUDLOBBY_ROOT="$ROOT" bridge_bringup_verify "$NTR" "$(dirname "$NTR")" 0 2>/dev/null || true)"
[ "$ntv" = "missing:no_token" ] && r=yes || r=no
harness_check "#608 real-bot bring-up verdict is missing:no_token (still a fault)" "$r"
grep -q 'valreal Telegram bridge no_token at bring-up' "$NTEV"/fleet-*.jsonl 2>/dev/null && r=yes || r=no
harness_check "#608 real-bot bring-up DOES emit the no_token bridge_down alert" "$r"

# --- Fleet-pulse path (bridge_down_state, grace 0): canary not-down, real down ---
CLAUDLOBBY_ROOT="$ROOT" bridge_down_state "$NTC" 0 >/dev/null 2>&1 && r=no || r=yes  # rc 0 = down
harness_check "#608 canary is NOT actionably down for fleet-pulse (no pulse alert)" "$r"
[ "$(CLAUDLOBBY_ROOT="$ROOT" bridge_down_state "$NTR" 0 2>/dev/null || true)" = "no_token" ] && r=yes || r=no
harness_check "#608 real bot IS actionably down (no_token) for fleet-pulse" "$r"

# ===========================================================================
# The no_bridge bring-up alert must tell the truth about auto-heal. keepalive's
# _bridge_heal is gated OFF fleet-wide (OBSERVABILITY_BRIDGE_HEAL != 1), so an
# alert that promises "keepalive will heal" lies whenever the gate is off — and
# even when on, the poller is an MCP stdio child of claude, so the only lever is
# a full bot bounce, never a gentle in-place respawn. Reuse the #453 dark-bridge
# fixture (valheal: no bot.pid -> no_bridge) and drive the REAL
# bridge_bringup_verify in both gate states — the verify reads the gate from the
# env, so one fixture serves both. Assert the emitted alert text tracks reality.
echo ""
echo "=== validate bridge_down heal-honesty (OBSERVABILITY_BRIDGE_HEAL gates the wording) ==="
_heal_conf 0 y   # token present, dark poller -> no_bridge (gate in bot.conf is inert here; verify reads the env)

# Sanity: token resolves + poller absent classifies no_bridge (not no_token/no_handle).
[ "$(bridge_state "$HDIR" 2>/dev/null || true)" = "no_bridge" ] && r=yes || r=no
harness_check "heal-honesty fixture classifies no_bridge (token set, poller absent)" "$r"

# --- Gate OFF (fleet default): the alert must NOT promise a heal that never runs ---
bhv="$(CLAUDLOBBY_ROOT="$ROOT" OBSERVABILITY_BRIDGE_HEAL=0 \
    bridge_bringup_verify "$HDIR" "$(dirname "$HDIR")" 0 2>/dev/null || true)"
[ "$bhv" = "missing:no_bridge" ] && r=yes || r=no
harness_check "gate-off bring-up verdict is missing:no_bridge (unchanged)" "$r"
bhoff="$(grep -h 'valheal Telegram bridge down at bring-up' "$NTEV"/fleet-*.jsonl 2>/dev/null | tail -n1 || true)"
printf '%s' "$bhoff" | grep -q 'dark until restart' && r=yes || r=no
harness_check "gate-off alert states inbound dark until restart (honest, mirrors no_token)" "$r"
printf '%s' "$bhoff" | grep -q 'keepalive will heal' && r=no || r=yes
harness_check "gate-off alert drops the false 'keepalive will heal' promise" "$r"

# --- Gate ON: the alert states a bounce (full claude restart), not a respawn ---
CLAUDLOBBY_ROOT="$ROOT" OBSERVABILITY_BRIDGE_HEAL=1 \
    bridge_bringup_verify "$HDIR" "$(dirname "$HDIR")" 0 >/dev/null 2>&1 || true
grep -hq 'valheal Telegram bridge down at bring-up.*bounce' "$NTEV"/fleet-*.jsonl 2>/dev/null && r=yes || r=no
harness_check "gate-on alert states keepalive will bounce to recover" "$r"

# ===========================================================================
# #579 — the dead-session path must emit a RESTART line the uptime parser reads.
# navi's #577 review: test_uptime.py only feeds the PARSER a hand-written sample;
# nothing drove keepalive's real dead-session branch to prove it EMITS a line the
# parser recognizes — so the #577 restart_bot_service extraction left that wording
# one refactor from silently drifting out of uptime.py's _LOG_LINE_RE. Drive the
# real path (a session-less bot, via the HLIB recorder stub so nothing truly
# restarts) and assert the REAL parser extracts a RESTART from the emitted log.
echo ""
echo "=== validate dead-session RESTART line (#579: keepalive emitter ⇄ uptime parser) ==="
DBOT="valdead"
DDIR="$ROOT/local/$FLEET/runtime/bots/$DBOT"
mkdir -p "$DDIR/data"
cat > "$DDIR/bot.conf" <<CONF
BOT_NAME="$DBOT"
BOT_ID="$DBOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
# No tmux session for valdead → keepalive takes the dead-session branch. The RESTART
# log line is echoed before the restart action fires, so it lands regardless of the
# (stubbed) restart.
CLAUDLOBBY_ROOT="$ROOT" "$HLIB/keepalive.sh" "$DDIR" >/dev/null 2>&1 || true
grep -qE 'RESTART.*session dead' "$DDIR/keepalive.log" 2>/dev/null && r=yes || r=no
harness_check "keepalive dead-session path emits a RESTART … session dead log line" "$r"
# Load-bearing assertion: the REAL uptime parser (parse_keepalive_log, backed by
# _LOG_LINE_RE) must extract a RESTART from that emitted line — the emitter⇄parser
# coupling navi flagged as guarded only by a hand-written fixture until now.
dead_restarts=$(python3 -c "
import sys; sys.path.insert(0, '$LIB_DIR/..')
from claudlobby.uptime import parse_keepalive_log
from pathlib import Path
print(sum(1 for _, s in parse_keepalive_log(Path('$DDIR/keepalive.log')) if s == 'RESTART'))
" 2>/dev/null || echo 0)
[ "${dead_restarts:-0}" -ge 1 ] && r=yes || r=no
harness_check "uptime parser extracts a RESTART from the real emitted keepalive.log (#579)" "$r"

# Regression guard: send_reload_command must resend Enter ONLY when the TUI
# swallowed it (command still on the input line), never after a clean submit.
# This fixture consumes each submitted line and redraws a fresh prompt below it,
# mimicking Claude Code — where a submitted command scrolls up into the transcript
# but stays visible in the pane. A verify scoped to the whole pane matches that
# scrolled-up text and fires a spurious empty Enter at the idle prompt; one scoped
# to the bottom input line does not. Assert exactly one submission per reload
# command (2 total) — i.e. zero spurious Enters. The idle checks above only assert
# the commands appear, so without this a widened/deleted verify regresses silently.
SBOT_DIR="$ROOT/local/$FLEET/runtime/bots/$SBOT"
mkdir -p "$SBOT_DIR/data"
cat > "$SBOT_DIR/bot.conf" <<CONF
BOT_NAME="$SBOT"
BOT_ID="$SBOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
SUBMIT_LOG="$SBOT_DIR/submits.log"
: > "$SUBMIT_LOG"
# Idle prompt so keepalive takes the reload branch; each read logs the submission,
# then prints it and pushes it well above the bottom input line, so only a
# genuinely-unsubmitted command is still at the prompt for the verify to match.
#
# The prompt is drawn inside a bordered box with footer lines BELOW it, because
# that is where Claude Code puts it — the input line is never the last line of
# the pane. A fixture that put the prompt last would let a verify that only ever
# reads the last few lines pass here while never once reaching the input line in
# production, which is exactly how the pre-#763 `tail -3` verify shipped dead.
cat > "$SBOT_DIR/fixture.sh" <<FIX
#!/bin/bash
draw_prompt() {
    printf -- '--------\n> \n--------\n\n  auto mode on\n'
}
draw_prompt
while IFS= read -r l; do
    printf '%s\n' "\$l" >> "$SUBMIT_LOG"
    printf 'sent[%s]\n\n\n\n\n\n\n' "\$l"
    draw_prompt
done
FIX
tmux new-session -d -s "$SBOT" "bash '$SBOT_DIR/fixture.sh'"
sleep 1
touch "$SBOT_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/keepalive.sh" "$SBOT_DIR" >/dev/null 2>&1 || true
sleep 1
submits=$(wc -l < "$SUBMIT_LOG")
[ "$submits" -eq 2 ] && r=yes || r=no
harness_check "send_reload_command fires no spurious Enter on clean submit (verify scoped to prompt)" "$r"

# === Scenario 2: lossless restart — age-gated resume injection on start ===
# Drive the REAL start-bot.sh against a throwaway bot whose `claude` is a stub
# injected via CLAUDE_BIN (prints the readiness string, then `cat` so send-keys
# echo into the pane), with plugin management off (empty FLEET_PLUGINS_REQUIRED)
# so the start is hermetic and fast — no real auth, MCP, or plugin network call.
# A fresh session.md -> /claudna:session resume is sent BEFORE STARTUP_PROMPT; a
# stale one -> resume skipped (clean start).
RB_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-validate-rb.XXXXXX")"
RB_DIR="$RB_ROOT/local/$FLEET/runtime/bots/valrb"
mkdir -p "$RB_DIR/.claude" "$RB_DIR/logs" "$RB_ROOT/bin" "$RB_ROOT/tmp"
# Controlled HOME with consent pre-accepted. start-bot.sh's consent block runs
# with_lock on $HOME/.claude/settings.json.lock, which fails on a fresh HOME (no
# .claude dir yet) under set -e — a CI runner's empty HOME would abort start-bot.sh
# before the resume injection ever fires. Pinning HOME keeps the scenario hermetic.
RB_HOME="$RB_ROOT/home"
mkdir -p "$RB_HOME/.claude"
printf '{"skipAutoPermissionPrompt":true,"skipDangerousModePermissionPrompt":true}\n' > "$RB_HOME/.claude/settings.json"
cat > "$RB_ROOT/bin/claude" <<'STUB'
#!/bin/bash
exec cat
STUB
chmod +x "$RB_ROOT/bin/claude"
cat > "$RB_DIR/bot.conf" <<CONF
BOT_NAME="valrb"
BOT_ID="valrb"
BOT_LABEL="valrb"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
STARTUP_PROMPT="ZZZ_STARTUPMARK"
FLEET_PLUGINS_REQUIRED=""
CONF
RB_SESSION="$(tmux_session_name "$RB_DIR")"

_run_startbot() {  # $1 = fresh|stale -> echo the resulting pane
    local iso
    if [ "$1" = fresh ]; then iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; else iso="2020-01-01T00:00:00Z"; fi
    printf -- '---\ncwd: %s\nlast_updated: %s\nschema_version: 2\n---\n' "$RB_DIR" "$iso" \
        > "$RB_DIR/.claude/session.md"
    tmux kill-session -t "$RB_SESSION" 2>/dev/null || true
    sleep 0.3
    TMPDIR="$RB_ROOT/tmp" BOOT_LOCK_HOLD_S=0 CLAUDE_BIN="$RB_ROOT/bin/claude" \
        HOME="$RB_HOME" PATH="$RB_ROOT/bin:$PATH" CLAUDLOBBY_ROOT="$RB_ROOT" \
        "$LIB_DIR/start-bot.sh" "$RB_DIR" >"$RB_ROOT/startbot.$1.out" 2>&1 || true
    sleep 1
    tmux capture-pane -t "$RB_SESSION" -p 2>/dev/null || true
}

echo ""
echo "=== validate-bot-change: lossless restart (resume on start, age-gated) ==="
_lossless_fail_before=$fail
pane_fresh="$(_run_startbot fresh)"
printf '%s' "$pane_fresh" | grep -q '/claudna:session resume' && r=yes || r=no
harness_check "fresh session.md -> /claudna:session resume injected on start" "$r"
_rln="$(printf '%s\n' "$pane_fresh" | grep -n '/claudna:session resume' | head -1 | cut -d: -f1 || true)"
_sln="$(printf '%s\n' "$pane_fresh" | grep -n 'ZZZ_STARTUPMARK' | head -1 | cut -d: -f1 || true)"
{ [ -n "$_rln" ] && [ -n "$_sln" ] && [ "$_rln" -lt "$_sln" ]; } && r=yes || r=no
harness_check "resume keystroke precedes STARTUP_PROMPT in the pane" "$r"
pane_stale="$(_run_startbot stale)"
printf '%s' "$pane_stale" | grep -q '/claudna:session resume' && r=no || r=yes
harness_check "stale session.md -> resume injection skipped (clean start)" "$r"
grep -q 'RESUME SKIP' "$RB_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "stale skip recorded in startup.log (RESUME SKIP)" "$r"

# Surface hermeticity evidence when the lossless checks fail (e.g. a CI runner
# where this scenario fails but a dev host passes): start-bot.sh's stderr is
# otherwise swallowed to a file we discard, hiding WHY the resume block did not
# run. Gated on failure so a passing run stays quiet.
if [ "$fail" -gt "$_lossless_fail_before" ]; then
    echo "  --- DIAGNOSTIC: lossless checks failed; dumping start-bot evidence ---"
    echo "  [tmux] $(tmux -V 2>&1)"
    echo "  [start-bot fresh stdout+stderr]"
    sed 's/^/    /' "$RB_ROOT/startbot.fresh.out" 2>/dev/null || echo "    (none)"
    echo "  [start-bot stale stdout+stderr]"
    sed 's/^/    /' "$RB_ROOT/startbot.stale.out" 2>/dev/null || echo "    (none)"
    echo "  [startup.log]"
    sed 's/^/    /' "$RB_DIR/logs/startup.log" 2>/dev/null || echo "    (none)"
    echo "  [fresh pane]"; printf '%s\n' "$pane_fresh" | sed 's/^/    /'
    echo "  [stale pane]"; printf '%s\n' "$pane_stale" | sed 's/^/    /'
    echo "  ----------------------------------------------------------------"
fi

# === Scenario 2b: readiness probe — READY vs TIMEOUT + rc_timeout event (#533/#751) ===
# Same REAL start-bot.sh. The probe now asserts bridge_state ground truth, not a
# bring-up pane string (#751: the old `remote-control is active` grep drifted out of
# current builds and false-fired rc_timeout on every start). Positive path first
# (the fresh/stale valrb above has no TELEGRAM_BOT_HANDLE -> bridge_state no_handle
# -> ready, no poller to await): READY logged, NO rc_timeout. Then the negative
# path: a CHANNEL bot whose poller never wrote a live bot.pid -> bridge_state
# no_bridge -> the probe must TIMEOUT and emit the (now true-positive) rc_timeout
# event fleet-pulse escalates. Empirical proof of #533 items 3-4 + the #751 fix
# (unit tests prove composition; only running start-bot proves the event fires —
# and, for #751, that a ready bot no longer FALSELY fires). RC_READY_TIMEOUT_S=1
# keeps the negative run fast — the 90s default is untestable in a harness (poll 2
# is a single check now, so there is no second timeout to shorten).
echo ""
echo "=== validate-bot-change: readiness alerting (#533 items 3-4, #751) ==="
_rc_fail_before=$fail
grep -q 'READY —' "$RB_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "bridge_state ready (no_handle bot) -> READY recorded in startup.log" "$r"
grep -rq '"type":"rc_timeout"' "$RB_DIR/data/events/" 2>/dev/null && r=no || r=yes
harness_check "ready verdict -> no rc_timeout event (no false alarm, #751)" "$r"

# Negative: reconfigure valrb as a CHANNEL bot (handle + a resolvable token) whose
# poller never came up — no bot.pid under TELEGRAM_STATE_DIR -> bridge_state stays
# no_bridge -> the probe times out and emits the now-true-positive rc_timeout.
cat > "$RB_ROOT/bin/claude" <<'STUB'
#!/bin/bash
exec cat
STUB
chmod +x "$RB_ROOT/bin/claude"
cat >> "$RB_DIR/bot.conf" <<CONF
TELEGRAM_BOT_HANDLE="valrb"
TELEGRAM_TOKEN_ENV_NAME="VALRB_TOKEN"
TELEGRAM_STATE_DIR="$RB_DIR/state"
CONF
printf 'VALRB_TOKEN=8888888:AAAAAAAAAAAAAAAAAAAA\n' > "$RB_DIR/.env"
mkdir -p "$RB_DIR/state"   # exists, but no bot.pid -> bridge_state no_bridge
rm -rf "$RB_DIR/data/events" 2>/dev/null || true
tmux kill-session -t "$RB_SESSION" 2>/dev/null || true
sleep 0.3
printf -- '---\ncwd: %s\nlast_updated: %s\nschema_version: 2\n---\n' "$RB_DIR" "2020-01-01T00:00:00Z" \
    > "$RB_DIR/.claude/session.md"
TMPDIR="$RB_ROOT/tmp" BOOT_LOCK_HOLD_S=0 RC_READY_TIMEOUT_S=1 \
    CLAUDE_BIN="$RB_ROOT/bin/claude" \
    HOME="$RB_HOME" PATH="$RB_ROOT/bin:$PATH" CLAUDLOBBY_ROOT="$RB_ROOT" \
    "$LIB_DIR/start-bot.sh" "$RB_DIR" >"$RB_ROOT/startbot.timeout.out" 2>&1 || true
sleep 1
grep -q 'TIMEOUT' "$RB_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "no RC string -> TIMEOUT recorded in startup.log" "$r"
_rcev="$(grep -rl '"type":"rc_timeout"' "$RB_DIR/data/events/" 2>/dev/null | head -1 || true)"
[ -n "$_rcev" ] && r=yes || r=no
harness_check "TIMEOUT emits an rc_timeout fleet event (fleet-pulse escalation input)" "$r"
if [ -n "$_rcev" ]; then
    python3 -c "import sys,json; e=json.loads(open('$_rcev').readline()); sys.exit(0 if e['type']=='rc_timeout' and e['ts'] else 1)" 2>/dev/null && r=yes || r=no
else r=no; fi
harness_check "rc_timeout event is valid JSON with ts+type (fleet-pulse-readable)" "$r"

if [ "$fail" -gt "$_rc_fail_before" ]; then
    echo "  --- DIAGNOSTIC: RC readiness checks failed ---"
    echo "  [startup.log]"; sed 's/^/    /' "$RB_DIR/logs/startup.log" 2>/dev/null || echo "    (none)"
    echo "  [events]"; sed 's/^/    /' "$RB_DIR/data/events/"fleet-*.jsonl 2>/dev/null || echo "    (none)"
    echo "  [start-bot timeout stdout+stderr]"; sed 's/^/    /' "$RB_ROOT/startbot.timeout.out" 2>/dev/null || echo "    (none)"
fi

# === Scenario 2c: RC readiness ESCALATION — fleet-pulse pages on an rc_timeout burst (#533) ===
# 2b proved start-bot EMITS rc_timeout. This proves the downstream half: fleet-pulse reads
# that event from >= threshold bots within the window and FIRES the escalation page. The real
# fleet-pulse runs from a stub lib dir whose tg-post.sh RECORDS the page instead of sending it,
# so the assertion is the alert message the burst-detector actually produced. A single bot
# (below threshold) must stay silent. 2b + 2c together cover #533 items 3-4 end-to-end: emit
# then escalate. The incidental service_down / session_missing pages are the sandbox bots
# having no live session; the assertions target the rc_timeout line.
echo ""
echo "=== validate-bot-change: RC readiness ESCALATION page (#533 items 3-4) ==="
_esc_fail_before=$fail
_esc_fleet="valesc"
_esc_lib="$ROOT/esclib"
mkdir -p "$_esc_lib"
ln -s "$LIB_DIR/fleet-pulse.sh" "$_esc_lib/fleet-pulse.sh"
ln -s "$LIB_DIR/lib-common.sh"  "$_esc_lib/lib-common.sh"
_esc_pages="$ROOT/esc-pages.log"
: > "$_esc_pages"
cat > "$_esc_lib/tg-post.sh" <<STUB
#!/bin/bash
printf '%s\n' "\$1" >> "$_esc_pages"
STUB
chmod +x "$_esc_lib/tg-post.sh"
_esc_bots="$ROOT/local/$_esc_fleet/runtime/bots"

esc_seed() {  # <bot> <emit rc_timeout: yes|no> — seed a sandbox bot, optionally with a timeout event
    mkdir -p "$_esc_bots/$1"
    printf 'BOT_SERVICE=%s\n' "$1" > "$_esc_bots/$1/bot.conf"
    # Route the seed through the SAME helper start-bot.sh emits rc_timeout with, so the
    # seeded ledger row can never drift from the real emitter schema (it computes ts + the
    # fleet-<today>.jsonl path internally, matching what fleet-pulse then reads).
    if [ "$2" = yes ]; then
        emit_fleet_event rc_timeout startup '{}' "$_esc_bots/$1" "$1"
    fi
    return 0
}
esc_run() {
    CLAUDLOBBY_ROOT="$ROOT" FLEET_PULSE_ESCALATION_CHAT_ID="-100999" \
        "$_esc_lib/fleet-pulse.sh" "$_esc_fleet" >/dev/null 2>&1 || true
}

# Positive: 2 bots TIMEOUT within the window (== default threshold) -> page fires.
esc_seed escone yes
esc_seed esctwo yes
esc_run
grep -q 'FLEET ALERT: rc_timeout on 2 bots' "$_esc_pages" && r=yes || r=no
harness_check "rc_timeout burst on >= threshold bots FIRES the escalation page" "$r"

# Negative: only 1 bot with rc_timeout -> below threshold -> no rc_timeout page.
rm -rf "$_esc_bots"
mkdir -p "$_esc_bots"
: > "$_esc_pages"
esc_seed escone yes
esc_seed esctwo no
esc_run
grep -q 'rc_timeout' "$_esc_pages" && r=no || r=yes
harness_check "a single rc_timeout (below threshold) does NOT page (no false alarm)" "$r"

if [ "$fail" -gt "$_esc_fail_before" ]; then
    echo "  --- DIAGNOSTIC: escalation pages recorded ---"
    sed 's/^/    /' "$_esc_pages" 2>/dev/null || echo "    (none)"
fi

# === Scenario 2d: plugin marketplace registration — positional add, verified, loud on failure (#596) ===
# Same REAL start-bot.sh, plugin management ON (non-empty FLEET_PLUGINS_REQUIRED)
# with `claude` stubbed via the CLAUDE_BIN seam (the PATH rebuild inside
# start-bot discards a stub dir, so the seam is the only hermetic route; the
# same stub serves the pane). ONE run covers both paths: valmarket registers
# (the stub writes the registry like the real CLI), valmarketbad fails (stub
# exits 1, registers nothing). start-bot must use the positional
# `plugin marketplace add <owner>/<repo>` form (dead flags are how a fleet
# silently lands on the wrong plugin), verify + log valmarket, and for
# valmarketbad log PLUGIN ERROR + emit a plugin_marketplace_failed fleet
# event — loud, but never startup-blocking.
echo ""
echo "=== validate-bot-change: marketplace registration (#596: positional + verified + loud) ==="
_mp_fail_before=$fail
MP_DIR="$RB_ROOT/local/$FLEET/runtime/bots/valmp"
mkdir -p "$MP_DIR/.claude" "$MP_DIR/logs"
cat > "$MP_DIR/bot.conf" <<CONF
BOT_NAME="valmp"
BOT_ID="valmp"
BOT_LABEL="valmp"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
FLEET_PLUGINS_REQUIRED="demo@valmarket"
FLEET_PLUGINS_MARKETPLACES="valmarket=github:ExampleOrg/example-plugins valmarketbad=github:ExampleOrg/does-not-exist"
CONF
MP_SESSION="$(tmux_session_name "$MP_DIR")"
cat > "$RB_ROOT/bin/claude" <<STUB
#!/bin/bash
if [ "\$1" = plugin ]; then
    printf '%s\n' "\$*" >> "$RB_ROOT/plugin-argv.log"
    if [ "\$2" = marketplace ] && [ "\$3" = add ]; then
        [ "\$4" = ExampleOrg/does-not-exist ] && exit 1
        mkdir -p "\$HOME/.claude/plugins"
        printf '{"valmarket":{}}\n' > "\$HOME/.claude/plugins/known_marketplaces.json"
    fi
    exit 0
fi
exec cat
STUB
chmod +x "$RB_ROOT/bin/claude"
TMPDIR="$RB_ROOT/tmp" BOOT_LOCK_HOLD_S=0 RC_READY_TIMEOUT_S=10 CLAUDE_BIN="$RB_ROOT/bin/claude" \
    HOME="$RB_HOME" PATH="$RB_ROOT/bin:$PATH" CLAUDLOBBY_ROOT="$RB_ROOT" \
    "$LIB_DIR/start-bot.sh" "$MP_DIR" >"$RB_ROOT/startbot.mp.out" 2>&1 || true
grep -qx 'plugin marketplace add ExampleOrg/example-plugins' "$RB_ROOT/plugin-argv.log" 2>/dev/null && r=yes || r=no
harness_check "marketplace add uses the positional owner/repo form (no dead flags)" "$r"
grep -q 'PLUGIN marketplace valmarket registered' "$MP_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "registration verified against known_marketplaces.json + logged" "$r"
grep -q 'PLUGIN ERROR marketplace valmarketbad' "$MP_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "failed registration logs PLUGIN ERROR (not swallowed)" "$r"
_mpev="$(grep -rl '"type":"plugin_marketplace_failed"' "$MP_DIR/data/events/" 2>/dev/null | head -1 || true)"
[ -n "$_mpev" ] && r=yes || r=no
harness_check "failed registration emits plugin_marketplace_failed fleet event (loud, not silent)" "$r"
if [ -n "$_mpev" ]; then
    python3 -c "import sys,json; evs=[json.loads(l) for l in open('$_mpev') if 'plugin_marketplace_failed' in l]; sys.exit(0 if len(evs)==1 and evs[0]['data']['marketplace']=='valmarketbad' else 1)" 2>/dev/null && r=yes || r=no
else r=no; fi
harness_check "exactly one failure event, and it names valmarketbad (success stays quiet)" "$r"
grep -q 'READY —' "$MP_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "registration failure does NOT block startup (session still comes up)" "$r"

if [ "$fail" -gt "$_mp_fail_before" ]; then
    echo "  --- DIAGNOSTIC: marketplace registration checks failed ---"
    echo "  [plugin argv]"; sed 's/^/    /' "$RB_ROOT/plugin-argv.log" 2>/dev/null || echo "    (none)"
    echo "  [valmp startup.log]"; sed 's/^/    /' "$MP_DIR/logs/startup.log" 2>/dev/null || echo "    (none)"
    echo "  [valmp events]"; sed 's/^/    /' "$MP_DIR/data/events/"fleet-*.jsonl 2>/dev/null || echo "    (none)"
fi

# === Scenario 3: weekly worker-only restart — manager skip + loud failure ===
# Run weekly-worker-restart.sh from a stub lib dir (stub spin-up-bot FAILS, so
# the loud emit_failure_alert path is exercised too). The manager (MANAGER_TMUX==BOT_ID)
# must be skipped; the worker must be processed.
WR_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-validate-wr.XXXXXX")"
WR_LIB="$WR_ROOT/lib"
mkdir -p "$WR_LIB"
cp "$LIB_DIR/lib-common.sh" "$LIB_DIR/weekly-worker-restart.sh" "$WR_LIB/"
printf '#!/bin/bash\nexit 0\n' > "$WR_LIB/pre-stop-handoff.sh"
printf '#!/bin/bash\necho "stub spin-up: $1" >&2\nexit 7\n' > "$WR_LIB/spin-up-bot.sh"
chmod +x "$WR_LIB/pre-stop-handoff.sh" "$WR_LIB/spin-up-bot.sh"
WR_BOTS="$WR_ROOT/local/$FLEET/runtime/bots"
mkdir -p "$WR_BOTS/wmgr/data" "$WR_BOTS/wworker/data"
printf 'BOT_ID=wmgr\nMANAGER_TMUX=wmgr  # this bot is a manager\n' > "$WR_BOTS/wmgr/bot.conf"
printf 'BOT_ID=wworker\nMANAGER_TMUX=wmgr\n' > "$WR_BOTS/wworker/bot.conf"
CLAUDLOBBY_ROOT="$WR_ROOT" "$WR_LIB/weekly-worker-restart.sh" "$FLEET" >/dev/null 2>&1 || true
wr_log="$WR_ROOT/state/weekly-worker-restart.log"
wr_events="$(ls "$WR_ROOT/state/events"/fleet-*.jsonl 2>/dev/null | head -1 || true)"

echo ""
echo "=== validate-bot-change: weekly worker-only restart ==="
grep -q 'skip (manager): wmgr' "$wr_log" 2>/dev/null && r=yes || r=no
harness_check "weekly restart SKIPS the manager (MANAGER_TMUX==BOT_ID)" "$r"
grep -q 'worker: wworker' "$wr_log" 2>/dev/null && r=yes || r=no
harness_check "weekly restart PROCESSES the worker" "$r"
grep -q 'worker: wmgr' "$wr_log" 2>/dev/null && r=no || r=yes
harness_check "manager never entered the worker restart path" "$r"
{ [ -n "$wr_events" ] && grep -q '"type":"restart_failed"' "$wr_events"; } && r=yes || r=no
harness_check "worker restart failure raises a restart_failed alert (shared emit_failure_alert)" "$r"

# === Scenario 4: daily bounce retired from update-claude-code.sh (static) ===
echo ""
echo "=== validate-bot-change: daily bounce retired (download-only) ==="
grep -Eq 'BOUNCE|spin-up-bot\.sh' "$LIB_DIR/update-claude-code.sh" && r=no || r=yes
harness_check "update-claude-code.sh no longer bounces the fleet" "$r"
grep -q 'npm install -g @anthropic-ai/claude-code@latest' "$LIB_DIR/update-claude-code.sh" && r=yes || r=no
harness_check "update-claude-code.sh still downloads the binary daily" "$r"

# ===========================================================================
# #415 — fleet.yaml-authoritative discovery + pane_stuck idle-guard.
#   (1) An UNDECLARED runtime dir (stale/cross-fleet residue — e.g. a bot moved
#       to another fleet, leaving its old dir behind) must be SKIPPED: zero pulse
#       events. RED before #415 — the filesystem-glob loop health-checked every
#       dir and emitted session_missing/service_down/pane_stuck for orphans
#       (the craig/greg bug).
#   (2) pane_stuck must honor the .idle marker like activity_stuck does: a bot
#       parked at an idle prompt has a stable pane — that is idle, not stuck.
# ===========================================================================
echo ""
echo "=== validate #415: fleet.yaml discovery filter + pane_stuck idle-guard ==="

F2="valf415"
F2_BOTS="$ROOT/local/$F2/runtime/bots"
KEEP="valkeep"; ORPH="valorphan"; IDLEK="validlek"
mkdir -p "$ROOT/local/$F2" "$F2_BOTS/$KEEP/data" "$F2_BOTS/$ORPH/data" "$F2_BOTS/$IDLEK/data"

# fleet.yaml declares KEEP + IDLEK but NOT ORPH (the residue analogue).
cat > "$ROOT/local/$F2/fleet.yaml" <<YAML
fleet:
  name: $F2
  bots:
    $KEEP:
      expertise: [software-engineering]
    $IDLEK:
      expertise: [software-engineering]
YAML
for b in "$KEEP" "$ORPH" "$IDLEK"; do
    cat > "$F2_BOTS/$b/bot.conf" <<CONF
BOT_NAME="$b"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
done

# IDLEK gets a live idle pane so its pane-hash state can seed; KEEP + ORPH get no
# session, so the old glob loop would emit session_missing for BOTH.
tmux new-session -d -s "$IDLEK" 'printf "\n> \n"; sleep 600'
sleep 1

# Run 1: seeds IDLEK pane hash/ts; health-checks declared bots.
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/fleet-pulse.sh" "$F2" >/dev/null 2>&1 || true

# Make IDLEK idle (.idle newer than .last-tool-call) and backdate its pane ts so
# the next sweep sees elapsed >= 300 without a real 5-minute wait.
touch "$F2_BOTS/$IDLEK/data/.last-tool-call"; sleep 1; touch "$F2_BOTS/$IDLEK/data/.idle"
_now415=$(date +%s); printf '%s' "$((_now415 - 400))" > "$ROOT/state/pulse/$IDLEK.pane_ts"

# Run 2: IDLEK pane unchanged + elapsed 400 would trip pane_stuck — idle-guard must suppress.
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/fleet-pulse.sh" "$F2" >/dev/null 2>&1 || true

keep_ev=$(ls "$F2_BOTS/$KEEP/data/events"/fleet-*.jsonl 2>/dev/null | head -1 || true)
orph_ev=$(ls "$F2_BOTS/$ORPH/data/events"/fleet-*.jsonl 2>/dev/null | head -1 || true)
idlek_ev=$(ls "$F2_BOTS/$IDLEK/data/events"/fleet-*.jsonl 2>/dev/null | head -1 || true)

[ -n "$keep_ev" ] && grep -q '"type":"session_missing"' "$keep_ev" && r=yes || r=no
harness_check "#415 declared bot is still health-checked (session_missing fired for $KEEP)" "$r"

if [ -z "$orph_ev" ]; then r=yes
elif grep -qE '"type":"(session_missing|service_down|pane_stuck)"' "$orph_ev"; then r=no
else r=yes; fi
harness_check "#415 undeclared orphan dir emits ZERO pulse events (filtered via fleet.yaml)" "$r"

[ -n "$idlek_ev" ] && grep -q '"type":"pane_stuck"' "$idlek_ev" && r=no || r=yes
harness_check "#415 pane_stuck suppressed for an idle-at-prompt bot (.idle guard)" "$r"

# ===========================================================================
# #611 summary socket resolution + pane_stuck busy-guard (this PR).
#   (a) #611: the SUMMARY must resolve the tmux socket the SAME way the main loop
#       does (tmux_socket_for_bot), else a bot whose TMUX_SOCKET != BOT_SERVICE
#       shows a false SESSION DOWN in the summary while the main loop sees it up.
#   (b) pane_stuck must NOT fire on a WORKING bot: a fresh data/.last-tool-call
#       (mid-tool-call) or an "esc to interrupt" pane (active turn / waiting on a
#       subagent) is busy, not stuck.
# ===========================================================================
echo ""
echo "=== validate #611 summary socket + pane_stuck busy-guard (this PR) ==="

# (a) #611 repro: TMUX_SOCKET points at the live server, BOT_SERVICE is a
#     DIFFERENT string. The shadow tmux() puts the session on tmux-valsock.
SOCKB="valsock"
mkdir -p "$F2_BOTS/$SOCKB/data"
cat > "$F2_BOTS/$SOCKB/bot.conf" <<CONF
BOT_NAME="$SOCKB"
BOT_SERVICE="com.diverge.$SOCKB"
TMUX_SOCKET="tmux-$SOCKB"
MANAGER_TMUX="$MGR"
CONF
tmux new-session -d -s "$SOCKB" 'printf "\n> \n"; sleep 600'

# (b) pane_stuck busy repros: BUSYM = fresh .last-tool-call (mid-tool-call),
#     BUSYP = "esc to interrupt" pane (active turn). Both static + backdated.
BUSYM="valbusym"; BUSYP="valbusyp"
mkdir -p "$F2_BOTS/$BUSYM/data" "$F2_BOTS/$BUSYP/data"
for b in "$BUSYM" "$BUSYP"; do
    cat > "$F2_BOTS/$b/bot.conf" <<CONF
BOT_NAME="$b"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
done
tmux new-session -d -s "$BUSYM" 'printf "\n> \n"; sleep 600'
tmux new-session -d -s "$BUSYP" 'printf "\nWorking (esc to interrupt)\n"; sleep 600'
sleep 1

# Declare the three new bots so fleet-pulse does not filter them as orphans (#415).
cat > "$ROOT/local/$F2/fleet.yaml" <<YAML
fleet:
  name: $F2
  bots:
    $KEEP:
      expertise: [software-engineering]
    $IDLEK:
      expertise: [software-engineering]
    $SOCKB:
      expertise: [software-engineering]
    $BUSYM:
      expertise: [software-engineering]
    $BUSYP:
      expertise: [software-engineering]
YAML

# Run 3: seed pane hashes/ts for the new bots.
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/fleet-pulse.sh" "$F2" >/dev/null 2>&1 || true

# Mark BUSYM "working" (fresh marker, no .idle) and backdate both busy bots past
# the 300s pane threshold; BUSYP stays marker-less (its esc-to-interrupt pane is
# the only busy signal).
_n611=$(date +%s)
touch "$F2_BOTS/$BUSYM/data/.last-tool-call"
printf '%s' "$((_n611 - 400))" > "$ROOT/state/pulse/$BUSYM.pane_ts"
printf '%s' "$((_n611 - 400))" > "$ROOT/state/pulse/$BUSYP.pane_ts"

# Run 4: the sweep where pane_stuck would trip for the busy bots + the summary runs.
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/fleet-pulse.sh" "$F2" >/dev/null 2>&1 || true

busym_ev=$(ls "$F2_BOTS/$BUSYM/data/events"/fleet-*.jsonl 2>/dev/null | head -1 || true)
busyp_ev=$(ls "$F2_BOTS/$BUSYP/data/events"/fleet-*.jsonl 2>/dev/null | head -1 || true)
{ [ -n "$busym_ev" ] && grep -q '"type":"pane_stuck"' "$busym_ev"; } && r=no || r=yes
harness_check "pane_stuck NOT fired for a working bot (fresh .last-tool-call, mid-tool-call)" "$r"
{ [ -n "$busyp_ev" ] && grep -q '"type":"pane_stuck"' "$busyp_ev"; } && r=no || r=yes
harness_check "pane_stuck NOT fired for a working bot (esc-to-interrupt pane, active turn)" "$r"

# #611: the summary must show the TMUX_SOCKET-only bot as up, not a false DOWN.
_sumfile="$ROOT/state/pulse/pulse-summary.txt"
printf '%s' "$(grep "^$SOCKB " "$_sumfile" 2>/dev/null || true)" | awk '{print $2}' | grep -qx up && r=yes || r=no
harness_check "#611 summary session=up for a bot whose TMUX_SOCKET != BOT_SERVICE" "$r"
# related: a BOT_SERVICE-less bot must not show a false SERVICE DOWN in the summary.
printf '%s' "$(grep "^$IDLEK " "$_sumfile" 2>/dev/null || true)" | awk '{print $3}' | grep -qx ok && r=yes || r=no
harness_check "#611 summary service=ok (not false DOWN) for a BOT_SERVICE-less bot" "$r"

# ===========================================================================
# Per-bot socket isolation (#414) — blast radius = 1 + observable misses.
# valmgr + valbot are up on DISTINCT private servers (tmux-valmgr/tmux-valbot),
# so a single server's death can no longer drop the whole fleet at once.
# ===========================================================================
echo ""
echo "=== validate #414: per-bot socket isolation (blast radius + send-miss) ==="

command tmux -L "$(vsock "$MGR")" has-session -t "$MGR" 2>/dev/null && r=yes || r=no
harness_check "#414 precondition: manager is up on its own private server" "$r"
command tmux -L "$(vsock "$BOT")" has-session -t "$BOT" 2>/dev/null && r=yes || r=no
harness_check "#414 precondition: worker is up on a DISTINCT private server" "$r"

# Crown jewel: kill ONE bot's whole server; only that bot dies.
command tmux -L "$(vsock "$BOT")" kill-server 2>/dev/null || true
sleep 0.3
command tmux -L "$(vsock "$BOT")" has-session -t "$BOT" 2>/dev/null && r=no || r=yes
harness_check "#414 blast radius: the killed bot's server is gone" "$r"
command tmux -L "$(vsock "$MGR")" has-session -t "$MGR" 2>/dev/null && r=yes || r=no
harness_check "#414 blast radius = 1: a peer SURVIVES the kill (shared-server SPOF removed)" "$r"

# Observable miss: a cross-socket send to a dead target lands a send_miss event
# in the caller's ledger — the silent `|| true` is gone.
BOT_DIR="$BOT_DIR" BOT_ID="$BOT" bash -c \
    '. "'"$LIB_DIR"'/lib-common.sh"; bot_tmux_send "tmux-valgone" "valgone" "ping"' \
    >/dev/null 2>&1 || true
{ ls "$EVENTS"/fleet-*.jsonl >/dev/null 2>&1 && grep -q '"type":"send_miss"' "$EVENTS"/fleet-*.jsonl; } && r=yes || r=no
harness_check "#414 send-miss: a cross-socket send to a dead target is logged, not silently dropped" "$r"

# ===========================================================================
# #591 Phase 1 — Telegram bridge poller-hijack mechanism (the regression gate
# for the poller-hijack fix plan). Proves, against the REAL installed plugin,
# that the boot-time reap in server.ts is last-writer-wins: a second instance
# started in the same state dir SIGTERMs the LIVE holder, takes the single
# getUpdates slot, and abandons it on exit (ownership-checked unlink) — the
# confirmed root cause of the permanent dark-bridge incidents. Promoted from
# the bot-local repro (17/17 x3 at root-cause time) onto the scaffolding it
# shares with the scenarios above: per-run private dirs, the check counters,
# the cleanup trap.
#
# Fake token only — the env prefix on each spawn also shields any real
# TELEGRAM_BOT_TOKEN in the caller's environment, so the pollers never reach
# a real bot. No tmux: pollers are raw bun processes held open on fifos,
# mirroring how claude runs the MCP (stdin EOF = the graceful
# client-disconnect path). No bot.conf either — keepalive is not involved
# (the heal half of the incident is the #453 scenario above). If this
# scenario ever grows a throwaway bot.conf: tmux_socket_for_bot refuses an
# empty BOT_SERVICE while FLEET_NAME is set — the harness-wide unset at the
# top is what makes the empty-BOT_SERVICE fixtures work.
#
# When the plugin learns to defer to a live holder (#591 Phases 3/4), the
# anchor probe below FAILS loudly — flip this scenario's expectations to
# "defer occurs" in the same change that adopts the fixed plugin.
echo ""
echo "=== validate bridge-hijack mechanism (#591 Phase 1: last-writer-wins reap) ==="

# Newest installed plugin version, never a pinned one (#591 P1 step 2). The
# cache root honors CLAUDE_CONFIG_DIR — multi-account bots keep their plugin
# cache under the account dir, same seam as start-bot.sh. A missing or empty
# cache just yields a path whose server.ts fails the probe below → SKIP.
BH_BASE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/cache/claude-plugins-official/telegram"
BH_PLUGIN="$BH_BASE/$(ls -1 "$BH_BASE" 2>/dev/null | sort -V | tail -n1 || true)"
BH_SKIP=""
command -v bun >/dev/null 2>&1 || BH_SKIP="bun not installed"
[ -n "$BH_SKIP" ] || [ -f "$BH_PLUGIN/server.ts" ] || BH_SKIP="telegram plugin not installed under $BH_BASE"

if [ -n "$BH_SKIP" ]; then
    # Dep-absent hosts (CI runners) skip clean with a reason — mirroring the
    # pytest skipif contract. Hosts WITH the deps are the enforcement point.
    echo "  SKIP  bridge-hijack scenario: $BH_SKIP (needs bun + the installed telegram plugin)"
else
    BH_DIR="$ROOT/bh"
    BH_STATE="$BH_DIR/state"
    mkdir -p "$BH_STATE"
    BH_TOKEN="8888888:AAAAAAAAAAAAAAAAAAAA"
    _bh_fail_before=$fail

    # Anchor probe — the promoted patch-anchor assertion (#591 P1 step 3).
    # The checks below encode EXACTLY this reap block. On drift, fail loud
    # and skip the spawns: a changed reap makes them meaningless, and
    # cascading FAILs would bury the real message. Deliberately a FAIL, not
    # a SKIP — a present-but-different plugin must never read as "covered".
    python3 - "$BH_PLUGIN/server.ts" 2>/dev/null <<'PY' && r=yes || r=no
import sys
src = open(sys.argv[1]).read()
block = """    process.kill(stale, 0)
    process.stderr.write(`telegram channel: replacing stale poller pid=${stale}\\n`)
    process.kill(stale, 'SIGTERM')"""
sys.exit(0 if src.count(block) == 1 else 1)
PY
    harness_check "plugin reap anchor present exactly once (drift fails loud, never tests nothing)" "$r"

    if [ "$r" != "yes" ]; then
        echo "  NOTE  plugin at $BH_PLUGIN no longer matches the hijack expectations — update this scenario alongside the plugin (#591 Phase 4)"
    else
        # spawn_poller <label> <fd-number> [prev_pid] — start the plugin the
        # way claude does (bun run ... start), stdin held open on a fifo so
        # the poller lives until its fd closes. MUST run in the main shell —
        # a subshell would take the fifo writer fd down with it. Fixed fd
        # numbers via eval keep this parseable by bash 3.2 (macOS /bin/bash;
        # exec {var}> auto-allocation is bash 4.1+). Sets BH_PID from
        # bot.pid, waiting for a value different from prev_pid.
        BH_PIDS=""
        spawn_poller() {
            local label="$1" fd="$2" prev="${3:-}" fifo i cur
            fifo="$BH_DIR/$label.fifo"
            mkfifo "$fifo"
            ( TELEGRAM_STATE_DIR="$BH_STATE" TELEGRAM_BOT_TOKEN="$BH_TOKEN" \
                bun run --cwd "$BH_PLUGIN" --shell=bun --silent start \
                < "$fifo" > "$BH_DIR/$label.out" 2> "$BH_DIR/$label.err" ) &
            eval "exec $fd>\"\$fifo\""
            BH_PID=""
            for i in $(seq 1 60); do
                cur="$(cat "$BH_STATE/bot.pid" 2>/dev/null || true)"
                if [ -n "$cur" ] && [ "$cur" != "$prev" ]; then BH_PID="$cur"; break; fi
                sleep 0.25
            done
            [ -n "$BH_PID" ] && BH_PIDS="$BH_PIDS $BH_PID"
            return 0
        }
        bh_until() { # <max_s> <cmd...> — poll at 0.25s ticks; yes on success
            local i n=$(( $1 * 4 ))
            shift
            for i in $(seq 1 "$n"); do
                "$@" 2>/dev/null && { echo yes; return; }
                sleep 0.25
            done
            echo no
        }
        bh_gone() { ! ps -p "$1" >/dev/null 2>&1; }

        spawn_poller A 8
        BH_A="$BH_PID"
        { [ -n "$BH_A" ] && ps -p "$BH_A" >/dev/null 2>&1; } && r=yes || r=no
        harness_check "victim poller A up and holding bot.pid (real plugin, fake token)" "$r"

        spawn_poller B 9 "$BH_A"
        BH_B="$BH_PID"
        [ "$(bh_until 6 grep -q "replacing stale poller pid=$BH_A" "$BH_DIR/B.err")" = "yes" ] && r=yes || r=no
        harness_check "newcomer B SIGTERMs the LIVE holder (last-writer-wins reap fired)" "$r"

        { [ "$(bh_until 5 bh_gone "$BH_A")" = "yes" ] && [ "$(bh_until 5 grep -q "shutting down" "$BH_DIR/A.err")" = "yes" ]; } && r=yes || r=no
        harness_check "victim A exits via the graceful shutdown path" "$r"

        { [ -n "$BH_B" ] && [ "$(cat "$BH_STATE/bot.pid" 2>/dev/null)" = "$BH_B" ]; } && r=yes || r=no
        harness_check "slot taken over: bot.pid now = newcomer B" "$r"

        exec 9>&-   # transient B loses its client: stdin EOF
        [ "$(bh_until 6 bh_gone "$BH_B")" = "yes" ] && r=yes || r=no
        harness_check "transient B exits when its client goes away (stdin EOF)" "$r"

        [ ! -f "$BH_STATE/bot.pid" ] && r=yes || r=no
        harness_check "B unlinks bot.pid on exit — slot ABANDONED" "$r"

        sleep 1   # settle window for the negative assertion below
        { [ ! -f "$BH_STATE/bot.pid" ] && bh_gone "${BH_A:-0}" && bh_gone "${BH_B:-0}"; } && r=yes || r=no
        harness_check "victim never returns: slot stays dark after settle (no respawn, no reclaim)" "$r"

        exec 8>&-   # release the dead victim's writer fd

        if [ "$fail" -gt "$_bh_fail_before" ]; then
            echo "  --- DIAGNOSTIC: bridge-hijack poller stderr ---"
            for _f in "$BH_DIR"/*.err; do
                [ -s "$_f" ] || continue
                echo "  [$(basename "$_f")]"
                sed 's/^/    /' "$_f" | tail -6
            done
        fi
    fi
fi

# ===========================================================================
# Nested-fleet supervision (#602 P2 slice 2). The recursive-containment vault
# may nest a fleet one level under a system container:
#     flat    local/<fleet>/...            (the live Pi today — must stay identical)
#     nested  local/<system>/<fleet>/...   (opt-in; the container has no fleet.yaml)
# This proves the bash supervision layer resolves + health-checks a NESTED bot
# end-to-end, WITHOUT disturbing the flat path (the byte-identical invariant).
# ===========================================================================
echo ""
echo "=== validate #602 P2: nested-fleet resolution + supervision ==="

NSYS="valsys"; NF="valnest"; NBOT="valnestbot"
NF_DIR="$ROOT/local/$NSYS/$NF"
NF_BOTS="$NF_DIR/runtime/bots"
mkdir -p "$NF_BOTS/$NBOT/data"
cat > "$NF_DIR/fleet.yaml" <<YAML
fleet:
  name: $NF
  bots:
    $NBOT:
      expertise: [software-engineering]
YAML
cat > "$NF_BOTS/$NBOT/bot.conf" <<CONF
BOT_NAME="$NBOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF

# Direct resolver assertions run in THIS shell (lib-common sourced at the top),
# so point CLAUDLOBBY_ROOT at the throwaway root for the duration, then restore.
_prev_root="${CLAUDLOBBY_ROOT:-}"
CLAUDLOBBY_ROOT="$ROOT"

[ "$(resolve_fleet_dir "$NF")" = "$NF_DIR" ] && r=yes || r=no
harness_check "#602 resolve_fleet_dir finds a fleet nested under a system container" "$r"

[ "$(resolve_bots_dir "$NF")" = "$NF_BOTS" ] && r=yes || r=no
harness_check "#602 resolve_bots_dir returns the nested runtime/bots" "$r"

host_bots_dirs | grep -qxF "$NF_BOTS" && r=yes || r=no
harness_check "#602 host_bots_dirs enumerates the nested bots dir" "$r"

# Byte-identical invariant: the FLAT valfleet must STILL resolve flat, unchanged.
[ "$(resolve_fleet_dir "$FLEET")" = "$ROOT/local/$FLEET" ] && r=yes || r=no
harness_check "#602 flat fleet STILL resolves flat (byte-identical invariant holds)" "$r"

CLAUDLOBBY_ROOT="$_prev_root"

# End-to-end: the REAL fleet-pulse supervision script must find + health-check
# the nested bot (no live session -> session_missing), proving resolve_bots_dir
# and the nested fleet.yaml discovery both fire through a production script.
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/fleet-pulse.sh" "$NF" >/dev/null 2>&1 || true
nbot_ev=$(ls "$NF_BOTS/$NBOT/data/events"/fleet-*.jsonl 2>/dev/null | head -1 || true)
[ -n "$nbot_ev" ] && grep -q '"type":"session_missing"' "$nbot_ev" && r=yes || r=no
harness_check "#602 fleet-pulse health-checks a bot in a NESTED fleet (session_missing fired)" "$r"

# ===========================================================================
# Scenario: equippable briefing trigger (#627 P3/P4/P6). The timer-fired
# /briefing dispatch must (a) fire a briefing_dispatched event, (b) land as a
# REAL bare slash command — NOT set +H; prose (the F6 regression canary) — and
# (c) defer on a busy bot; the shared dispatch.sh classifier must keep the
# set +H; guard on (d) prose, (e) file-path prose + a leading-whitespace slash;
# and (f) the /briefing skill's documented env-read must resolve the CONFIGURED
# sections, not the canonical default. This is BEHAVIOR a unit test cannot
# prove. The composed-timer plumbing (compose -> enroll -> journal fire ->
# reconcile prune, incl. the launchd .plist prune) is the sibling
# lib/rehearse-briefing-timer.sh (real systemd timer, ~2 min, run separately).
# ===========================================================================
echo ""
echo "=== validate-bot-change: equippable briefing trigger (#627 P6) ==="

BRIEF="valbrief"; BRIEFBUSY="valbriefbusy"; SINK="valsink"
BRIEF_DIR="$ROOT/local/$FLEET/runtime/bots/$BRIEF"
BRIEFBUSY_DIR="$ROOT/local/$FLEET/runtime/bots/$BRIEFBUSY"
# data/ holds the busy bot's .last-tool-call marker and is where the trigger
# emits events; briefing-trigger.sh + emit_fleet_event create logs/ + data/events
# on demand. SINK is a pure classifier sink (pane-only) — no dir needed; its
# socket resolves by the basename fallback.
mkdir -p "$BRIEF_DIR/data" "$BRIEFBUSY_DIR/data"

# Composed-SHAPE bot.conf: BOT_SERVICE empty so tmux_socket_for_bot resolves the
# harness fallback tmux-<name> (the socket the tmux() shim targets). BRIEFING_*
# mirror the composer emission (compose_bot_conf); BRIEFING_SECTIONS_MORNING is
# UPPER-CASED exactly as composer.py emits it — the var the /briefing skill reads
# by upper-casing the dispatched slot — with a NON-default section list so (f)
# can tell config-tracking from the canonical morning default. (Emission case is
# unit-tested in tests/test_briefing.py; this scenario proves the read-side.)
for _d in "$BRIEF_DIR" "$BRIEFBUSY_DIR"; do
    _n="$(basename "$_d")"
    cat > "$_d/bot.conf" <<CONF
BOT_NAME="$_n"
BOT_ID="$_n"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
BRIEFING_SLOTS="morning"
BRIEFING_SOURCES="github"
BRIEFING_SECTIONS_MORNING="wrap tomorrow overnight"
CONF
done

# Idle briefing bot: plain pane, no esc-to-interrupt, no fresh .last-tool-call
# -> bot_is_busy reads not-busy -> the trigger dispatches.
tmux new-session -d -s "$BRIEF" "sleep 600"
# Busy briefing bot: a fresh data/.last-tool-call -> bot_is_busy reads BUSY via
# the rendering-immune marker branch (no pane-render race) -> the trigger defers.
tmux new-session -d -s "$BRIEFBUSY" "sleep 600"
touch "$BRIEFBUSY_DIR/data/.last-tool-call"
# Classifier sink: an idle pane that receives direct dispatch.sh sends, so the
# computed PAYLOAD (bare vs set +H;) is observable verbatim in the captured pane.
tmux new-session -d -s "$SINK" "sleep 600"
sleep 1  # let panes render

# --- Observe: the real trigger against the idle + busy briefing bots ----------
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/briefing-trigger.sh" "$FLEET" "$BRIEF" morning >/dev/null 2>&1 || true
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/briefing-trigger.sh" "$FLEET" "$BRIEFBUSY" morning >/dev/null 2>&1 || true
# Prose + classifier-edge payloads straight through dispatch.sh: prose with a
# bang, file-path prose, and a leading-whitespace slash must ALL keep set +H;.
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/dispatch.sh" "$SINK" "deploy failed alert !!" >/dev/null 2>&1 || true
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/dispatch.sh" "$SINK" "/home/crog/x is broken" >/dev/null 2>&1 || true
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/dispatch.sh" "$SINK" " /leading-space-prose" >/dev/null 2>&1 || true
sleep 1  # let the sends render into the panes

# --- Assert ---
brief_events=$(ls "$BRIEF_DIR"/data/events/fleet-*.jsonl 2>/dev/null | head -1 || true)
briefbusy_events=$(ls "$BRIEFBUSY_DIR"/data/events/fleet-*.jsonl 2>/dev/null | head -1 || true)
brief_pane=$(tmux capture-pane -t "$BRIEF" -p 2>/dev/null || true)
busy_pane=$(tmux capture-pane -t "$BRIEFBUSY" -p 2>/dev/null || true)
sink_pane=$(tmux capture-pane -t "$SINK" -p 2>/dev/null || true)

# (a) the trigger fires — briefing_dispatched on the idle bot's ledger
{ [ -n "$brief_events" ] && grep -q '"type":"briefing_dispatched"' "$brief_events"; } && r=yes || r=no
harness_check "briefing trigger fires: briefing_dispatched event emitted (idle bot)" "$r"

# (b) bare-slash lands — /briefing morning present, NO set +H; prefix (F6 canary)
{ printf '%s' "$brief_pane" | grep -q '/briefing morning' \
    && ! printf '%s' "$brief_pane" | grep -q 'set +H'; } && r=yes || r=no
harness_check "briefing dispatch lands as a BARE slash command (no set +H; — F6 regression canary)" "$r"

# (c) busy-defer — briefing_deferred/bot_busy, and NOTHING sent to the busy pane
printf '%s' "$busy_pane" | grep -q '/briefing' && _sent=yes || _sent=no
{ [ -n "$briefbusy_events" ] \
    && grep -q '"type":"briefing_deferred".*"reason":"bot_busy"' "$briefbusy_events" \
    && [ "$_sent" = no ]; } && r=yes || r=no
harness_check "briefing defers on a busy bot: briefing_deferred/bot_busy, no dispatch" "$r"

# (d) prose control — a non-slash payload keeps the set +H; guard
printf '%s' "$sink_pane" | grep -qE 'set \+H; deploy failed alert' && r=yes || r=no
harness_check "dispatch classifier keeps set +H; on prose (deploy failed !!)" "$r"

# (e) classifier edges — file-path prose + leading-whitespace slash keep the guard
printf '%s' "$sink_pane" | grep -qE 'set \+H; /home/crog/x is broken' && r=yes || r=no
harness_check "dispatch classifier keeps set +H; on file-path prose (/home/... not a slash cmd)" "$r"
printf '%s' "$sink_pane" | grep -qE 'set \+H; +/leading-space-prose' && r=yes || r=no
harness_check "dispatch classifier keeps set +H; on a leading-whitespace slash (not anchored ^/)" "$r"

# (f) skill env-consumption (F4). The /briefing skill must actually CONSUME the
# configured sections — the F4 "silently-ignored section list" risk. SKILL.md is
# model-facing prose with no runnable artifact in this model-free harness, so
# prove the consumption CONTRACT lives in the REAL library skill file: its
# actionable "## Instructions" must READ BRIEFING_SECTIONS_<SLOT> and RENDER the
# configured sections (a mention in a reference table is not consumption). TEETH:
# gutting SKILL.md's BRIEFING_SECTIONS handling makes this go RED (mutation-
# verified). A live model honoring the instruction is the P7 human canary; this
# is the deterministic file-contract gate. NOTE: the prior version re-read a
# bot.conf var the test itself wrote and never touched SKILL.md, so a gutted
# skill stayed green — hollow (#640, rajan request-changes).
_skill="$LIB_DIR/../library/skills/briefing/SKILL.md"
_instr=$(awk '/^## Instructions/{f=1; next} /^## /{f=0} f' "$_skill" 2>/dev/null)
{ [ -f "$_skill" ] \
    && printf '%s\n' "$_instr" | grep -q 'BRIEFING_SECTIONS' \
    && printf '%s\n' "$_instr" | grep -qi 'configured section'; } && r=yes || r=no
harness_check "briefing SKILL.md Instructions consume BRIEFING_SECTIONS_<SLOT> (read the var + render the configured sections)" "$r"

echo ""
echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ]
