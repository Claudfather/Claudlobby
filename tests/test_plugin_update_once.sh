#!/usr/bin/env bash
# tests/test_plugin_update_once.sh — plugin_ensure (lib-common.sh) runs
# `claude plugin update` at most once per host boot PER PLUGIN when
# BOOT_PLUGIN_UPDATE_ONCE=1, keeps installing a missing plugin on every call
# regardless of that flag, and falls back to updating every call -- saying
# so in the log -- when the once-gate itself cannot be trusted: the boot
# epoch unresolvable, or the stamp directory unwritable (#1573 boot
# admission, task 4).
#
# Before this, start-bot.sh ran `claude plugin update` for every required
# plugin on every bot start -- on an 18-bot host a cold boot fires it 18
# times at once, alongside the MCP startups of the sessions themselves.
#
# Standalone bash (not pytest-collected on its own); discovered by
# tests/test_sh_suites.py, which runs it hermetically (constructed env: PATH
# + LANG only, plus a throwaway HOME/TMPDIR -- see tests/conftest.py
# constructed_env). Runs under macOS /bin/bash (3.2) too.
#
# Hermetic: a fake `claude` on a private, prepended PATH records its argv to
# a log file and exits with whatever code a small state file names -- no
# real claude binary, network, or plugin state is ever touched. HOME is
# redirected to a temp dir so the installed_plugins.json check never reads
# the real ~/.claude/plugins/, and CLAUDLOBBY_ROOT is redirected so the
# stamp/lock files never touch a real state/ directory.
#
# Every call below is BARE -- never `plugin_ensure ... || true` -- on
# purpose: the function must be errexit-safe on its own, not only when its
# caller happens to wrap it (start-bot.sh does; nothing guarantees the next
# caller will). The first cut of the function aborted this very script on
# the unresolvable-epoch case, because `epoch="$(door)"` with a door that
# returns 1 is itself a failing statement under set -e.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
PASS=0; FAIL=0; TOTAL=0
assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS + 1)); else echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1)); fi
}

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export CLAUDLOBBY_ROOT="$T/root"
export HOME="$T/home"
mkdir -p "$CLAUDLOBBY_ROOT" "$HOME/.claude/plugins" "$T/bin"

# shellcheck source=../lib/lib-common.sh
. "$LIB_DIR/lib-common.sh"

# A fake `claude` on a prepended PATH -- resolved by name, exactly as
# start-bot.sh resolves the real one ($CLAUDE, usually the bare "claude").
# Its argv is appended to CLAUDE_ARGV_LOG; its exit code is read fresh out of
# CLAUDE_EXIT_FILE on every invocation (the heredoc is unquoted so the two
# paths are baked in at creation time, while $* and the exit-file read stay
# escaped so they evaluate at run time, once per call).
export PATH="$T/bin:$PATH"
CLAUDE_ARGV_LOG="$T/claude-argv.log"
CLAUDE_EXIT_FILE="$T/claude-exit-code"
cat > "$T/bin/claude" <<EOF
#!/bin/bash
echo "\$*" >> "$CLAUDE_ARGV_LOG"
exit "\$(cat "$CLAUDE_EXIT_FILE" 2>/dev/null || echo 0)"
EOF
chmod +x "$T/bin/claude"

LOGFILE="$T/startup.log"
PLUGIN_REF="theplugin@trial"
PLUGIN_SANITIZED="theplugin_trial"

update_calls() { grep -c "plugin update" "$CLAUDE_ARGV_LOG" 2>/dev/null || true; }
install_calls() { grep -c "plugin install" "$CLAUDE_ARGV_LOG" 2>/dev/null || true; }
stamp_path() { printf '%s' "$CLAUDLOBBY_ROOT/state/boot/plugins-updated.$1.$PLUGIN_SANITIZED"; }
log_has() { grep -q "$1" "$LOGFILE" && echo true || echo false; }
exists() { [ -e "$1" ] && echo true || echo false; }

# Resets everything a scenario can dirty: the call log of the fake claude,
# the startup log, its exit code (back to success), any stamp/lock state
# from a prior scenario, and installed_plugins.json (present by default --
# the "already installed" state every case but #3 needs).
_reset_plugin_state() {
    : > "$CLAUDE_ARGV_LOG"
    : > "$LOGFILE"
    echo 0 > "$CLAUDE_EXIT_FILE"
    rm -rf "$CLAUDLOBBY_ROOT/state"
    printf '{"plugins":["%s"]}\n' "$PLUGIN_REF" > "$HOME/.claude/plugins/installed_plugins.json"
}

echo "=== plugin_ensure — once-per-host-boot update gate (#1573) ==="

# (1)+(2) Two calls in one boot epoch with once_flag=1: only the FIRST runs a
# real update; the second finds the stamp and skips. A different epoch is
# not gated by the stamp of the first -- its own update runs.
_reset_plugin_state
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
assert_eq "epoch A, call 1: one plugin update queued" "1" "$(update_calls)"
assert_eq "epoch A, call 1: stamp written for that epoch" "true" "$(exists "$(stamp_path 1000000000)")"
assert_eq "epoch A, call 1: the mkdir spinlock dir is released (a leak would stall every later caller)" "false" \
    "$(exists "$CLAUDLOBBY_ROOT/state/boot/plugins.lock.d")"
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
assert_eq "epoch A, call 2: still exactly one update (the second is gated)" "1" "$(update_calls)"
assert_eq "epoch A, call 2: no install ever ran" "0" "$(install_calls)"
assert_eq "epoch A, call 2: skip is logged" "true" "$(log_has "PLUGIN update skipped (already run this boot)")"
CLAUDLOBBY_BOOT_EPOCH=2000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
assert_eq "epoch B: a second, independent update runs" "2" "$(update_calls)"
assert_eq "epoch B: its own stamp is written" "true" "$(exists "$(stamp_path 2000000000)")"

# (3) Plugin absent from installed_plugins.json: install runs on every call,
# regardless of once_flag -- the flag only ever gates UPDATE.
_reset_plugin_state
printf '{"plugins":["someotherplugin@trial"]}\n' > "$HOME/.claude/plugins/installed_plugins.json"
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
assert_eq "plugin missing: install runs on every call" "2" "$(install_calls)"
assert_eq "plugin missing: never updates" "0" "$(update_calls)"
assert_eq "plugin missing: cold-start line logged" "true" "$(log_has "PLUGIN installing $PLUGIN_REF (cold start)")"

# (4) once_flag=0 (or absent, which the start-bot.sh call site coerces to
# "0"): update runs on every call -- the behaviour of today, unchanged.
_reset_plugin_state
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 0
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 0
assert_eq "once_flag=0: update runs on every call" "2" "$(update_calls)"
assert_eq "once_flag=0: no stamp is ever written" "false" "$(exists "$(stamp_path 1000000000)")"

# (6) A fake claude that exits 1: the failed update must not stamp the boot
# as done, so the very next call (same epoch) updates again.
_reset_plugin_state
echo 1 > "$CLAUDE_EXIT_FILE"
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
assert_eq "failed update: one update attempted" "1" "$(update_calls)"
assert_eq "failed update: no stamp written" "false" "$(exists "$(stamp_path 1000000000)")"
echo 0 > "$CLAUDE_EXIT_FILE"
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
assert_eq "after a failed update: the next call updates again" "2" "$(update_calls)"
assert_eq "after a failed update succeeds: the stamp now exists" "true" "$(exists "$(stamp_path 1000000000)")"

# (7) The stamp directory cannot be created -- a FILE sits where state/ must
# be a directory, which fails `mkdir -p` under any privilege (a chmod-based
# read-only dir passes for root, and a CI runner may be root). The gate is
# untrustworthy, so the update runs on every call, the log says why, and
# both calls return promptly: without this branch a flock-less host would
# spin the whole with_lock budget (30s) per plugin per start, and a host
# with flock would fail the locked section before the update ran -- no
# update at all, and nothing logged.
_reset_plugin_state
: > "$CLAUDLOBBY_ROOT/state"
_t0=$(date +%s)
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
_t1=$(date +%s)
assert_eq "state dir unwritable: update runs on every call" "2" "$(update_calls)"
assert_eq "state dir unwritable: the unavailable line names it" "true" \
    "$(log_has "PLUGIN update-once unavailable (state dir unwritable")"
assert_eq "state dir unwritable: both calls return promptly (no lock spin)" "true" \
    "$([ "$((_t1 - _t0))" -lt 10 ] && echo true || echo false)"

# (8) with_lock takes the flock path where flock exists (Linux) and runs the
# locked section in a forked SUBSHELL there -- `( flock -x 200; "$@" )
# 200>lock` -- while stock macOS has no flock and runs it in-process behind
# an mkdir spinlock. The nested locked function reads plugin_ensure locals
# by dynamic scope and reports a failed update through its return code, and
# both must survive the fork. A stub flock that just exits 0 forces the
# subshell path on any host (it does no locking -- the fork plumbing is what
# is under test); _FLOCK_BIN is a plain variable resolved once at source
# time, so it is pointed at the stub here and restored afterwards.
printf '#!/bin/bash\nexit 0\n' > "$T/bin/flock"; chmod +x "$T/bin/flock"
_saved_flock="$_FLOCK_BIN"
_FLOCK_BIN="$T/bin/flock"
_reset_plugin_state
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
assert_eq "flock path: exactly one update across two calls" "1" "$(update_calls)"
assert_eq "flock path: the stamp touched inside the subshell is visible" "true" "$(exists "$(stamp_path 1000000000)")"
assert_eq "flock path: skip is logged" "true" "$(log_has "PLUGIN update skipped (already run this boot)")"
_reset_plugin_state
echo 1 > "$CLAUDE_EXIT_FILE"
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
assert_eq "flock path: a failed update leaves no stamp" "false" "$(exists "$(stamp_path 1000000000)")"
echo 0 > "$CLAUDE_EXIT_FILE"
CLAUDLOBBY_BOOT_EPOCH=1000000000 plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
assert_eq "flock path: the next call updates again" "2" "$(update_calls)"
_FLOCK_BIN="$_saved_flock"

# (5) CLAUDLOBBY_BOOT_EPOCH left empty and resolve_boot_epoch itself stubbed
# to fail (the unresolvable-clock case -- no uptime/sysctl/proc answer
# either): update runs on every call and the unavailable line is logged.
# Runs LAST: overriding resolve_boot_epoch in-process has no clean way back
# to the real one (unset -f drops it entirely, it does not restore the
# sourced definition), so every scenario needing the real door must run
# before this. The call stays bare, which is the errexit pin: the door
# returning 1 must never abort the caller.
_reset_plugin_state
resolve_boot_epoch() { return 1; }
CLAUDLOBBY_BOOT_EPOCH= plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
CLAUDLOBBY_BOOT_EPOCH= plugin_ensure "$PLUGIN_REF" claude "$LOGFILE" 1
assert_eq "epoch unresolvable: update runs on every call" "2" "$(update_calls)"
assert_eq "epoch unresolvable: the unavailable line is logged" "true" \
    "$(log_has "PLUGIN update-once unavailable (boot epoch unresolvable)")"
assert_eq "epoch unresolvable: no stamp directory is even created" "false" "$(exists "$CLAUDLOBBY_ROOT/state/boot")"

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
