#!/usr/bin/env bash
# rehearse-staged-claude-update.sh — the #1768 canary: the REAL staged update
# (update-claude-code.sh with CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED=1), REAL npm
# and the REAL package, against a THROWAWAY root. Never the live binary, never
# the fleet link, never a fleet, never the operator's Telegram.
#
# What it proves, in order:
#   1. normal arm, version A: staged into its own prefix, verified, linked, and
#      no sudo anywhere (a sudo shim that records and fails is first on PATH);
#   2. a real claude session started THROUGH the link runs A: its own bash mode
#      reports CLAUDE_CODE_EXECPATH = A and runs rg, which on this host can only
#      come from that binary (there is no system rg, and the session HOME is
#      throwaway, so the rg function's fallbacks are empty);
#   3. normal arm, version B: the link moves to B, and A is kept as the rollback;
#   4. THE RUNNING-SESSION PROPERTY: the session started on A is still alive,
#      /proc/<pid>/exe still names A, and its bash mode still reports A and still
#      runs rg — after the link moved under it;
#   5. stub arm, version C: npm with --ignore-scripts reproduces the 2026-09-23
#      stub on demand (#1768 pre-scope). The verify fails, the link does NOT
#      move, C never becomes a version, and the alert fires (captured).
#
# Cost: three real installs, ~227 MB each (~80 s each on the Pi), and one
# credential-less claude TUI for the session arms. It cannot spend: the session
# has no credentials and ANTHROPIC_BASE_URL points at a dead port.
#
# Isolation is structural, then ASSERTED:
#   - everything lives under one mktemp base; cleanup refuses any other path;
#   - the job runs under `env -i`: HOME, CLAUDLOBBY_ROOT and the plane socket are
#     the base's, TELEGRAM_* and the fleet's variables are simply absent, and
#     alerts go to a recording tg-post stub under the throwaway root;
#   - CLAUDE_UPDATE_FLEET_PATH is an empty dir, so the job cannot even measure
#     the host's own claude;
#   - the live binary (the target of `command -v claude`) and the live fleet
#     link are fingerprinted before and after; a change FAILS the run.
#
# Usage: REHEARSE_STAGED_UPDATE_REAL=1 lib/rehearse-staged-claude-update.sh [A B C]
#   A, B, C default to three published versions, newest last (C, the stub arm).
#   REHEARSE_KEEP=1 keeps the base for inspection; REHEARSE_NPM_CACHE=<dir> keeps
#   npm's download cache across runs.
# Exit: 0 every check passed, 1 a check failed, 2 refused (gate, deps, input),
#       3 isolation could not be established or did not hold.

set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "$LIB_DIR/.." && pwd)"

if [ "${REHEARSE_STAGED_UPDATE_REAL:-}" != "1" ]; then
    echo "rehearse-staged-claude-update: refused — this downloads ~700 MB through real npm." >&2
    echo "  Set REHEARSE_STAGED_UPDATE_REAL=1 to run it." >&2
    exit 2
fi
for dep in npm node jq tmux python3; do
    command -v "$dep" >/dev/null 2>&1 || { echo "rehearse: missing $dep" >&2; exit 2; }
done
if [ ! -e /proc/self/exe ]; then
    echo "rehearse: needs /proc (the running-session and prune checks read it)" >&2
    exit 2
fi
REAL_NPM="$(command -v npm)"
NODE_DIR="$(dirname "$(command -v node)")"
PLANE_CLI="$SRC_ROOT/.venv/bin/claudlobby"
[ -x "$PLANE_CLI" ] || PLANE_CLI="$(command -v claudlobby || true)"
[ -n "$PLANE_CLI" ] || { echo "rehearse: no claudlobby CLI for the plane's cold rung" >&2; exit 2; }

if [ "$#" -eq 3 ]; then
    VA="$1" VB="$2" VC="$3"
else
    # shellcheck disable=SC2207
    _vs=($("$REAL_NPM" view @anthropic-ai/claude-code versions --json 2>/dev/null \
        | jq -r '.[]' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | tail -3))
    [ "${#_vs[@]}" -eq 3 ] || { echo "rehearse: could not list three versions" >&2; exit 2; }
    VA="${_vs[0]}" VB="${_vs[1]}" VC="${_vs[2]}"
fi
for v in "$VA" "$VB" "$VC"; do
    [[ $v =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "rehearse: not a version: $v" >&2; exit 2; }
done

# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
set +e

BASE="$(mktemp -d "${TMPDIR:-/tmp}/rehearse-1768.XXXXXX")" || exit 3
ROOT="$BASE/root"
SOCK="rehearse-1768-$$"
fail=0

# --- bookkeeping ---------------------------------------------------------------
check() {  # check "<what>" <yes|no>
    if [ "$2" = yes ]; then printf '  PASS  %s\n' "$1"; else printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); fi
}
cleanup() {
    tmux -L "$SOCK" kill-server 2>/dev/null || true
    [ "${REHEARSE_KEEP:-}" = 1 ] && { echo "kept: $BASE"; return 0; }
    case "$BASE" in
        "${TMPDIR:-/tmp}"/rehearse-1768.*) rm -rf "$BASE" 2>/dev/null || { sleep 2; rm -rf "$BASE"; } ;;
        *) echo "rehearse: refusing to remove $BASE" >&2 ;;
    esac
}
trap cleanup EXIT

# The live binary and the live fleet link, fingerprinted: a rehearsal that
# touched either has failed however its own checks read.
fingerprint() {
    local live
    live="$(readlink -f "$(command -v claude 2>/dev/null)" 2>/dev/null || true)"
    printf 'live=%s %s\n' "$live" "$( [ -n "$live" ] && stat -c '%i %s %Y' "$live" 2>/dev/null )"
    printf 'link=%s\n' "$(readlink "${CLAUDLOBBY_ROOT:-$SRC_ROOT}/state/bin/claude" 2>/dev/null || echo absent)"
}
FP_BEFORE="$(fingerprint)"

# --- the throwaway root ----------------------------------------------------------
mkdir -p "$ROOT/lib" "$ROOT/runtime/bots/rbot" "$BASE/home/.local/bin" "$BASE/empty"
cat > "$ROOT/lib/tg-post.sh" <<STUB
#!/bin/bash
printf '%s\n' "\$*" >> "$BASE/tg-capture"
STUB
chmod +x "$ROOT/lib/tg-post.sh"
printf 'export TELEGRAM_GROUP_CHAT_ID="-1001234567890"\n' > "$ROOT/runtime/bots/rbot/bot.conf"
# First on the job's PATH (it prepends $HOME/.local/bin): any sudo is recorded
# and refused, so "no sudo" is observed rather than assumed.
cat > "$BASE/home/.local/bin/sudo" <<STUB
#!/bin/bash
printf '%s\n' "\$*" >> "$BASE/sudo-calls"
exit 1
STUB
chmod +x "$BASE/home/.local/bin/sudo"

LINK="$ROOT/state/bin/claude"
exe_of() { printf '%s/state/claude/versions/%s/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe' "$ROOT" "$1"; }

# REHEARSE_NPM_CACHE keeps npm's download cache across runs (a re-run then
# fetches nothing); by default it lives, and dies, under the base.
NPM_CACHE="${REHEARSE_NPM_CACHE:-$BASE/home/.npm}"
run_job() {  # run_job <version> -> rc; appends the job's own log to the transcript
    env -i HOME="$BASE/home" PATH="$NODE_DIR:/usr/bin:/bin" LANG=C.UTF-8 \
        npm_config_cache="$NPM_CACHE" \
        CLAUDLOBBY_ROOT="$ROOT" CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED=1 \
        CLAUDE_UPDATE_VERSION="$1" CLAUDE_UPDATE_FLEET_PATH="$BASE/empty" \
        PLANE_EMIT_CLI="$PLANE_CLI" PLANE_SOCKET="$BASE/no-daemon.sock" \
        FLEET_EVENT_EMIT_TIMEOUT_S=120 \
        bash "$LIB_DIR/update-claude-code.sh" >>"$BASE/job.out" 2>&1
}
plane_events() {
    python3 - "$ROOT/state/plane/plane.db" <<'PY' 2>/dev/null || true
import sqlite3, sys
try:
    c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
    for (e,) in c.execute("SELECT event FROM events WHERE source_ref LIKE 'fleet-events:%'"):
        print(e)
except sqlite3.Error:
    pass
PY
}
show_log() {
    sed -n "${1},\$p" "$ROOT/state/claude-update.log" 2>/dev/null | sed 's/^/      /'
}
log_lines() { wc -l < "$ROOT/state/claude-update.log" 2>/dev/null | tr -d ' ' || echo 0; }

echo "rehearse-staged-claude-update (#1768)"
echo "  versions: A=$VA  B=$VB  C=$VC (stub arm, --ignore-scripts)"
echo "  npm:      $REAL_NPM ($("$REAL_NPM" --version)), node $(node --version)"
echo "  base:     $BASE"
echo ""

# --- 1. normal arm, A ---------------------------------------------------------------
echo "== 1. normal arm: stage $VA =="
from=$(( $(log_lines) + 1 )); t0=$(date +%s)
run_job "$VA"; rc=$?
echo "    job rc=$rc in $(( $(date +%s) - t0 ))s"; show_log "$from"
check "A: the job succeeded" "$([ "$rc" -eq 0 ] && echo yes || echo no)"
check "A: the fleet link names A's binary" "$([ "$(readlink "$LINK")" = "$(exe_of "$VA")" ] && echo yes || echo no)"
out="$("$LINK" --version 2>&1 | head -1)"
check "A: the link runs A ($out)" "$(case "$out" in "$VA"*) echo yes ;; *) echo no ;; esac)"
check "A: the staged binary is the real one (size $(wc -c < "$(exe_of "$VA")" | tr -d ' ') bytes)" \
    "$([ "$(wc -c < "$(exe_of "$VA")" | tr -d ' ')" -gt 100000000 ] && echo yes || echo no)"
check "A: no sudo was invoked" "$([ ! -e "$BASE/sudo-calls" ] && echo yes || echo no)"

# --- 2. a real session through the link ----------------------------------------------
echo ""
echo "== 2. a real claude session, launched through the link =="
mkdir -p "$BASE/sess-home" "$BASE/cfg" "$BASE/cwd"
CWD="$(cd -P "$BASE/cwd" && pwd -P)"
jq -n --arg cwd "$CWD" --arg ver "$VA" '{hasCompletedOnboarding:true,lastOnboardingVersion:$ver,
  theme:"dark",projects:{($cwd):{hasTrustDialogAccepted:true,hasCompletedProjectOnboarding:true,
  allowedTools:[],history:[]}}}' > "$BASE/cfg/.claude.json"
tmux -L "$SOCK" new-session -d -s sess -x 500 -y 50 \
    "cd '$CWD' && exec env -i PATH=/usr/local/bin:/usr/bin:/bin HOME='$BASE/sess-home' TERM=xterm-256color LANG=C.UTF-8 LC_ALL=C.UTF-8 CLAUDE_CONFIG_DIR='$BASE/cfg' ANTHROPIC_BASE_URL=http://127.0.0.1:9 '$LINK'"
box="$(PANE_READY_TICKS=120 pane_await_input_box "$SOCK" sess)"
check "session: the TUI drew its input box ($box)" "$([ "$box" = drawn ] && echo yes || echo no)"
PID="$(tmux -L "$SOCK" list-panes -t sess -F '#{pane_pid}' 2>/dev/null | head -1)"

# The pane, with the no-break space the TUI draws after its output glyph
# (U+00A0, measured in a live capture) folded to a plain one, so a marker match
# does not depend on which space the renderer chose.
cap() { tmux -L "$SOCK" capture-pane -t sess -p -S -200 2>/dev/null | sed 's/\xc2\xa0/ /g'; }
bash_mode() {  # bash_mode <marker> <command>: run it in the session's bash mode
    tmux -L "$SOCK" send-keys -t sess -l "!echo $1; $2"
    sleep 1
    tmux -L "$SOCK" send-keys -t sess Enter
    local i=0
    while [ "$i" -lt 30 ]; do
        cap | grep -q "⎿  $1" && break
        sleep 1; i=$((i + 1))
    done
    cap | awk -v m="$1" 'index($0, "⎿  " m) {on=1} on {print} /Not logged in/ && on {exit}'
}
probe_session() {  # probe_session <tag> <version the session must still run>
    local tag="$1" want o1 o2
    want="$(exe_of "$2")"
    o1="$(bash_mode "ZZ_EXEC_$tag" 'echo "EXECPATH=$CLAUDE_CODE_EXECPATH"')"
    printf '%s\n' "$o1" | sed 's/^/      /'
    check "$tag: the session's CLAUDE_CODE_EXECPATH names $2's binary" \
        "$(printf '%s' "$o1" | grep -qF "EXECPATH=$(readlink -f "$want")" && echo yes || echo no)"
    o2="$(bash_mode "ZZ_RG_$tag" 'rg --version | head -1')"
    printf '%s\n' "$o2" | sed 's/^/      /'
    check "$tag: the session's own rg runs (its binary, re-executed)" \
        "$(printf '%s' "$o2" | grep -q 'ripgrep [0-9]' && echo yes || echo no)"
    check "$tag: /proc/$PID/exe names $2's binary" \
        "$([ "$(readlink "/proc/$PID/exe" 2>/dev/null)" = "$(readlink -f "$want")" ] && echo yes || echo no)"
}
probe_session "before-swap" "$VA"

# --- 3. normal arm, B ---------------------------------------------------------------------
echo ""
echo "== 3. normal arm: stage $VB (the link moves while the session runs) =="
from=$(( $(log_lines) + 1 )); t0=$(date +%s)
run_job "$VB"; rc=$?
echo "    job rc=$rc in $(( $(date +%s) - t0 ))s"; show_log "$from"
check "B: the job succeeded" "$([ "$rc" -eq 0 ] && echo yes || echo no)"
check "B: the fleet link names B's binary" "$([ "$(readlink "$LINK")" = "$(exe_of "$VB")" ] && echo yes || echo no)"
check "B: A is kept as the rollback" \
    "$([ -x "$(exe_of "$VA")" ] && [ "$(cat "$ROOT/state/claude/versions/.previous" 2>/dev/null)" = "$(exe_of "$VA")" ] && echo yes || echo no)"

# --- 4. the running-session property --------------------------------------------------------
echo ""
echo "== 4. the session started on A, after the link moved to B =="
check "after-swap: the session is still alive (pid $PID)" "$(kill -0 "$PID" 2>/dev/null && echo yes || echo no)"
probe_session "after-swap" "$VA"
tmux -L "$SOCK" kill-server 2>/dev/null || true

# --- 5. stub arm, C ------------------------------------------------------------------------------
echo ""
echo "== 5. stub arm: stage $VC with --ignore-scripts (the 09-23 stub, on demand) =="
cat > "$BASE/home/.local/bin/npm" <<STUB
#!/bin/bash
if [ "\$1" = install ]; then
    printf '%s\n' "\$* --ignore-scripts" >> "$BASE/npm-wrapper-calls"
    exec "$REAL_NPM" "\$@" --ignore-scripts
fi
exec "$REAL_NPM" "\$@"
STUB
chmod +x "$BASE/home/.local/bin/npm"
from=$(( $(log_lines) + 1 )); t0=$(date +%s)
run_job "$VC"; rc=$?
echo "    job rc=$rc in $(( $(date +%s) - t0 ))s"; show_log "$from"
check "C: the job FAILED (rc $rc)" "$([ "$rc" -eq 1 ] && echo yes || echo no)"
check "C: npm ran with --ignore-scripts" \
    "$(grep -q -- "@anthropic-ai/claude-code@$VC --ignore-scripts" "$BASE/npm-wrapper-calls" 2>/dev/null && echo yes || echo no)"
check "C: ... and npm said success (exit 0), the 09-23 shape" \
    "$(show_log "$from" | grep -q 'staging finished (npm exit 0)' && echo yes || echo no)"
check "C: the fleet link did NOT move (still B)" "$([ "$(readlink "$LINK")" = "$(exe_of "$VB")" ] && echo yes || echo no)"
check "C: the stub never became a version" "$([ ! -e "$ROOT/state/claude/versions/$VC" ] && echo yes || echo no)"
check "C: no staging left behind" "$(ls -a "$ROOT/state/claude/versions" | grep -q '^\.staging' && echo no || echo yes)"
check "C: the log names the stub in its own words" \
    "$(show_log "$from" | grep -q 'claude native binary not installed' && echo yes || echo no)"
check "C: binary_update_failed landed on the (throwaway) plane" \
    "$(plane_events | grep -qx binary_update_failed && echo yes || echo no)"
check "C: the Telegram leg fired (captured, never sent)" \
    "$(grep -q 'FLEET ALERT \[binary_update_failed\]' "$BASE/tg-capture" 2>/dev/null && echo yes || echo no)"
check "C: the fleet still runs B" "$(case "$("$LINK" --version 2>&1 | head -1)" in "$VB"*) echo yes ;; *) echo no ;; esac)"

# --- isolation held ------------------------------------------------------------------------------
echo ""
echo "== isolation =="
FP_AFTER="$(fingerprint)"
printf '    before: %s\n    after:  %s\n' "$(echo $FP_BEFORE)" "$(echo $FP_AFTER)"
check "the live binary and the live fleet link are untouched" "$([ "$FP_BEFORE" = "$FP_AFTER" ] && echo yes || echo no)"
check "no sudo at any point" "$([ ! -e "$BASE/sudo-calls" ] && echo yes || echo no)"

echo ""
if [ "$fail" -eq 0 ]; then echo "RESULT: all checks passed"; exit 0; fi
if [ "$FP_BEFORE" != "$FP_AFTER" ]; then echo "RESULT: ISOLATION BROKEN — $fail failed"; exit 3; fi
echo "RESULT: $fail check(s) FAILED"; exit 1
