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
    tmux kill-session -t "$BOT" 2>/dev/null || true
    tmux kill-session -t "$MGR" 2>/dev/null || true
    tmux kill-session -t "$IBOT" 2>/dev/null || true
    tmux kill-session -t "$BUSY" 2>/dev/null || true
    tmux kill-session -t "$SBOT" 2>/dev/null || true
    rm -rf "$ROOT"
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

echo ""
echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ]
