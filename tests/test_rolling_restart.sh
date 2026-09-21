#!/usr/bin/env bash
# tests/test_rolling_restart.sh — rolling-restart.sh + wait_bridge_ready gate.
# Standalone bash (not pytest-collected). Runs under macOS /bin/bash (3.2).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
PASS=0; FAIL=0; TOTAL=0
assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS + 1)); else echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1)); fi
}

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export CLAUDLOBBY_ROOT="$T"

echo "=== bridge_fence_write + wait_bridge_ready — the marker-fenced gate ==="
# shellcheck source=../lib/lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT="$T/bot"; mkdir -p "$BOT/logs"
LOG="$BOT/logs/startup.log"

# Prior-boot content that already contains a (stale) BRIDGE_READY — the exact
# "looks healthy but THIS boot never came up" trap the gate exists to stop.
printf '%s\n' "2026-01-01 POLL_START — old boot" "2026-01-01 BRIDGE_READY — Telegram poller up" > "$LOG"

# (1) bridge_fence_write appends a unique marker and echoes the token.
tok="$(bridge_fence_write "$BOT")"
assert_eq "(1) fence marker written to the log" "true" "$(grep -q "$tok" "$LOG" && echo true || echo false)"

# (2) The stale BRIDGE_READY (BEFORE the marker) must NOT pass.
wait_bridge_ready "$BOT" 0 "$tok" && r=ready || r=timeout
assert_eq "(2) stale BRIDGE_READY before the marker is NOT accepted" "timeout" "$r"

# (3) A fresh BRIDGE_READY AFTER the marker passes.
printf '%s\n' "2026-07-23 POLL_START — this boot" "2026-07-23 BRIDGE_READY — Telegram poller up" >> "$LOG"
wait_bridge_ready "$BOT" 0 "$tok" && r=ready || r=timeout
assert_eq "(3) fresh BRIDGE_READY after the marker passes" "ready" "$r"

# (4) A NEW marker with no ready-yet after it → timeout (drives the serial wait).
tok2="$(bridge_fence_write "$BOT")"
printf '%s\n' "2026-07-23 POLL_START — restarted, bridge still coming up" >> "$LOG"
wait_bridge_ready "$BOT" 0 "$tok2" && r=ready || r=timeout
assert_eq "(4) new marker, BRIDGE_READY not yet written → timeout" "timeout" "$r"

# (5) Rotation that KEEPS the marker (log-rotate's line-tail): still passes.
: > "$LOG"
tok3="$(bridge_fence_write "$BOT")"
printf '%s\n' "2026-07-23 POLL_START — booting" "2026-07-23 BRIDGE_READY — Telegram poller up" >> "$LOG"
tail -n 3 "$LOG" > "$LOG.rot" && mv "$LOG.rot" "$LOG"   # rotation keeps the recent tail
wait_bridge_ready "$BOT" 0 "$tok3" && r=ready || r=timeout
assert_eq "(5) rotation keeping the marker still accepts a fresh BRIDGE_READY" "ready" "$r"

# (6) Rotation that DROPS the marker → fail CLOSED (timeout), NEVER false-ready.
#     This is the #696 finding: the old byte-offset fallback accepted a stale
#     pre-restart BRIDGE_READY here; the marker approach rejects it.
: > "$LOG"
printf '%s\n' "OLD POLL_START — prior boot" "OLD BRIDGE_READY — Telegram poller up" > "$LOG"  # marker rotated away
wait_bridge_ready "$BOT" 0 "RR_FENCE_that_was_rotated_away" && r=ready || r=timeout
assert_eq "(6) marker rotated away → fail CLOSED (stale BRIDGE_READY rejected)" "timeout" "$r"

echo ""
echo "=== rolling-restart.sh — fleet enumeration + CLI guards ==="

# rr_list_fleets finds fleets at BOTH depths (flat + nested). Source the script
# (source-guard keeps main from running) and call the enumerator directly.
mkdir -p "$T/local/flatfleet" "$T/local/sysA/nestedfleet"
printf 'fleet:\n  name: flatfleet\n' > "$T/local/flatfleet/fleet.yaml"
printf 'fleet:\n  name: nestedfleet\n' > "$T/local/sysA/nestedfleet/fleet.yaml"
# shellcheck source=../lib/rolling-restart.sh
. "$LIB_DIR/rolling-restart.sh"
_fleets="$(rr_list_fleets | sort | tr '\n' ',')"
assert_eq "rr_list_fleets finds flat + nested fleets" "flatfleet,nestedfleet," "$_fleets"

# CLI guards (subprocess so an exit doesn't kill the test shell).
run_rc() { CLAUDLOBBY_ROOT="$T" bash "$LIB_DIR/rolling-restart.sh" "$@" >/dev/null 2>&1; echo $?; }
assert_eq "no fleet and no --all → usage error (2)"   "2" "$(run_rc)"
assert_eq "unknown option → error (2)"                "2" "$(run_rc --bogus)"
assert_eq "non-integer --ceiling → error (2)"         "2" "$(run_rc flatfleet --ceiling abc)"
assert_eq "--workers-only + --managers-only → error (2)" "2" \
    "$(run_rc flatfleet --workers-only --managers-only)"

echo ""
echo "=== rr_bot_ceiling — the driver ceiling follows the composed one ==="
# The driver's fixed 180 was SHORTER than the launcher's own composed
# readiness ceiling (RC_READY_TIMEOUT_S, 200 at this tip), and start-bot.sh
# writes BRIDGE_READY only AFTER that poll — so a healthy bot whose poller
# came up in the 180..200 band halted the whole roll with a
# rolling_restart_stalled FLEET ALERT. The budget is derived per bot from
# bot.conf (F4: bot.conf is the carrier) plus the margin for
# pre-stop-handoff + spin-up + the poller settle.
CEIL_BOT="$T/ceiling-bot"; mkdir -p "$CEIL_BOT"
printf 'BOT_ID=ceil\nRC_READY_TIMEOUT_S=200\n' > "$CEIL_BOT/bot.conf"
assert_eq "composed RC_READY_TIMEOUT_S=200 → 320s budget (200 + 120 margin)" \
    "320" "$(rr_bot_ceiling "$CEIL_BOT")"

# A raise of host.boot.mcp_timeout_ms moves RC_READY_TIMEOUT_S, and the
# driver must move with it rather than needing its own edit.
printf 'BOT_ID=ceil\nRC_READY_TIMEOUT_S=320\n' > "$CEIL_BOT/bot.conf"
assert_eq "a raised composed ceiling moves the driver too (320 → 440)" \
    "440" "$(rr_bot_ceiling "$CEIL_BOT")"

# No composed key (a bot.conf predating the boot policy): the floor is the
# same 90s default start-bot.sh itself falls back to, so the two never
# disagree about what the launcher will wait.
CEIL_BOT_OLD="$T/ceiling-bot-old"; mkdir -p "$CEIL_BOT_OLD"
printf 'BOT_ID=old\n' > "$CEIL_BOT_OLD/bot.conf"
assert_eq "no composed RC_READY_TIMEOUT_S → 210s budget (90 default + 120)" \
    "210" "$(rr_bot_ceiling "$CEIL_BOT_OLD")"

# A non-numeric value must not abort the roll under set -e — it falls back to
# the same 90 default rather than into arithmetic.
printf 'BOT_ID=junk\nRC_READY_TIMEOUT_S=later\n' > "$CEIL_BOT_OLD/bot.conf"
assert_eq "non-numeric composed value → the 90 default, no arithmetic error" \
    "210" "$(rr_bot_ceiling "$CEIL_BOT_OLD")"

# A ZERO-PADDED value is ALL DIGITS, so the guard above hands it straight to
# the arithmetic — where a bare $(( 090 )) is read as OCTAL ("value too great
# for base", rc 1, empty stdout). rr_process_fleet runs under a caller that
# suspends errexit, so that would be silent: an empty ceiling, the gate back
# on wait_bridge_ready's own 180, and an alert reading "within s". 10# forces
# base 10.
printf 'BOT_ID=padded\nRC_READY_TIMEOUT_S=090\n' > "$CEIL_BOT_OLD/bot.conf"
assert_eq "zero-padded composed value reads as decimal, not octal (090 → 210)" \
    "210" "$(rr_bot_ceiling "$CEIL_BOT_OLD")"

# --ceiling still wins for every bot in the run: an operator who names a
# number means it.
_SAVED_CEILING="$CEILING"; _SAVED_CEILING_SET="$CEILING_SET"
CEILING=45; CEILING_SET=1
assert_eq "--ceiling overrides the derivation (45 beats a composed 320)" \
    "45" "$(rr_bot_ceiling "$CEIL_BOT")"
CEILING="$_SAVED_CEILING"; CEILING_SET="$_SAVED_CEILING_SET"

echo ""
echo "=== rolling-restart.sh --managers-only — skips workers, restarts the manager ==="

# A fleet of one manager (zzz-manager) and two workers, named so glob order
# ("$bots_dir"/*/, alphabetical) processes the workers FIRST — proving both
# are logged as skipped before the manager is ever reached. Hermetic per the
# suite contract: pre-stop-handoff.sh and spin-up-bot.sh are stubbed onto a
# private LIB_DIR override, so this never touches a real tmux session,
# systemd unit, or launchd job.
MGR_BOTS_DIR="$T/local/mgrfleet/runtime/bots"
mkdir -p "$MGR_BOTS_DIR/aaa-worker-1" "$MGR_BOTS_DIR/bbb-worker-2" "$MGR_BOTS_DIR/zzz-manager"
cat > "$T/local/mgrfleet/fleet.yaml" <<'YAML'
fleet:
  name: mgrfleet
  bots:
    aaa-worker-1:
      expertise: [eng]
    bbb-worker-2:
      expertise: [eng]
    zzz-manager:
      expertise: [orchestration]
YAML
printf 'BOT_ID=aaa-worker-1\nMANAGER_TMUX=zzz-manager\n' > "$MGR_BOTS_DIR/aaa-worker-1/bot.conf"
printf 'BOT_ID=bbb-worker-2\nMANAGER_TMUX=zzz-manager\n' > "$MGR_BOTS_DIR/bbb-worker-2/bot.conf"
printf 'BOT_ID=zzz-manager\nMANAGER_TMUX=zzz-manager\n' > "$MGR_BOTS_DIR/zzz-manager/bot.conf"

STUB_LIB="$T/stub-lib"
mkdir -p "$STUB_LIB"
cat > "$STUB_LIB/pre-stop-handoff.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat > "$STUB_LIB/spin-up-bot.sh" <<'SH'
#!/usr/bin/env bash
# Hermetic double: a real spin-up would talk to tmux/systemd/launchd. This
# only satisfies the fence-then-BRIDGE_READY contract wait_bridge_ready reads.
bot_dir="$1"
mkdir -p "$bot_dir/logs"
printf "STUB BRIDGE_READY\n" >> "$bot_dir/logs/startup.log"
exit 0
SH
chmod +x "$STUB_LIB/pre-stop-handoff.sh" "$STUB_LIB/spin-up-bot.sh"

# rr_process_fleet and its lib-common dependents are already in this shell
# from the source above; drive it directly rather than through rr_main so no
# real tmux/systemd/launchd call is ever in reach. LIB_DIR is restored right
# after so the weekly-worker-restart.sh check below still reads the real lib/.
REAL_LIB_DIR="$LIB_DIR"
LOG="$T/rolling-restart-managers-only.log"
RESTARTED=0; SKIPPED=0; FAILED=0
# CEILING_SET=1 keeps the 0s budget an OPERATOR override here: without it the
# per-bot derivation would hand this hermetic run a 210s wait if the stub ever
# failed to write its BRIDGE_READY.
WORKERS_ONLY=0; MANAGERS_ONLY=1; SKIP_HEALTHY=0; CONTINUE_ON_FAIL=0; CEILING=0; CEILING_SET=1
LIB_DIR="$STUB_LIB"
rr_process_fleet "mgrfleet" || true
LIB_DIR="$REAL_LIB_DIR"

assert_eq "--managers-only skips worker aaa-worker-1" "true" \
    "$(grep -q "SKIP (worker): aaa-worker-1" "$LOG" && echo true || echo false)"
assert_eq "--managers-only skips worker bbb-worker-2" "true" \
    "$(grep -q "SKIP (worker): bbb-worker-2" "$LOG" && echo true || echo false)"
assert_eq "--managers-only restarts the manager zzz-manager" "true" \
    "$(grep -q "READY: zzz-manager" "$LOG" && echo true || echo false)"
assert_eq "--managers-only never skips the manager" "false" \
    "$(grep -q "SKIP (worker): zzz-manager" "$LOG" && echo true || echo false)"
assert_eq "--managers-only skip count is 2 (both workers)" "2" "$SKIPPED"
assert_eq "--managers-only restarted count is 1 (the manager)" "1" "$RESTARTED"

echo ""
echo "=== #1358: a stalled gate must NAME the auth-cache signature, not blame a slow bridge ==="
# A gate that stalls on the host-global MCP auth cache used to report only its
# own ceiling, while the advice an operator actually reads here came from
# start-bot via spin-up-bot (redirected into this same log): "keepalive owns
# heal". For this one cause that is the remedy that provably cannot work --
# keepalive restarts, the restart re-reads the same cache, the poller is skipped
# again. A fleet-wide rolling restart stalled on exactly this on 2026-09-19.
#
# Hermetic on the same suite contract as the section above: a STUB spin-up that
# deliberately writes no BRIDGE_READY, so the gate fails by construction rather
# than by timing. CLAUDE_CONFIG_DIR is pinned per-bot, which is also the branch
# this caller needs -- rolling-restart runs at fleet level without the bot env,
# so it must resolve the cache the BOT consults, not the operator one.
AC_BOTS_DIR="$T/local/acfleet/runtime/bots"
AC_CFG="$T/acfleet-cfg"
mkdir -p "$AC_BOTS_DIR/acbot" "$AC_CFG"
cat > "$T/local/acfleet/fleet.yaml" <<'YAML'
fleet:
  name: acfleet
  bots:
    acbot:
      expertise: [eng]
YAML
printf 'BOT_ID=acbot\nMANAGER_TMUX=acmgr\nCLAUDE_CONFIG_DIR="%s"\n' "$AC_CFG" > "$AC_BOTS_DIR/acbot/bot.conf"

AC_STUB="$T/stub-lib-ac"; mkdir -p "$AC_STUB"
printf '#!/usr/bin/env bash\nexit 0\n' > "$AC_STUB/pre-stop-handoff.sh"
# Writes NO BRIDGE_READY: the gate must time out. That is the whole scenario.
printf '#!/usr/bin/env bash\nexit 0\n' > "$AC_STUB/spin-up-bot.sh"
chmod +x "$AC_STUB/pre-stop-handoff.sh" "$AC_STUB/spin-up-bot.sh"

# rr_fail raises a real fleet alert. CLAUDLOBBY_ROOT is the throwaway $T, so the
# tg-post.sh it reaches for does not exist and no send is possible; the fake
# escalation id is belt-and-braces and states the intent (#846 convention).
export FLEET_PULSE_ESCALATION_CHAT_ID="-100999"

ac_run() {   # $1 = cache content written to the bot CLAUDE_CONFIG_DIR
    printf '%s' "$1" > "$AC_CFG/mcp-needs-auth-cache.json"
    LOG="$2"
    RESTARTED=0; SKIPPED=0; FAILED=0
    WORKERS_ONLY=0; MANAGERS_ONLY=0; SKIP_HEALTHY=0; CONTINUE_ON_FAIL=1; CEILING=0
    LIB_DIR="$AC_STUB"
    rr_process_fleet "acfleet" || true
    LIB_DIR="$REAL_LIB_DIR"
}

# NEGATIVE CONTROL FIRST, and it has to be first: the absence asserted here is
# only worth something once the armed run below proves the line can appear at
# all. An assertion on a silence never shown to be breakable passes forever.
ac_run '{}' "$T/rr-authcache-clean.log"
assert_eq "(clean cache) the gate still fails on its ceiling" "true" \
    "$(grep -q 'FAILED: acbot — no BRIDGE_READY within' "$T/rr-authcache-clean.log" && echo true || echo false)"
assert_eq "(clean cache) no AUTH_CACHE_ARMED line" "false" \
    "$(grep -q 'AUTH_CACHE_ARMED' "$T/rr-authcache-clean.log" && echo true || echo false)"
assert_eq "(clean cache) the failure does not claim the cache is armed" "false" \
    "$(grep -q 'auth cache is ARMED' "$T/rr-authcache-clean.log" && echo true || echo false)"

# ARMED: a real recorded entry, copied byte-for-byte from a live armed host
# cache (2026-09-20T09:49:01-04:00), so the parse meets the shape the defect
# actually produces rather than one invented to suit it.
ac_run '{"plugin:telegram:telegram":{"timestamp":1789912141541,"id":"3eaf116ce58465c5"}}' \
    "$T/rr-authcache-armed.log"
assert_eq "(armed cache) AUTH_CACHE_ARMED recorded in the rolling-restart log" "true" \
    "$(grep -q 'AUTH_CACHE_ARMED' "$T/rr-authcache-armed.log" && echo true || echo false)"
assert_eq "(armed cache) the line names the skipped mcp server" "true" \
    "$(grep -q 'plugin:telegram:telegram' "$T/rr-authcache-armed.log" && echo true || echo false)"
assert_eq "(armed cache) it read the BOT config dir, not the operator one" "true" \
    "$(grep -q "Cache: $AC_CFG/" "$T/rr-authcache-armed.log" && echo true || echo false)"
assert_eq "(armed cache) the FAILED line names the signature" "true" \
    "$(grep -q 'FAILED: acbot .* auth cache is ARMED' "$T/rr-authcache-armed.log" && echo true || echo false)"
assert_eq "(armed cache) the FAILED line strikes the keepalive remedy" "true" \
    "$(grep -q 'keepalive cannot heal this' "$T/rr-authcache-armed.log" && echo true || echo false)"
assert_eq "(armed cache) the FAILED line still carries the ceiling it waited" "true" \
    "$(grep -q 'FAILED: acbot — no BRIDGE_READY within' "$T/rr-authcache-armed.log" && echo true || echo false)"

# UNDETERMINED: the third state. Malformed JSON is the realistic trigger -- the
# cache is host-global and written by Claude Code at arbitrary moments, so a read
# concurrent with a write lands here. The alert must neither claim ARMED nor go
# quiet: a cache that could not be read has not ruled anything out.
ac_run 'not json {{{' "$T/rr-authcache-unknown.log"
assert_eq "(unreadable cache) AUTH_CACHE_UNKNOWN recorded, not silence" "true" \
    "$(grep -q 'AUTH_CACHE_UNKNOWN' "$T/rr-authcache-unknown.log" && echo true || echo false)"
assert_eq "(unreadable cache) it does NOT claim the cache is armed" "false" \
    "$(grep -q 'auth cache is ARMED' "$T/rr-authcache-unknown.log" && echo true || echo false)"
assert_eq "(unreadable cache) the FAILED line says an armed cache is not ruled out" "true" \
    "$(grep -q 'FAILED: acbot .* could NOT be read' "$T/rr-authcache-unknown.log" && echo true || echo false)"
assert_eq "(unreadable cache) the gate still fails on its ceiling" "true" \
    "$(grep -q 'FAILED: acbot — no BRIDGE_READY within' "$T/rr-authcache-unknown.log" && echo true || echo false)"

echo ""
echo "=== weekly-worker-restart.sh rides the shared gate ==="
assert_eq "weekly restart calls wait_bridge_ready" "true" \
    "$(grep -q 'wait_bridge_ready' "$LIB_DIR/weekly-worker-restart.sh" && echo true || echo false)"

# The same ceiling coupling as rr_bot_ceiling above, pinned by READING the
# file rather than by driving it: weekly-worker-restart.sh is a top-level
# script with no source-guard, so a suite cannot source it without running a
# real fleet bounce, and its LIB_DIR is self-derived (no seam to point at
# stubs). A text pin is the honest instrument here, and it is narrow: it says
# the derivation is present and the old fixed default is gone, nothing more.
assert_eq "weekly restart derives its ceiling from the composed RC_READY_TIMEOUT_S" "true" \
    "$(grep -q 'bot_conf_get "\$bot_dir" RC_READY_TIMEOUT_S' "$LIB_DIR/weekly-worker-restart.sh" && echo true || echo false)"
assert_eq "weekly restart no longer carries a fixed 180s gate default" "false" \
    "$(grep -q 'WEEKLY_RESTART_CEILING:-180' "$LIB_DIR/weekly-worker-restart.sh" && echo true || echo false)"
assert_eq "WEEKLY_RESTART_CEILING remains the operator override" "true" \
    "$(grep -q 'WEEKLY_RESTART_CEILING:-' "$LIB_DIR/weekly-worker-restart.sh" && echo true || echo false)"
# The zero-padded case is worse on this side than on rolling-restart's, and it
# is why the text pin extends to it: bash discards the enclosing command on an
# expansion error, so one octal-looking value skips the rest of the per-bot
# LOOP and the script still exits 0 under a "RESTART complete" line.
assert_eq "weekly restart forces base 10 on the composed value (10#)" "true" \
    "$(grep -q '10#\$_wr_rc_s' "$LIB_DIR/weekly-worker-restart.sh" && echo true || echo false)"

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
