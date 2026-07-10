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
# fleet-pulse resolves bots via resolve_bots_dir <fleet> = local/<fleet>/runtime/bots.
BOT_DIR="$ROOT/local/$FLEET/runtime/bots/$BOT"
install_error_trap "$BOT_DIR"
EVENTS="$BOT_DIR/data/events"

cleanup() {
    # Per-bot servers must be torn down with kill-server, or empty servers leak.
    for _s in "$BOT" "$MGR" "$IBOT" "$BUSY" "$SBOT" "$MBOT" "${HBOT:-}" "${RB_SESSION:-}" "${IDLEK:-}"; do
        [ -n "$_s" ] && command tmux -L "$(vsock "$_s")" kill-server 2>/dev/null || true
    done
    rm -rf "$ROOT" "${RB_ROOT:-}" "${WR_ROOT:-}"
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

check() {
    local desc="$1" cond="$2"
    if [ "$cond" = "yes" ]; then pass=$((pass+1)); printf "  PASS  %s\n" "$desc"
    else fail=$((fail+1)); printf "  FAIL  %s\n" "$desc"; fi
}

echo "=== validate-bot-change: observe the trust-loop behaviors ==="
[ -n "$events_file" ] && grep -q '"type":"activity_stuck"' "$events_file" && r=yes || r=no
check "activity_stuck event emitted (animated-but-hung worker)" "$r"
[ -n "$events_file" ] && grep -q '"type":"overdue_dispatch"' "$events_file" && r=yes || r=no
check "overdue_dispatch event emitted (deadline passed, no report)" "$r"
printf '%s' "$mgr_pane" | grep -q '\[FLEET-PULSE\]' && r=yes || r=no
check "manager notified via [FLEET-PULSE] push" "$r"
[ -n "$events_file" ] && grep -q '"type":"bridge_down"' "$events_file" && r=yes || r=no
check "bridge_down event emitted (live session, Telegram poller not delivering)" "$r"

# #460: a never-closing dispatch must age out of the overdue set so fleet-pulse
# stops re-emitting overdue_dispatch every cycle. Drive the real matcher (the CLI
# fleet-pulse consumes) with a 25h-old, never-reported dispatch and assert nothing.
aged_log="$ROOT/state/dispatch-log-aged.jsonl"
printf '{"ts":"t","manager":"%s","bot":"%s","task":"x","dispatched_at":%s,"expected_by":%s}\n' \
    "$MGR" "$BOT" "$((now - 90000))" "$((now - 89400))" > "$aged_log"
aged_out=$(python3 "$LIB_DIR/dispatch-overdue.py" --all "$aged_log" "$ROOT/state/report-back.jsonl" "$now" 2>/dev/null || true)
[ -z "$aged_out" ] && r=yes || r=no
check "overdue_dispatch expires past max age (#460 — no re-emit for a 25h-old dispatch)" "$r"

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
check "overdue_dispatch event carries the dispatch task_id" "$r"
printf '%s' "$mgr_pane" | grep -q 'report-back --task' && printf '%s' "$mgr_pane" | grep -q 't-1-aaaa' && r=yes || r=no
check "manager nudge names the open id (self-heal echo instruction)" "$r"

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
check "reload-fleet marks a running bot with .reload-pending (happy path)" "$r"

# Loud-fail: a failing 'claude plugin update' must be LOUD, never silent.
printf '#!/bin/bash\necho boom >&2; exit 1\n' > "$STUB_BIN/claude"
chmod +x "$STUB_BIN/claude"
rm -f "$BOT_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" PATH="$STUB_BIN:$PATH" "$LIB_DIR/reload-fleet.sh" "$FLEET" >/dev/null 2>&1 || true
fleet_events=$(ls "$ROOT/state/events"/fleet-*.jsonl 2>/dev/null | head -1 || true)
[ -n "$fleet_events" ] && grep -q '"type":"reload_failed"' "$fleet_events" && r=yes || r=no
check "reload-fleet emits reload_failed event on failure (loud, not silent)" "$r"
mgr_pane=$(tmux capture-pane -t "$MGR" -p 2>/dev/null || true)
printf '%s' "$mgr_pane" | grep -q 'reload_failed' && r=yes || r=no
check "reload-fleet alerts the manager on failure (shared emit_failure_alert)" "$r"
[ ! -f "$BOT_DIR/data/.reload-pending" ] && r=yes || r=no
check "reload-fleet does not half-reload (no marker when download fails)" "$r"

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
tmux new-session -d -s "$IBOT" 'printf "\n> \n"; sleep 600'
sleep 1
touch "$IBOT_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/keepalive.sh" "$IBOT_DIR" >/dev/null 2>&1 || true
sleep 1
ibot_pane=$(tmux capture-pane -t "$IBOT" -p 2>/dev/null || true)
printf '%s' "$ibot_pane" | grep -q '/reload-plugins' && r=yes || r=no
check "keepalive sends /reload-plugins to an idle bot with .reload-pending" "$r"
printf '%s' "$ibot_pane" | grep -q '/reload-skills' && r=yes || r=no
check "keepalive sends /reload-skills to an idle bot with .reload-pending" "$r"
[ ! -f "$IBOT_DIR/data/.reload-pending" ] && r=yes || r=no
check "keepalive clears .reload-pending after firing the reload" "$r"

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
check "keepalive long-think: esc-to-interrupt pane (no recent marker) → BUSY, NOT reloaded (#7)" "$r"

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
check "keepalive marker path: idle-looking pane + fresh .last-tool-call → BUSY, NOT reloaded (#7)" "$r"
[ ! -f "$MBOT_DIR/data/.idle" ] && r=yes || r=no
check "keepalive marker path: .idle marker not set (fleet-pulse stays consistent)" "$r"

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
check "heal bounces a dark poller on an idle bot (restart ladder invoked)" "$r"
hev=$(ls "$HDIR/data/events"/keepalive-*.jsonl 2>/dev/null | head -1 || true)
[ -n "$hev" ] && grep -q '"state":"BRIDGE_HEAL"' "$hev" && r=yes || r=no
check "heal emits a BRIDGE_HEAL keepalive event" "$r"
run_heal   # tick 2 → bounce #2 (reaches the cap of 2)
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "2" ] && r=yes || r=no
check "heal retries up to the attempt cap (2nd bounce)" "$r"
run_heal   # tick 3 → budget exhausted → escalate-only, NO 3rd bounce
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "2" ] && r=yes || r=no
check "heal stops bouncing at the cap (no 3rd bounce — F3 escalate-only)" "$r"
[ -f "$HDIR/data/.bridge-heal-escalated" ] && r=yes || r=no
check "heal escalates once when the budget is exhausted" "$r"
hev=$(ls "$HDIR/data/events"/keepalive-*.jsonl 2>/dev/null | head -1 || true)
[ -n "$hev" ] && grep -q 'budget exhausted' "$hev" && r=yes || r=no
check "heal logs budget-exhausted escalation" "$r"

# --- Phase B: flag OFF, same dark poller → never bounces (the F6b gate) ---
_heal_conf 0 y
_heal_reset
run_heal
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "0" ] && r=yes || r=no
check "heal is a no-op when OBSERVABILITY_BRIDGE_HEAL!=1 (F6b gate holds)" "$r"

# --- Phase C: flag ON but token unresolvable → no_token, never bounce (F2/5e) ---
_heal_conf 1 n
_heal_reset
run_heal
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "0" ] && r=yes || r=no
check "heal never bounces on no_token (a bounce cannot conjure a missing token)" "$r"

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
check "keepalive dead-session path emits a RESTART … session dead log line" "$r"
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
check "uptime parser extracts a RESTART from the real emitted keepalive.log (#579)" "$r"

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
cat > "$SBOT_DIR/fixture.sh" <<FIX
#!/bin/bash
printf '> \n'
while IFS= read -r l; do
    printf '%s\n' "\$l" >> "$SUBMIT_LOG"
    printf 'sent[%s]\n\n\n\n\n\n\n> \n' "\$l"
done
FIX
tmux new-session -d -s "$SBOT" "bash '$SBOT_DIR/fixture.sh'"
sleep 1
touch "$SBOT_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/keepalive.sh" "$SBOT_DIR" >/dev/null 2>&1 || true
sleep 1
submits=$(wc -l < "$SUBMIT_LOG")
[ "$submits" -eq 2 ] && r=yes || r=no
check "send_reload_command fires no spurious Enter on clean submit (verify scoped to prompt)" "$r"

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
printf 'remote-control is active\n'
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
check "fresh session.md -> /claudna:session resume injected on start" "$r"
_rln="$(printf '%s\n' "$pane_fresh" | grep -n '/claudna:session resume' | head -1 | cut -d: -f1 || true)"
_sln="$(printf '%s\n' "$pane_fresh" | grep -n 'ZZZ_STARTUPMARK' | head -1 | cut -d: -f1 || true)"
{ [ -n "$_rln" ] && [ -n "$_sln" ] && [ "$_rln" -lt "$_sln" ]; } && r=yes || r=no
check "resume keystroke precedes STARTUP_PROMPT in the pane" "$r"
pane_stale="$(_run_startbot stale)"
printf '%s' "$pane_stale" | grep -q '/claudna:session resume' && r=no || r=yes
check "stale session.md -> resume injection skipped (clean start)" "$r"
grep -q 'RESUME SKIP' "$RB_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
check "stale skip recorded in startup.log (RESUME SKIP)" "$r"

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

# === Scenario 2b: RC readiness probe — READY vs TIMEOUT + rc_timeout event (#533) ===
# Same REAL start-bot.sh. Positive path first (the fresh/stale runs above used a
# stub that prints the readiness string): READY must be logged and NO rc_timeout
# event emitted. Then the negative path: a stub that never prints the string
# simulates the #478 regression (RC never came up) — the probe must TIMEOUT and
# emit an rc_timeout event to the ledger fleet-pulse escalates. This is the
# empirical proof of #533 items 3-4 (unit tests prove composition; only running
# start-bot proves the event actually fires). RC_READY_TIMEOUT_S=1 keeps the
# TIMEOUT fast — the 90s default is untestable in a harness.
echo ""
echo "=== validate-bot-change: RC readiness alerting (#533 items 3-4) ==="
_rc_fail_before=$fail
grep -q 'READY — remote-control active' "$RB_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
check "RC string present -> READY recorded in startup.log" "$r"
grep -rq '"type":"rc_timeout"' "$RB_DIR/data/events/" 2>/dev/null && r=no || r=yes
check "RC present -> no rc_timeout event (no false alarm)" "$r"

cat > "$RB_ROOT/bin/claude" <<'STUB'
#!/bin/bash
exec cat
STUB
chmod +x "$RB_ROOT/bin/claude"
rm -rf "$RB_DIR/data/events" 2>/dev/null || true
tmux kill-session -t "$RB_SESSION" 2>/dev/null || true
sleep 0.3
printf -- '---\ncwd: %s\nlast_updated: %s\nschema_version: 2\n---\n' "$RB_DIR" "2020-01-01T00:00:00Z" \
    > "$RB_DIR/.claude/session.md"
TMPDIR="$RB_ROOT/tmp" BOOT_LOCK_HOLD_S=0 RC_READY_TIMEOUT_S=1 CLAUDE_BIN="$RB_ROOT/bin/claude" \
    HOME="$RB_HOME" PATH="$RB_ROOT/bin:$PATH" CLAUDLOBBY_ROOT="$RB_ROOT" \
    "$LIB_DIR/start-bot.sh" "$RB_DIR" >"$RB_ROOT/startbot.timeout.out" 2>&1 || true
sleep 1
grep -q 'TIMEOUT' "$RB_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
check "no RC string -> TIMEOUT recorded in startup.log" "$r"
_rcev="$(grep -rl '"type":"rc_timeout"' "$RB_DIR/data/events/" 2>/dev/null | head -1 || true)"
[ -n "$_rcev" ] && r=yes || r=no
check "TIMEOUT emits an rc_timeout fleet event (fleet-pulse escalation input)" "$r"
if [ -n "$_rcev" ]; then
    python3 -c "import sys,json; e=json.loads(open('$_rcev').readline()); sys.exit(0 if e['type']=='rc_timeout' and e['ts'] else 1)" 2>/dev/null && r=yes || r=no
else r=no; fi
check "rc_timeout event is valid JSON with ts+type (fleet-pulse-readable)" "$r"

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
check "rc_timeout burst on >= threshold bots FIRES the escalation page" "$r"

# Negative: only 1 bot with rc_timeout -> below threshold -> no rc_timeout page.
rm -rf "$_esc_bots"
mkdir -p "$_esc_bots"
: > "$_esc_pages"
esc_seed escone yes
esc_seed esctwo no
esc_run
grep -q 'rc_timeout' "$_esc_pages" && r=no || r=yes
check "a single rc_timeout (below threshold) does NOT page (no false alarm)" "$r"

if [ "$fail" -gt "$_esc_fail_before" ]; then
    echo "  --- DIAGNOSTIC: escalation pages recorded ---"
    sed 's/^/    /' "$_esc_pages" 2>/dev/null || echo "    (none)"
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
check "weekly restart SKIPS the manager (MANAGER_TMUX==BOT_ID)" "$r"
grep -q 'worker: wworker' "$wr_log" 2>/dev/null && r=yes || r=no
check "weekly restart PROCESSES the worker" "$r"
grep -q 'worker: wmgr' "$wr_log" 2>/dev/null && r=no || r=yes
check "manager never entered the worker restart path" "$r"
{ [ -n "$wr_events" ] && grep -q '"type":"restart_failed"' "$wr_events"; } && r=yes || r=no
check "worker restart failure raises a restart_failed alert (shared emit_failure_alert)" "$r"

# === Scenario 4: daily bounce retired from update-claude-code.sh (static) ===
echo ""
echo "=== validate-bot-change: daily bounce retired (download-only) ==="
grep -Eq 'BOUNCE|spin-up-bot\.sh' "$LIB_DIR/update-claude-code.sh" && r=no || r=yes
check "update-claude-code.sh no longer bounces the fleet" "$r"
grep -q 'npm install -g @anthropic-ai/claude-code@latest' "$LIB_DIR/update-claude-code.sh" && r=yes || r=no
check "update-claude-code.sh still downloads the binary daily" "$r"

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
check "#415 declared bot is still health-checked (session_missing fired for $KEEP)" "$r"

if [ -z "$orph_ev" ]; then r=yes
elif grep -qE '"type":"(session_missing|service_down|pane_stuck)"' "$orph_ev"; then r=no
else r=yes; fi
check "#415 undeclared orphan dir emits ZERO pulse events (filtered via fleet.yaml)" "$r"

[ -n "$idlek_ev" ] && grep -q '"type":"pane_stuck"' "$idlek_ev" && r=no || r=yes
check "#415 pane_stuck suppressed for an idle-at-prompt bot (.idle guard)" "$r"

# ===========================================================================
# Per-bot socket isolation (#414) — blast radius = 1 + observable misses.
# valmgr + valbot are up on DISTINCT private servers (tmux-valmgr/tmux-valbot),
# so a single server's death can no longer drop the whole fleet at once.
# ===========================================================================
echo ""
echo "=== validate #414: per-bot socket isolation (blast radius + send-miss) ==="

command tmux -L "$(vsock "$MGR")" has-session -t "$MGR" 2>/dev/null && r=yes || r=no
check "#414 precondition: manager is up on its own private server" "$r"
command tmux -L "$(vsock "$BOT")" has-session -t "$BOT" 2>/dev/null && r=yes || r=no
check "#414 precondition: worker is up on a DISTINCT private server" "$r"

# Crown jewel: kill ONE bot's whole server; only that bot dies.
command tmux -L "$(vsock "$BOT")" kill-server 2>/dev/null || true
sleep 0.3
command tmux -L "$(vsock "$BOT")" has-session -t "$BOT" 2>/dev/null && r=no || r=yes
check "#414 blast radius: the killed bot's server is gone" "$r"
command tmux -L "$(vsock "$MGR")" has-session -t "$MGR" 2>/dev/null && r=yes || r=no
check "#414 blast radius = 1: a peer SURVIVES the kill (shared-server SPOF removed)" "$r"

# Observable miss: a cross-socket send to a dead target lands a send_miss event
# in the caller's ledger — the silent `|| true` is gone.
BOT_DIR="$BOT_DIR" BOT_ID="$BOT" bash -c \
    '. "'"$LIB_DIR"'/lib-common.sh"; bot_tmux_send "tmux-valgone" "valgone" "ping"' \
    >/dev/null 2>&1 || true
{ ls "$EVENTS"/fleet-*.jsonl >/dev/null 2>&1 && grep -q '"type":"send_miss"' "$EVENTS"/fleet-*.jsonl; } && r=yes || r=no
check "#414 send-miss: a cross-socket send to a dead target is logged, not silently dropped" "$r"

echo ""
echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ]
