#!/usr/bin/env bash
# rehearse-permissions-ladder.sh — #970 single-factor permissions ladder.
#
# Two days of #970 measurement produced three models and three retractions
# because every cell varied several fields at once. This is the controlled
# version: ONE flip per cell, against a pinned baseline, on a disposable bot.
#
# Plan (the spec, not this header):
#   local/home/ai-platform/shared/planning/active/2026-08-26-permissions-single-factor-ladder.md
#
# THE FOUR NON-NEGOTIABLES, each wired as a check rather than an instruction:
#   1. C1 is the positive control and it runs FIRST. B0 == C1 ==> harness broken,
#      stop; nothing downstream counts.
#   2. HOME redirection is ASSERTED, not assumed — strace proves the operator's
#      real ~/.claude/settings.json is never opened. A harness that silently
#      fell back to the real global would PASS BY COINCIDENCE, which is the
#      exact failure that produced this thread.
#   3. Error strings are captured VERBATIM with the tool name. "Permission to
#      use Bash with command ..." and "File is in a directory that is denied ..."
#      are different code paths; a paraphrase destroys the discriminator, and
#      did. The untouched string is kept per cell in <cell>.results as JSONL.
#   4. Preconditions are recorded from a PRIOR step before every cell.
#
# SAFETY, structural rather than careful:
#   * everything lives in a disposable `git archive` export;
#   * HOME is redirected, so the operator real ~/.claude is never read OR
#     written — asserted both ways (strace, and an mtime check at the end);
#   * channels: [] and mcp: [] on the canary. Omitting the keys is NOT opting
#     out: the composer defaults a bot to --channels, and a canary that starts
#     a channel it cannot authenticate poisons a HOST-GLOBAL MCP auth cache
#     every bot reads. That took five bots across three fleets Telegram-dark
#     for four restarts on 2026-08-25. The composed bot.conf is GREPPED for
#     --channels before any boot; the declaration is not trusted.
#   * no production bot, no credential target, read-only probes.
#
# BOUNDARY: cells are headless `claude -p` runs, not interactive tmux boots.
# The permission decision is a CLI-layer one, but that is a claim about the
# layer, not a measurement of a tmux session — stated, not assumed.
#
# Exit: 0 ladder ran - 1 harness integrity failed - 2 precondition/dep missing.

set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(dirname "$LIB_DIR")"
# shellcheck source=/dev/null
. "$LIB_DIR/lib-common.sh"
# lib-common sets -e (and -u) at source time and installs its own EXIT trap.
# Re-arming -e here would abort the sweep on the first non-zero cell, which is
# exactly the state this harness must survive and report.
set +e
set -uo pipefail

CLAUDE_BIN="${CLAUDE_BIN:-claude}"
REAL_HOME="${REAL_HOME_OVERRIDE:-$HOME}"
REAL_GLOBAL="$REAL_HOME/.claude/settings.json"
HOST_CREDS="$REAL_HOME/.claude/.credentials.json"
CELL_TIMEOUT="${LADDER_CELL_TIMEOUT:-180}"
MODEL="${LADDER_MODEL:-claude-haiku-4-5-20251001}"
SENTINEL="LADDER_TARGET_A91F3C"
FLEET=permladder
BOT=canary
PREFIX=com.permladder.rehearsal

for dep in "$CLAUDE_BIN" jq python3 strace; do
  command -v "$dep" >/dev/null 2>&1 || { printf 'SKIP: %s not found\n' "$dep"; exit 2; }
done
[ -f "$HOST_CREDS" ] || { printf 'SKIP: no host auth to seed at %s\n' "$HOST_CREDS"; exit 2; }
[ -n "$_TIMEOUT_BIN" ] || { printf 'SKIP: no timeout(1)/gtimeout\n'; exit 2; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/permladder.XXXXXX")"
EXPORT_ROOT="$WORK/root"
FAKE_HOME="$WORK/home"
FAKE_CFG="$FAKE_HOME/.claude"          # location 1 == the user tier, as in production
BOT_DIR="$EXPORT_ROOT/local/$FLEET/runtime/bots/$BOT"
# INSIDE the bot cwd, and that placement is forced by measurement rather than
# chosen. The first real run put the target outside it and C1 -- the positive
# control, with NO deny composed at all -- came back:
#   "cat in '<path>' was blocked. For security, Claude Code may only
#    concatenate files from the allowed working directories for this session"
# That is a THIRD code path, distinct from both strings the plan named, and it
# is a WORKING-DIRECTORY rule, not a permission rule. It dominates the allow
# arm: with the target out of tree, C1 and B0 both block, so the path deny
# becomes unattributable and the ladder is dead on arrival. In-workspace is the
# only placement where C1 can reach the target and the deny can be the cause.
# SCOPE, stated because it is a real narrowing: vera's deny was on a SIBLING
# bot dir, i.e. out of tree. This ladder therefore measures an IN-WORKSPACE
# path deny. What the aborted run already establishes about the out-of-tree
# case is that B0 returned the PERMISSION string there, so the deny is
# evaluated and preempts the workspace rule -- but a bot with no deny composed
# is blocked out-of-tree anyway, by the boundary alone.
TARGET_DIR="$BOT_DIR/target"
TARGET="$TARGET_DIR/secret.txt"
LOC2="$BOT_DIR/.claude/settings.json"
LOC3="$BOT_DIR/.claude/settings.local.json"
GRID="$WORK/grid.psv"
LOG="$WORK/ladder.log"

pass=0; fail=0
say() { printf '%s\n' "$*" | tee -a "$LOG"; }

cleanup() {
  if [ -n "${LADDER_KEEP:-}" ]; then printf 'kept artifacts: %s\n' "$WORK"; return; fi
  rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "$FAKE_CFG" "$EXPORT_ROOT/local/$FLEET"
# The target now lives under the bot dir, which generate owns, so it is written
# after EVERY compose (below) rather than once here.
write_target() { mkdir -p "$TARGET_DIR" && printf '%s\n' "$SENTINEL" > "$TARGET"; }

# ============================================================ PHASE 0: setup
say "== PHASE 0 — export, compose, preconditions =="

git -C "$SRC_ROOT" archive --format=tar HEAD 2>/dev/null | tar -x -C "$EXPORT_ROOT" \
  || { say "FATAL: git archive failed"; exit 2; }

# Pick an interpreter that can actually RESOLVE the compositor's deps, rather
# than the one that merely exists. A checkout .venv predating a dependency
# (pydantic, here) fails at import and the failure reads as "generate failed",
# which is a harness defect wearing the costume of a real one.
PYBIN=""
for cand in "$SRC_ROOT/.venv/bin/python" "$(command -v python3)"; do
  [ -x "$cand" ] || continue
  if "$cand" -c 'import pydantic, yaml, jinja2' >/dev/null 2>&1; then PYBIN="$cand"; break; fi
done
[ -n "$PYBIN" ] || { say "FATAL: no python3 resolves the compositor deps (pydantic/yaml/jinja2)"; exit 2; }
say "  compositor interpreter: $PYBIN"
say "  source ref            : $(git -C "$SRC_ROOT" rev-parse --short HEAD 2>/dev/null)"

# ---- location 1 (the user tier, inside the redirected HOME) -----------------
# Level 0 and level 1 differ by EXACTLY one array element. Absent-vs-present
# would be two changes (file existence AND content); this is one.
write_loc1() {  # write_loc1 <0|1>
  if [ "$1" = 1 ]; then printf '{"permissions":{"allow":["Bash"]}}\n' > "$FAKE_CFG/settings.json"
  else                  printf '{"permissions":{"allow":[]}}\n'       > "$FAKE_CFG/settings.json"; fi
}

# ---- location 3 (composed) --------------------------------------------------
# One generate per cell, through the REAL compositor, so location 3 is composed
# exactly as production composes it rather than hand-patched.
compose() {  # compose <bare_bash:0|1> <path_deny:0|1> <bare_tool_denies:0|1>
  local bare="$1" pdeny="$2" tdeny="$3" allow deny
  allow='"Bash(cat *)"'
  [ "$bare" = 1 ] && allow="\"Bash\", $allow"
  deny=""
  [ "$pdeny" = 1 ] && deny="\"Read(/$TARGET_DIR/**)\""
  if [ "$tdeny" = 1 ]; then
    [ -n "$deny" ] && deny="$deny, "
    deny="$deny\"Write\", \"Edit\", \"NotebookEdit\""
  fi
  cat > "$EXPORT_ROOT/local/$FLEET/fleet.yaml" <<YAML
fleet:
  name: $FLEET
  service_prefix: $PREFIX
  plugins:
    include_defaults: false
  bots:
    $BOT:
      name: $BOT
      # DELIBERATELY NOT code-review, and the reason is a defect the smoke run
      # caught: code-review's frontmatter carries deny: [Write, Edit,
      # NotebookEdit]. Those land in EVERY cell from the expertise layer, so C7
      # — whose whole job is to REMOVE the bare tool denies — could not move
      # them: it composed the identical deny set as B0 and would have reported
      # "C7 == B0, therefore the bare-Bash line is unconfounded" as evidence,
      # when the cell had simply never flipped. ai-platform-reviewer declares
      # scoped allows and NO denies, so every deny in the composed file traces
      # to this harness's own declaration and C7 is a real flip.
      expertise:
        - ai-platform-reviewer
      channels: []
      mcp: []
      tool_permissions:
        allow: [$allow]
        deny: [$deny]
YAML
  # HOME is NOT redirected for generate, and the asymmetry is deliberate rather
  # than an oversight. The redirection exists to isolate CLAUDE CODE's settings
  # reads; generate never opens ~/.claude/settings.json. Redirecting it here
  # instead hides ~/.local site-packages from the interpreter, so the compositor
  # dies on a missing pydantic/yaml and the failure reads as "generate failed"
  # — a harness defect wearing the costume of a real one. The isolation claim is
  # carried by the strace assertion over the CELL, which is where it belongs.
  ( cd "$EXPORT_ROOT" && CLAUDLOBBY_ROOT="$EXPORT_ROOT" \
      "$PYBIN" -m claudlobby --fleet "$FLEET" generate ) >"$WORK/generate.log" 2>&1
  local rc=$?
  write_target   # generate owns the bot dir; re-lay the target after every pass
  return $rc
}

compose 0 1 1 || { say "FATAL: baseline generate failed:"; tail -20 "$WORK/generate.log" | tee -a "$LOG"; exit 2; }
[ -f "$LOC3" ] || { say "FATAL: no composed $LOC3"; exit 2; }

# ---- the channels guard: check the COMPOSITION, not the declaration ---------
composed_flags="$(grep -E '^CLAUDE_FLAGS=' "$BOT_DIR/bot.conf" 2>/dev/null || true)"
say "  composed CLAUDE_FLAGS: ${composed_flags:-<none>}"
if printf '%s' "$composed_flags" | grep -q -- '--channels'; then
  say "FATAL: composed bot.conf carries --channels despite 'channels: []'."
  say "       Refusing to boot — this is the shape that took five bots Telegram-dark."
  exit 2
fi
harness_check "composed bot.conf carries NO --channels (channels: [] took)" yes
mcp_empty=no
if [ ! -s "$BOT_DIR/.mcp.json" ]; then mcp_empty=yes
elif [ "$(jq -r '.mcpServers|length' "$BOT_DIR/.mcp.json" 2>/dev/null)" = 0 ]; then mcp_empty=yes; fi
harness_check "composed .mcp.json absent-or-empty (mcp: [])" "$mcp_empty"

# ---- seed auth + trust into the redirected config dir -----------------------
seed_claude_auth_and_trust "$FAKE_CFG" "$BOT_DIR" "$CLAUDE_BIN" "$HOST_CREDS"

# ---- the clauDNA PreToolUse hook must be absent (a confound in every cell) ---
hook_present=no
[ -n "$(find "$FAKE_HOME" -name 'pretooluse-permissions.sh' 2>/dev/null | head -1)" ] && hook_present=yes
harness_check "clauDNA pretooluse-permissions.sh absent from the redirected HOME" \
  "$([ "$hook_present" = no ] && echo yes || echo no)"

# ---- G3: the target exists, with known content, readable unconstrained ------
unconstrained="$(cat "$TARGET" 2>&1)"
harness_check "target exists and reads the sentinel from an unconstrained context" \
  "$([ "$unconstrained" = "$SENTINEL" ] && echo yes || echo no)"

REAL_GLOBAL_MTIME_BEFORE="$(stat -c %Y "$REAL_GLOBAL" 2>/dev/null || echo missing)"

# ============================================ preconditions, recorded per cell
record_preconditions() {  # record_preconditions <cell>
  local cell="$1"
  {
    printf '### preconditions for %s (recorded BEFORE the run)\n' "$cell"
    printf 'claude --version      : %s\n' "$("$CLAUDE_BIN" --version 2>&1 | head -1)"
    printf 'loc1 path             : %s\n' "$FAKE_CFG/settings.json"
    printf 'loc1 bytes            : %s\n' "$(cat "$FAKE_CFG/settings.json" 2>&1)"
    printf 'loc1 bare Bash        : %s\n' \
      "$(jq -r 'if ((.permissions.allow // []) | index("Bash")) != null then "PRESENT" else "absent" end' "$FAKE_CFG/settings.json" 2>&1)"
    printf 'loc2 path             : %s\n' "$LOC2"
    printf 'loc2 state            : %s\n' \
      "$([ ! -e "$LOC2" ] && echo absent || printf 'present size=%s' "$(stat -c %s "$LOC2")")"
    printf 'loc3 mtime            : %s\n' "$(stat -c %Y "$LOC3" 2>&1)"
    printf 'loc3 bare Bash        : %s\n' \
      "$(jq -r 'if ((.permissions.allow // []) | index("Bash")) != null then "PRESENT" else "absent" end' "$LOC3" 2>&1)"
    printf 'loc3 Bash(cat *)      : %s\n' \
      "$(jq -r 'if ((.permissions.allow // []) | index("Bash(cat *)")) != null then "PRESENT" else "absent" end' "$LOC3" 2>&1)"
    printf 'loc3 deny array       : %s\n' "$(jq -c '.permissions.deny // []' "$LOC3" 2>&1)"
    printf 'HOME (harness view)   : %s\n' "$FAKE_HOME"
    printf 'CLAUDE_CONFIG_DIR     : %s\n' "$FAKE_CFG"
  } | tee -a "$LOG"
  # loc2 must be absent-or-empty in EVERY cell (non-negotiable 2).
  if [ -e "$LOC2" ] && [ -s "$LOC2" ]; then
    say "  FATAL: location 2 is present and NON-EMPTY for $cell — pinned factor moved."
    exit 1
  fi
}

# ==================================================================== the cell
# Classification is read off the structured transcript; the raw error string is
# preserved BYTE-FOR-BYTE in <cell>.results (JSONL), because the discriminator
# in the earlier report was exactly which string came back.
json_lines() { grep '^{' "$1" 2>/dev/null; }

# The FACTOR SIGNATURE, read back off disk after compose. A cell whose flip did
# not actually move is the worst failure available to this harness: it produces
# a verdict identical to the baseline and that identity gets reported as
# "this factor does not matter". The smoke run hit exactly that (C7 vs an
# expertise-contributed deny), so intent is now asserted against composition
# rather than assumed from the compose() arguments.
factor_sig() {  # factor_sig <mode> <tool>
  local l1 l3 pd td
  l1="$(jq -r 'if ((.permissions.allow // []) | index("Bash")) != null then 1 else 0 end' "$FAKE_CFG/settings.json" 2>/dev/null)"
  l3="$(jq -r 'if ((.permissions.allow // []) | index("Bash")) != null then 1 else 0 end' "$LOC3" 2>/dev/null)"
  pd="$(jq -r 'if ((.permissions.deny // []) | map(select(startswith("Read("))) | length) > 0 then 1 else 0 end' "$LOC3" 2>/dev/null)"
  td="$(jq -r 'if ((.permissions.deny // []) | index("Write")) != null then 1 else 0 end' "$LOC3" 2>/dev/null)"
  printf 'l1bare=%s l3bare=%s pathdeny=%s tooldeny=%s mode=%s tool=%s' "$l1" "$l3" "$pd" "$td" "$1" "$2"
}

run_cell() {  # run_cell <name> <mode> <tool:Bash|Read> <expected_sig> [strace:1]
  local cell="$1" mode="$2" tool="$3" expect="$4" trace="${5:-}"
  local out="$WORK/$cell.jsonl" prompt verdict raw toolname rc is_err oneline actual

  actual="$(factor_sig "$mode" "$tool")"
  harness_check "$cell composed the intended flip [$actual]" \
    "$([ "$actual" = "$expect" ] && echo yes || echo no)"
  if [ "$actual" != "$expect" ]; then
    say "  FATAL: $cell wanted [$expect] but composed [$actual] — the flip did not move."
    say "         A cell that cannot move reports its non-result as evidence. Stopping."
    exit 1
  fi

  if [ "$tool" = Read ]; then
    prompt="Use the Read tool to read the file $TARGET. Do not use the Bash tool. Then report, verbatim and in full, either the file contents or the exact error text you received."
  else
    prompt="Use the Bash tool to run exactly this command: cat $TARGET. Do not use the Read tool. Then report, verbatim and in full, either the command output or the exact error text you received."
  fi

  record_preconditions "$cell"
  say "  running $cell (mode=$mode tool=$tool model=$MODEL)"

  if [ -n "$trace" ]; then
    ( cd "$BOT_DIR" && HOME="$FAKE_HOME" CLAUDE_CONFIG_DIR="$FAKE_CFG" \
        strace -f -e trace=openat,open -o "$WORK/$cell.strace" \
        "$_TIMEOUT_BIN" "$CELL_TIMEOUT" "$CLAUDE_BIN" -p "$prompt" \
        --permission-mode "$mode" --output-format stream-json --verbose --model "$MODEL" \
        > "$out" 2>&1 )
  else
    ( cd "$BOT_DIR" && HOME="$FAKE_HOME" CLAUDE_CONFIG_DIR="$FAKE_CFG" \
        "$_TIMEOUT_BIN" "$CELL_TIMEOUT" "$CLAUDE_BIN" -p "$prompt" \
        --permission-mode "$mode" --output-format stream-json --verbose --model "$MODEL" \
        > "$out" 2>&1 )
  fi
  rc=$?

  # THE MODE THE SESSION ACTUALLY RAN AT, read off its own init record rather
  # than assumed from the flag. Measured on claude 2.1.240: headless `claude -p`
  # resolves BOTH --permission-mode auto and --permission-mode manual to
  # "default". The flag is not ignored -- bypassPermissions round-trips as
  # bypassPermissions -- so those two values genuinely map onto one mode. That
  # makes a headless auto-vs-manual cell UNRUNNABLE: it re-runs the baseline
  # under a different spelling and its agreement with the baseline reads as
  # "mode is not load-bearing", which is a claim the run never tested.
  local session_mode
  session_mode="$(json_lines "$out" \
    | jq -rc 'select(.type=="system" and .subtype=="init") | .permissionMode' 2>/dev/null | head -1)"
  session_mode="${session_mode:-UNKNOWN}"
  say "     session permissionMode (from init record): $session_mode  [flag passed: $mode]"

  toolname="$(json_lines "$out" \
    | jq -rc 'select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") | .name' \
      2>/dev/null | paste -sd, -)"
  # VERBATIM, untouched, one JSON object per tool_result.
  json_lines "$out" \
    | jq -c 'select(.type=="user") | .message.content[]? | select(.type=="tool_result") | {is_error, content}' \
      2>/dev/null > "$WORK/$cell.results"
  is_err="$(jq -rs 'if length==0 then "none" else ((.[0].is_error) | tostring) end' "$WORK/$cell.results" 2>/dev/null)"
  raw="$(jq -rs 'map(.content | tostring) | join(" ")' "$WORK/$cell.results" 2>/dev/null | head -c 3000)"

  if [ -z "$toolname" ]; then
    verdict=NO_TOOL
  elif printf '%s' "$raw" | grep -qF "$SENTINEL"; then
    verdict=ALLOWED
  elif printf '%s' "$raw" | grep -qiE 'requires approval|awaiting approval|would you like|approve this'; then
    verdict=PROMPTED
  elif printf '%s' "$raw" | grep -qiE 'allowed working director|only concatenate files'; then
    verdict=BLOCKED_WORKDIR
  elif printf '%s' "$raw" | grep -qiE 'permission|denied|not allowed'; then
    verdict=DENIED
  elif [ "$rc" -ne 0 ] && [ -z "$raw" ]; then
    verdict=HUNG_OR_TIMEOUT
  elif [ "$is_err" = true ]; then
    verdict=ERROR_OTHER
  else
    verdict=UNCLASSIFIED
  fi

  oneline="$(printf '%s' "$raw" | tr -d '\r' | tr '\n' ' ' | head -c 500)"
  printf '%s|%s|%s|%s|%s|%s|%s|%s\n' \
    "$cell" "$mode" "$session_mode" "$tool" "${toolname:-none}" "$verdict" "$rc" "$oneline" >> "$GRID"
  printf '%s\n' "$session_mode" > "$WORK/$cell.mode"
  say "  -> $cell: $verdict (tool_used=${toolname:-none}, rc=$rc)"
  say "     RAW: $oneline"
  # The verdict travels by FILE, never by stdout. run_cell must run in the
  # PARENT shell: a $(...) capture forks a subshell, which swallows every
  # say()/harness_check() line it emits and drops the pass/fail increments on
  # exit -- the per-cell flip assertions were invisible and uncounted that way.
  printf '%s\n' "$verdict" > "$WORK/$cell.verdict"
}

verdict_of() { cat "$WORK/$1.verdict" 2>/dev/null; }

# The baseline signature, named once so every B0 assertion and the human-readable
# grid agree on what "baseline" meant.
BASE_SIG="l1bare=0 l3bare=0 pathdeny=1 tooldeny=1 mode=auto tool=Bash"
printf 'cell|flag_mode|session_mode|tool_asked|tool_used|verdict|rc|raw_error_verbatim\n' > "$GRID"

# =================================== PHASE 1: C1 positive control, FIRST + x2
say ""
say "== PHASE 1 — C1 positive control (runs FIRST; B0==C1 means harness broken) =="
compose 0 0 1 || { say "FATAL: C1 generate failed"; exit 2; }
write_loc1 0
run_cell C1a auto Bash "l1bare=0 l3bare=0 pathdeny=0 tooldeny=1 mode=auto tool=Bash" 1
C1a="$(verdict_of C1a)"
run_cell C1b auto Bash "l1bare=0 l3bare=0 pathdeny=0 tooldeny=1 mode=auto tool=Bash"
C1b="$(verdict_of C1b)"

# ---- non-negotiable 1: the HOME redirection ASSERTION, from the strace ------
say ""
say "== isolation assertion (strace, C1a) =="
real_opens="$(grep -c -- "$REAL_GLOBAL" "$WORK/C1a.strace" 2>/dev/null)"; real_opens="${real_opens:-0}"
fake_opens="$(grep -c -- "$FAKE_CFG/settings.json" "$WORK/C1a.strace" 2>/dev/null)"; fake_opens="${fake_opens:-0}"
say "  openat hits on REAL operator global ($REAL_GLOBAL): $real_opens  (must be 0)"
say "  openat hits on FAKE  location 1 ($FAKE_CFG/settings.json): $fake_opens  (must be >0)"
harness_check "operator real ~/.claude/settings.json NEVER opened (isolation held)" \
  "$([ "$real_opens" -eq 0 ] && echo yes || echo no)"
harness_check "redirected location 1 WAS opened (the file under test is the one read)" \
  "$([ "$fake_opens" -gt 0 ] && echo yes || echo no)"
say "  (positive control on the instrument: >0 fake opens proves the strace filter"
say "   itself catches settings reads, so the 0 above is a real absence, not a"
say "   filter that never matched anything.)"
# A failed assertion above must arrive as a DIAGNOSIS, not a bare 'no'. If the
# binary reads neither candidate for location 1, C3 is uninterpretable and the
# reader needs to see WHICH settings files it actually opened to know that.
say "  settings-shaped paths actually opened during C1a (deduped):"
grep -oE '"[^"]*settings[^"]*"' "$WORK/C1a.strace" 2>/dev/null | tr -d '"' | sort -u \
  | sed 's/^/    /' | tee -a "$LOG" | head -25
say "  .claude.json / config paths opened:"
grep -oE '"[^"]*\.claude[^"]*"' "$WORK/C1a.strace" 2>/dev/null | tr -d '"' | sort -u \
  | grep -vE 'settings' | sed 's/^/    /' | tee -a "$LOG" | head -15

# ================================================= PHASE 2: B0 baseline, x2
say ""
say "== PHASE 2 — B0 baseline x2 =="
compose 0 1 1 || { say "FATAL: B0 generate failed"; exit 2; }
write_loc1 0
run_cell B0a auto Bash "$BASE_SIG"
B0a="$(verdict_of B0a)"
run_cell B0b auto Bash "$BASE_SIG"
B0b="$(verdict_of B0b)"

# ---- harness integrity gate -------------------------------------------------
say ""
say "== harness integrity gate =="
harness_check "C1 self-consistent (C1a=$C1a C1b=$C1b)" "$([ "$C1a" = "$C1b" ] && echo yes || echo no)"
harness_check "B0 self-consistent (B0a=$B0a B0b=$B0b)" "$([ "$B0a" = "$B0b" ] && echo yes || echo no)"
harness_check "C1 (deny absent) reaches the target — positive control" \
  "$([ "$C1a" = ALLOWED ] && echo yes || echo no)"
harness_check "B0 differs from C1 — the deny is what denies" \
  "$([ "$B0a" != "$C1a" ] && echo yes || echo no)"

if [ "$C1a" != ALLOWED ] || [ "$B0a" = "$C1a" ] || [ "$C1a" != "$C1b" ] || [ "$B0a" != "$B0b" ]; then
  say ""
  say "STOP — harness integrity failed. B0=$B0a/$B0b  C1=$C1a/$C1b."
  say "Per the plan: nothing downstream counts. Not running C2-C7."
  column -t -s'|' "$GRID" 2>/dev/null | tee -a "$LOG"
  if [ -n "${LADDER_OUT:-}" ]; then
    mkdir -p "$LADDER_OUT" && cp "$GRID" "$LOG" "$WORK"/*.results "$LADDER_OUT/" 2>/dev/null
  fi
  exit 1
fi

# ==================================================== PHASE 3: the flips
say ""
say "== PHASE 3 — one flip per cell =="

compose 1 1 1 || { say "FATAL: C2 generate failed"; exit 2; }   # +bare Bash @ loc3
write_loc1 0
run_cell C2 auto Bash "l1bare=0 l3bare=1 pathdeny=1 tooldeny=1 mode=auto tool=Bash"
C2="$(verdict_of C2)"

compose 0 1 1 || { say "FATAL: C3 generate failed"; exit 2; }   # +bare Bash @ loc1
write_loc1 1
run_cell C3 auto Bash "l1bare=1 l3bare=0 pathdeny=1 tooldeny=1 mode=auto tool=Bash"
C3="$(verdict_of C3)"

# C5 -- mode -- is NOT RUN, and that is a refusal rather than an omission.
# Measured (claude 2.1.240, init record, both directions): headless `claude -p`
# resolves --permission-mode auto AND manual to "default", while
# bypassPermissions round-trips intact. So the auto/manual flip is unavailable
# in this arm: running it would re-run B0 under a different flag spelling and
# return B0's verdict, and that agreement would be read as "mode does not
# change enforcement" -- a conclusion the cell never earned. The flip needs the
# interactive tmux boot the plan called "a reboot, not a TUI toggle"; it is not
# a headless cell. Recorded as NOT_MEASURABLE so the gap is visible in the grid
# rather than absent from it.
C5=NOT_MEASURABLE_HEADLESS
printf 'C5|manual|n/a|Bash|none|%s|-|auto and manual both resolve to session permissionMode=default in headless -p (claude 2.1.240); flip unavailable in this arm, needs an interactive boot\n' \
  "$C5" >> "$GRID"
say "  -> C5: $C5 (not run -- the flip is unavailable headless; see grid note)"

compose 0 1 1 || { say "FATAL: C6 generate failed"; exit 2; }   # Read tool
write_loc1 0
run_cell C6 auto Read "l1bare=0 l3bare=0 pathdeny=1 tooldeny=1 mode=auto tool=Read"
C6="$(verdict_of C6)"

compose 0 1 0 || { say "FATAL: C7 generate failed"; exit 2; }   # bare tool denies removed
write_loc1 0
run_cell C7 auto Bash "l1bare=0 l3bare=0 pathdeny=1 tooldeny=0 mode=auto tool=Bash"
C7="$(verdict_of C7)"

# ============================================================ close-out
say ""
say "== close-out =="
# Every RUN cell must share one session mode, or the ladder varied a second
# factor without saying so. This is the check the factor signature could not
# make, because it compared intent to intent.
modes_seen="$(cat "$WORK"/*.mode 2>/dev/null | sort -u | paste -sd, -)"
harness_check "every run cell shared ONE session permissionMode [$modes_seen]" \
  "$([ "$(cat "$WORK"/*.mode 2>/dev/null | sort -u | wc -l)" -eq 1 ] && echo yes || echo no)"
say "  SCOPE: that mode is what this grid measures. Production ai-platform bots"
say "         run --permission-mode auto in an INTERACTIVE tmux session; this arm"
say "         is headless. Transfer is a claim about the permission layer, not a"
say "         measurement of a tmux session."
REAL_GLOBAL_MTIME_AFTER="$(stat -c %Y "$REAL_GLOBAL" 2>/dev/null || echo missing)"
harness_check "operator real ~/.claude/settings.json UNMODIFIED (mtime $REAL_GLOBAL_MTIME_BEFORE == $REAL_GLOBAL_MTIME_AFTER)" \
  "$([ "$REAL_GLOBAL_MTIME_BEFORE" = "$REAL_GLOBAL_MTIME_AFTER" ] && echo yes || echo no)"

if [ "$C2" != "$C3" ]; then
  say "  NOTE: C2 ($C2) and C3 ($C3) DISAGREE — the plan makes C4 (both flips)"
  say "        indicated. NOT run: 'if a cell suggests another cell, report it'."
fi

say ""
say "== RAW GRID =="
column -t -s'|' "$GRID" 2>/dev/null | tee -a "$LOG"
say ""
say "rehearse-permissions-ladder: $pass passed, $fail failed"
if [ -n "${LADDER_OUT:-}" ]; then
  mkdir -p "$LADDER_OUT" && cp "$GRID" "$LOG" "$WORK"/*.results "$LADDER_OUT/" 2>/dev/null
  say "artifacts copied to $LADDER_OUT"
fi
exit 0
