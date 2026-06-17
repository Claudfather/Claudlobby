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
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-validate.XXXXXX")"
# fleet-pulse resolves bots via resolve_bots_dir <fleet> = local/<fleet>/runtime/bots.
BOT_DIR="$ROOT/local/$FLEET/runtime/bots/$BOT"
install_error_trap "$BOT_DIR"
EVENTS="$BOT_DIR/data/events"

cleanup() {
    # Per-bot servers must be torn down with kill-server, or empty servers leak.
    for _s in "$BOT" "$MGR" "$IBOT" "$BUSY" "$SBOT" "${RB_SESSION:-}" "${IDLEK:-}"; do
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
CONF

# --- Run: stand up a non-idle worker pane + a manager session to receive alerts ---
tmux new-session -d -s "$MGR" "sleep 600"
tmux new-session -d -s "$BOT" 'printf "\n⠹ Cogitating (esc to interrupt)\n"; sleep 600'
sleep 1  # let panes render

# Worker made a tool call a moment ago, then went silent (gap will exceed 1s).
touch "$BOT_DIR/data/.last-tool-call"

# A task was dispatched and is already past its deadline, with no report.
now=$(date +%s)
printf '{"ts":"2026-05-27T10:00:00Z","manager":"%s","bot":"%s","task":"do x","dispatched_at":%s,"expected_by":%s}\n' \
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

# Safety: a BUSY bot with a pending reload must NOT be interrupted (ghost-text
# risk). A dedicated busy fixture — an explicit spinner glyph then a long sleep —
# makes the BUSY classification deterministic regardless of test run order, rather
# than borrowing the fleet-pulse valbot, whose pane state is incidental here.
BUSY_DIR="$ROOT/local/$FLEET/runtime/bots/$BUSY"
mkdir -p "$BUSY_DIR/data"
cat > "$BUSY_DIR/bot.conf" <<CONF
BOT_NAME="$BUSY"
BOT_ID="$BUSY"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
tmux new-session -d -s "$BUSY" 'printf "⠋ Thinking...\n"; sleep 600'
sleep 1
touch "$BUSY_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/keepalive.sh" "$BUSY_DIR" >/dev/null 2>&1 || true
[ -f "$BUSY_DIR/data/.reload-pending" ] && r=yes || r=no
check "keepalive leaves .reload-pending on a BUSY bot (no interrupt)" "$r"

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
# A fresh session.md -> /claudna:session-resume is sent BEFORE STARTUP_PROMPT; a
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
printf '%s' "$pane_fresh" | grep -q '/claudna:session-resume' && r=yes || r=no
check "fresh session.md -> /claudna:session-resume injected on start" "$r"
_rln="$(printf '%s\n' "$pane_fresh" | grep -n '/claudna:session-resume' | head -1 | cut -d: -f1 || true)"
_sln="$(printf '%s\n' "$pane_fresh" | grep -n 'ZZZ_STARTUPMARK' | head -1 | cut -d: -f1 || true)"
{ [ -n "$_rln" ] && [ -n "$_sln" ] && [ "$_rln" -lt "$_sln" ]; } && r=yes || r=no
check "resume keystroke precedes STARTUP_PROMPT in the pane" "$r"
pane_stale="$(_run_startbot stale)"
printf '%s' "$pane_stale" | grep -q '/claudna:session-resume' && r=no || r=yes
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
