#!/bin/bash
# interactive-claude-spike.sh — #729 stage-B mechanic proof.
#
# Boots ONE real INTERACTIVE claude in a throwaway root, dispatches a task via
# lib/dispatch.sh, awaits the report-back.jsonl ledger row (the completion
# signal), then recovers the session transcript and attributes tokens with
# lib/transcript-usage.py. This is the reusable primitive the A/B comms-eval
# harness (stage C) and freshbox both need — the one thing no existing script
# does: interactive + real binary + fresh isolated config.
#
# It grafts three mechanics that live in separate scripts today:
#   - freshbox-boot-gate.sh : throwaway-root generate + seed_auth_and_trust  (real binary, but headless claude -p)
#   - start-bot.sh          : interactive --remote-control tmux launch        (but assumes a real fleet + auth)
#   - dispatch.sh / report-back.sh : socket-aware send + the ledger row       (the completion signal)
#
# Opt-in (burns real quota; never in CI): SPIKE_REALBOOT=1 bash lib/interactive-claude-spike.sh
# Keep the throwaway root for inspection: SPIKE_KEEP=1 (default 1).
#
# NOT `set -e`: every stage runs so we can print a PASS/FAIL matrix even when a
# stage fails (the point of a spike is the diagnosis, not an early abort).
set -uo pipefail

if [ "${SPIKE_REALBOOT:-0}" != 1 ]; then
  printf 'SKIP: real-boot spike is opt-in — set SPIKE_REALBOOT=1 to run (burns real quota)\n'
  exit 0
fi

SRC="${SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LIB="$SRC/lib"
ROOT=/tmp/spike729-root              # short path so the tmux socket sun_path stays <108 bytes
CONFIG_DIR="$ROOT/spikeconfig"       # the fresh, isolated CLAUDE_CONFIG_DIR (interactive transcripts land under here)
SPIKE_HOME="$ROOT/home"              # private HOME so the sandbox never sources the real fleet .env
BOT=spikebot
BOT_DIR="$ROOT/runtime/bots/$BOT"
SESSION="$BOT"                       # tmux_session_name == basename(BOT_DIR)
SOCKET="spk729.$BOT"                 # composed BOT_SERVICE — unique name isolates the tmux server from real bots
HOST_CREDS="$HOME/.claude/.credentials.json"   # captured against the REAL home before any override
STAMP="$(date +%s)-$$"
TASK_ID="spike729-$STAMP"

# A channel-less sandbox never prints "remote-control is active", so start-bot's
# readiness poll would burn its FULL ceiling on every boot. Keep it tiny — real
# readiness is proven downstream by the transcript appearing + the ledger row.
RC_READY_TIMEOUT_S="${RC_READY_TIMEOUT_S:-10}"
BOOT_WAIT_S="${BOOT_WAIT_S:-120}"    # wait for claude to create its session transcript
IDLE_WAIT_S="${IDLE_WAIT_S:-150}"
LEDGER_WAIT_S="${LEDGER_WAIT_S:-360}"

PASS=(); FAIL=()
ok()  { PASS+=("$1"); printf '  PASS  %s\n' "$1"; }
bad() { FAIL+=("$1"); printf '  FAIL  %s\n' "$1"; }
hdr() { printf '\n=== %s ===\n' "$1"; }
pane() { command tmux -L "$SOCKET" capture-pane -t "$SESSION" -p 2>/dev/null || true; }
find_transcript() { find "$CONFIG_DIR/projects" -name '*.jsonl' 2>/dev/null | head -1; }

cleanup() {
  command tmux -L "$SOCKET" kill-server 2>/dev/null || true
  [ "${SPIKE_KEEP:-1}" != 1 ] && rm -rf "$ROOT"
  return 0
}
trap cleanup EXIT

# --- 0. preconditions --------------------------------------------------------
hdr "0. preconditions"
for dep in claude jq python3; do
  command -v "$dep" >/dev/null 2>&1 || { printf 'ABORT: missing dep %s\n' "$dep"; exit 2; }
done
[ -f "$HOST_CREDS" ] || { printf 'ABORT: no host auth at %s\n' "$HOST_CREDS"; exit 2; }
CLAUDE_VER="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
printf 'claude %s ; host creds present ; SRC=%s\n' "$CLAUDE_VER" "$SRC"

# --- 1. scaffold throwaway root + generate (freshbox recipe) ------------------
hdr "1. scaffold + generate"
rm -rf "$ROOT"; mkdir -p "$ROOT" "$CONFIG_DIR" "$SPIKE_HOME/.claude"
ln -s "$SRC/library" "$ROOT/library"
ln -s "$SRC/lib"     "$ROOT/lib"
ln -s "$SRC/templates" "$ROOT/templates"
ln -s "$SRC/voices"  "$ROOT/voices" 2>/dev/null || true
cat > "$ROOT/fleet.yaml" <<YAML
fleet:
  name: spike729
  service_prefix: spk729
  accounts:
    default: ~/.claude
    spike: $CONFIG_DIR
  plugins:
    include_defaults: false
  bots:
    $BOT:
      name: $BOT
      account: spike
      expertise:
        - code-review
      dangerously_skip_permissions: true
      channels: []
      telegram:
        handle: spike729_bot
YAML
if CLAUDLOBBY_ROOT="$ROOT" PYTHONPATH="$SRC" python3 -m claudlobby generate >"$ROOT/generate.out" 2>&1; then
  [ -f "$BOT_DIR/bot.conf" ] && ok "generate composed the bot dir" || bad "generate ran but no bot.conf"
else
  bad "generate failed"; cat "$ROOT/generate.out"; exit 1
fi

# --- 2. seed auth + trust into CONFIG_DIR BEFORE first contact (freshbox) -----
hdr "2. seed auth + trust"
cp "$HOST_CREDS" "$CONFIG_DIR/.credentials.json" && chmod 600 "$CONFIG_DIR/.credentials.json"
jq -n --arg cwd "$BOT_DIR" --arg ver "${CLAUDE_VER:-0.0.0}" '{
  hasCompletedOnboarding: true,
  lastOnboardingVersion: $ver,
  projects: { ($cwd): { hasTrustDialogAccepted: true, hasCompletedProjectOnboarding: true } }
}' > "$CONFIG_DIR/.claude.json"
[ -s "$CONFIG_DIR/.credentials.json" ] && [ -s "$CONFIG_DIR/.claude.json" ] \
  && ok "auth + trust seeded (cwd=$BOT_DIR)" || bad "seeding produced empty files"

# --- 3. boot real interactive claude via start-bot.sh ------------------------
hdr "3. boot real interactive claude"
# env -u scrubs the PARENT fleet vars that would otherwise leak into the sandbox
# start-bot (it inherits the caller session env; bot.conf has no FLEET_PLUGINS,
# but an unscrubbed parent does — that leak clones marketplaces + installs the
# parent bot plugins into the sandbox, ~30s + network + a failed SSH install).
BOOT_T0=$(date +%s)
env -u FLEET_PLUGINS_REQUIRED -u FLEET_PLUGINS_MARKETPLACES -u TELEGRAM_BOT_TOKEN \
    -u BOT_DIR -u BOT_ID -u BOT_NAME -u BOT_SERVICE -u TMUX_SOCKET \
    HOME="$SPIKE_HOME" CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME=spike729 \
    RC_READY_TIMEOUT_S="$RC_READY_TIMEOUT_S" BOOT_LOCK_HOLD_S=0 \
    bash "$LIB/start-bot.sh" "$BOT_DIR" >"$ROOT/startbot.out" 2>&1
command tmux -L "$SOCKET" has-session -t "$SESSION" 2>/dev/null \
  && ok "tmux session '$SESSION' alive on socket '$SOCKET'" \
  || { bad "no tmux session after start-bot"; cat "$ROOT/startbot.out"; }
# Readiness = claude created its session transcript (bridge-independent, unlike
# the "remote-control is active" sentinel which never fires without a channel).
_t=0; TR=""
while [ "$_t" -lt "$BOOT_WAIT_S" ]; do
  TR="$(find_transcript)"
  [ -n "$TR" ] && break
  sleep 2; _t=$((_t+2))
done
BOOT_S=$(( $(date +%s) - BOOT_T0 ))
if [ -n "$TR" ]; then ok "claude session live — transcript created in ${BOOT_S}s"; else bad "no transcript after ${BOOT_WAIT_S}s"; fi
# trust proof: an untrusted workspace prints an advisory + drops composed settings
pane | grep -qiE 'has not been trusted|do you trust this folder|Ignoring [0-9]+ permissions' \
  && bad "untrusted-workspace advisory present — trust seed did NOT take" \
  || ok "no untrusted/trust-wizard advisory (trust seed took)"
# observation only (not a gate): did the bridge-dependent readiness sentinel fire?
if grep -q 'READY — remote-control active' "$BOT_DIR/logs/startup.log" 2>/dev/null; then
  printf '  note: "remote-control is active" sentinel fired\n'
else
  printf '  note: "remote-control is active" sentinel NEVER fired (channel-less) — start-bot hit its readiness ceiling; harness must not gate on it\n'
fi

# --- 4. wait for the startup-prompt turn to settle (idle) --------------------
hdr "4. wait for idle before dispatch"
_idle=0; _t=0
while [ "$_t" -lt "$IDLE_WAIT_S" ]; do
  if ! pane | grep -qiE 'esc to interrupt|Running|Thinking'; then
    _idle=$((_idle+1)); [ "$_idle" -ge 2 ] && break
  else _idle=0; fi
  sleep 3; _t=$((_t+3))
done
[ "$_idle" -ge 2 ] && ok "session idle after ${_t}s" || bad "session still busy after ${IDLE_WAIT_S}s"

# --- 5. dispatch a trivial task via lib/dispatch.sh --------------------------
hdr "5. dispatch via lib/dispatch.sh"
TASK="Run this one bash command verbatim and then stop, reporting nothing else: bash $LIB/report-back.sh $BOT completed spike-ok-$STAMP --task $TASK_ID"
if BOT_DIR="$BOT_DIR" CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME=spike729 HOME="$SPIKE_HOME" \
     bash "$LIB/dispatch.sh" "$SESSION" "$TASK" >"$ROOT/dispatch.out" 2>&1; then
  ok "dispatch.sh sent the task (socket resolved, session found)"
else
  bad "dispatch.sh failed"; cat "$ROOT/dispatch.out"
fi

# --- 6. await the report-back.jsonl ledger row (the completion signal) -------
hdr "6. await ledger completion row"
_t=0; _got=0; LEDGER=""
while [ "$_t" -lt "$LEDGER_WAIT_S" ]; do
  LEDGER="$(find "$ROOT" -name report-back.jsonl 2>/dev/null | head -1)"
  if [ -n "$LEDGER" ] && grep -q "\"task_id\":\"$TASK_ID\"" "$LEDGER" 2>/dev/null; then _got=1; break; fi
  sleep 4; _t=$((_t+4))
done
if [ "$_got" = 1 ]; then
  ok "ledger row for task_id=$TASK_ID after ${_t}s -> $LEDGER"
  grep "\"task_id\":\"$TASK_ID\"" "$LEDGER" | tail -1
else
  bad "no ledger row for task_id=$TASK_ID within ${LEDGER_WAIT_S}s"
  printf -- '--- last pane ---\n'; pane | tail -25
fi

# --- 7. recover transcript + attribute tokens -------------------------------
hdr "7. recover transcript + attribute"
TR="$(find_transcript)"
if [ -n "$TR" ]; then
  ok "transcript recovered: $TR"
  printf -- '--- transcript-usage.py --comms-share (mechanic proof) ---\n'
  USAGE_OUT="$(python3 "$LIB/transcript-usage.py" --comms-share "$CONFIG_DIR/projects" 2>&1)"
  printf '%s\n' "$USAGE_OUT"
  # Expose the per-line vs dedup-by-message.id discrepancy (stage-C blocker):
  # interactive Claude Code writes one assistant message as N content-block
  # lines, EACH repeating the flat message.usage. transcript-usage.py sums
  # per-line and over-counts by that multiplicity.
  printf -- '--- raw-per-line vs dedup-by-message.id (the parser blocker) ---\n'
  python3 - "$TR" <<'PY'
import json,sys,collections
naive=collections.Counter(); dedup=collections.Counter(); seen=set(); lines=asst=0
for l in open(sys.argv[1],encoding="utf-8",errors="replace"):
    if '"assistant"' not in l: continue
    try: o=json.loads(l)
    except: continue
    if o.get("type")!="assistant": continue
    asst+=1
    m=o.get("message") or {}; u=m.get("usage") or {}; mid=m.get("id")
    for k in ("input_tokens","output_tokens","cache_creation_input_tokens","cache_read_input_tokens"):
        naive[k]+=u.get(k) or 0
        if mid not in seen: dedup[k]+=u.get(k) or 0
    seen.add(mid)
print("  assistant lines=%d  unique message ids=%d" % (asst,len(seen)))
for k in ("input_tokens","output_tokens","cache_creation_input_tokens","cache_read_input_tokens"):
    inf = naive[k]/dedup[k] if dedup[k] else 0
    print("  %-28s naive=%-8d dedup=%-8d inflation=%.2fx" % (k,naive[k],dedup[k],inf))
PY
  # mechanic PASS: a transcript was recovered and the parser attributed >0 turns.
  TURNS="$(printf '%s' "$USAGE_OUT" | grep -oE 'turns=[0-9]+' | head -1 | cut -d= -f2)"
  [ "${TURNS:-0}" -gt 0 ] 2>/dev/null && ok "transcript-usage.py attributed the session (turns=$TURNS)" || bad "attribution returned zero turns"
else
  bad "no transcript found under $CONFIG_DIR/projects"
fi

# --- 8. result matrix --------------------------------------------------------
hdr "RESULT"
printf 'boot_s=%s  PASS=%s  FAIL=%s\n' "${BOOT_S:-?}" "${#PASS[@]}" "${#FAIL[@]}"
if [ "${#FAIL[@]}" -gt 0 ]; then printf 'failures:\n'; printf '  - %s\n' "${FAIL[@]}"; exit 1; fi
printf 'ALL STAGES PASSED — boot -> dispatch -> ledger -> recover -> attribute proven end-to-end.\n'
