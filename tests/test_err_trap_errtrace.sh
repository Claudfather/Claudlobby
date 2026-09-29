#!/usr/bin/env bash
# tests/test_err_trap_errtrace.sh — install_error_trap must instrument failures
# that happen INSIDE shell functions (#844).
#
# A bash ERR trap is not inherited by shell functions unless errtrace is on, so
# a bare `trap … ERR` covers only top-level failures. lib/ does nearly all its
# work in functions, which meant the fleet's error-breadcrumb mechanism was
# silently uninstrumented across the whole supervision surface. Ordinary unit
# tests cannot catch this — composition is identical either way; only running a
# real failure through the real trap shows it. So this suite fault-injects
# against the REAL install_error_trap / emit_script_error and counts rows.
#
# It also pins the two properties that make errtrace safe to arm, because both
# are the kind of premise that silently stops being true:
#   * suppressed contexts (`f || true`, `if f`) must stay SILENT — errtrace must
#     instrument real failures without emitting rows for deliberate tolerance
#   * the handler must write NOTHING to stdout — under errtrace the trap fires
#     inside the failing command substitution, so any handler stdout is captured
#     as the caller's value (`local v=$(fn)` silently becomes the handler's
#     output). Latent today; one stray echo away from corrupting values fleet-wide
#
# Hermetic: every case runs under `env -i` with a scratch CLAUDLOBBY_ROOT and a
# scratch bot dir, so rows land in a throwaway ledger and never in a real one.
# emit_script_error reaches only emit_fleet_event, whose record is the plane —
# captured here by tests/plane_capture_cli.sh standing in for the CLI rung (no
# daemon, no db, no tmux, no network). Runs under macOS /bin/bash (3.2).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_COMMON="$SCRIPT_DIR/../lib/lib-common.sh"
PASS=0; FAIL=0; TOTAL=0

assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then
        echo "  PASS: $d"; PASS=$((PASS + 1))
    else
        echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1))
    fi
}

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
BOTDIR="$T/bots/canary"
mkdir -p "$BOTDIR/data"
CAPTURE="$T/plane-capture.jsonl"; : > "$CAPTURE"

# Run <body> in a pristine shell with the real trap installed, then report both
# the body's stdout and how many script_error rows it produced, so a case can
# assert on a captured value as well as on row count. Two values are packed into
# one string rather than returned via globals: every call site is `r=$(run_case
# …)`, and globals would let an interleaved call clobber an unread result with no
# error. bash 3.2 has no namerefs, so packing is the portable option.
#   run_case <set-opts> <body>  ->  "<captured-stdout>|<row-count>"
# `|` is safe as the delimiter only because no body here prints one; the
# `tr -d '\n'` guards a multi-line stdout no current body produces. Both are
# assumptions rather than guarantees — a body that prints `|` needs a new
# delimiter, not a workaround.
# Each case runs under "$BASH", the interpreter running this suite, never a
# `bash` looked up on PATH, which can be a newer bash than the one under test:
# `/bin/bash tests/test_err_trap_errtrace.sh` measures bash 3.2 all the way down.
run_case() {
    local opts="$1" body="$2" out rows
    : > "$CAPTURE"
    out=$(
        env -i PATH="$PATH" HOME="$T" \
            CLAUDLOBBY_ROOT="$T" BOT_DIR="$BOTDIR" BOT_ID=canary FLEET_NAME=f \
            PLANE_EMIT_CLI="$SCRIPT_DIR/plane_capture_cli.sh" PLANE_CAPTURE="$CAPTURE" PLANE_SOCKET="$T/no.sock" \
            "$BASH" -c "
                set $opts
                . '$LIB_COMMON'
                install_error_trap '$BOTDIR'
                boom() { /nonexistent-command-xyz-844; }
                $body
            " 2>/dev/null
    )
    rows=$(grep -c '"type":"script_error"' "$CAPTURE" 2>/dev/null || true)
    printf '%s|%s' "$(printf '%s' "$out" | tr -d '\n')" "$rows"
}

rows_of() { printf '%s' "${1#*|}"; }
out_of()  { printf '%s' "${1%%|*}"; }

echo "install_error_trap — in-function failure instrumentation (#844)"

# --- the regression itself -----------------------------------------------------
# Fails on a bare `trap … ERR`: the trap is not inherited into boom(), so the
# script dies at 127 having written nothing at all.
r=$(run_case "-euo pipefail" 'boom')
assert_eq "in-function failure emits a script_error row" "1" "$(rows_of "$r")"

# Control: the same failure at top level was always instrumented. Proves the
# fixture can see rows at all, so a green above is never a false green.
r=$(run_case "-euo pipefail" '/nonexistent-command-xyz-844')
assert_eq "top-level failure still emits (control)" "1" "$(rows_of "$r")"

# Nested three frames down — functions calling functions is the shape lib/ is
# actually built out of.
r=$(run_case "-euo pipefail" 'mid() { boom; }; outer() { mid; }; outer')
assert_eq "failure three frames deep emits exactly one row" "1" "$(rows_of "$r")"

# --- deliberate tolerance must stay silent -------------------------------------
# Arming errtrace must not turn `lib/`'s 700+ guarded call sites into rows. Bash
# suppresses the ERR trap in the same contexts it suppresses errexit, and that
# suppression is inherited by callees — these pin that, since the fix is only
# safe while it holds.
r=$(run_case "-euo pipefail" 'boom || true; echo SURVIVED')
assert_eq "'f || true' emits nothing" "0" "$(rows_of "$r")"
assert_eq "'f || true' still runs on" "SURVIVED" "$(out_of "$r")"

r=$(run_case "-euo pipefail" 'if boom; then :; fi; echo SURVIVED')
assert_eq "'if f; then' emits nothing" "0" "$(rows_of "$r")"
assert_eq "'if f; then' still runs on" "SURVIVED" "$(out_of "$r")"

r=$(run_case "-euo pipefail" 'outer() { boom; }; outer || true; echo SURVIVED')
assert_eq "suppression reaches a nested callee" "0" "$(rows_of "$r")"
assert_eq "…and the nested caller still runs on" "SURVIVED" "$(out_of "$r")"

# --- handler must not pollute stdout -------------------------------------------
# Under errtrace the trap fires INSIDE the failing command substitution, so any
# handler stdout is captured as the caller's value. `local v=$(…)` is the shape
# that survives to use the corrupted value (the declaration's own status is 0, so
# errexit never aborts). Value must stay empty AND the row must still be written —
# asserting only the value would pass a handler that emits nothing at all.
r=$(run_case "-euo pipefail" 'f() { local v=$(boom); printf "[%s]" "$v"; }; f')
assert_eq "handler stdout never reaches the caller's value" "[]" "$(out_of "$r")"
assert_eq "…and the row is still written" "2" "$(rows_of "$r")"

# The case above runs the shipped handler, which writes no stdout — so it passes
# with or without the redirect and proves nothing on its own (verified: deleting
# the redirect left every other case green). The redirect exists to bound a
# handler that DOES print, so the test has to supply one. Shadowing
# emit_script_error works because the trap body resolves the name at fire time.
r=$(run_case "-euo pipefail" \
    'emit_script_error() { echo HANDLER-NOISE; }
     f() { local v=$(boom); printf "[%s]" "$v"; }; f')
assert_eq "a noisy handler still cannot corrupt the value" "[]" "$(out_of "$r")"

# The redirect bounds the dangerous CONTEXT (a trap firing inside an arbitrary
# command substitution), but the property it leans on — emit_script_error writes
# nothing to stdout — belongs to the function, and notify-behind.sh already calls
# it directly with no redirect of its own. Pin the function's own contract so a
# debug echo added inside emit_script_error/emit_fleet_event is caught here,
# rather than downstream as a value that quietly became the handler's output.
# Paired with the row assertion: a function that did nothing at all would also
# print nothing.
r=$(run_case "-euo pipefail" 'v=$(emit_script_error "$BOT_DIR" direct 1 probe); printf "[%s]" "$v"')
assert_eq "emit_script_error itself writes no stdout" "[]" "$(out_of "$r")"
assert_eq "…while still writing its row" "1" "$(rows_of "$r")"

# --- command substitution emits per frame, by design ---------------------------
# A failing substitution reports TWICE: once at the true failure line (the trap
# firing inside the substitution's own subshell) and once at the line of the
# substitution itself in the parent, which errexit cannot abort on because the
# enclosing command's status is its own. Both rows are true and carry different
# line numbers, so this is characterised rather than deduped — dedup would cost
# handler state to lose the more precise of the two.
#
# Pinned because `echo "$(…)"` is a shape lib/ actually contains (~64 sites), so
# this is the change's real new-row source: silent today, two rows and a
# still-running script after. A future change that starts collapsing or dropping
# one of these should have to say so out loud.
r=$(run_case "-euo pipefail" 'f() { echo "$(boom)"; }; f')
assert_eq "failing substitution emits at both frames" "2" "$(rows_of "$r")"
r=$(run_case "-euo pipefail" 'f() { echo "$(boom)"; }; f || true')
case "${BASH_VERSINFO[0]}" in
    # bash 3.2 does not carry a caller's suppression into a substitution; the
    # next section is what that costs and what the boot path does about it.
    3) assert_eq "…and on bash 3.2 a tolerant caller does not reach into it" "2" "$(rows_of "$r")" ;;
    *) assert_eq "…and stays silent when the caller tolerates it" "0" "$(rows_of "$r")" ;;
esac

# --- the boot path on bash 3.2 (#1963) ----------------------------------------
# bash 3.2 is /bin/bash on macOS, and every bot there boots under it. On 3.2 a
# command substitution starts with no suppression: a failing command inside it
# reaches the trap however the statement around it is guarded, so
# `x="$(f)" || true` and `if x="$(f)"; then` both file a row, and so does a
# failing substitution inside a function whose caller tolerates it. bash 5.2
# carries the caller's suppression into the substitution, so none of this shows
# there. A macOS boot filed ~120 critical script_error rows this way, every one
# for a failure the code already handled. So the boot path settles an expected
# failure INSIDE its substitution: `x="$(f || true)"`, or `x="$(f || exit $?)"`
# where the status still decides.
#
# Run as `/bin/bash tests/test_err_trap_errtrace.sh` on macOS to measure 3.2
# (.github/workflows/macos-shell.yml does). Under bash 5 the controls below say
# they cannot run, and the cases after them can only fail on their values.
case "${BASH_VERSINFO[0]}" in
    3)
        # Two rows each: one for the command that fails inside boom, one for
        # boom's own status, both inside the substitution.
        r=$(run_case "-euo pipefail" 's="$(boom)" || true; echo SURVIVED')
        assert_eq "control: on bash 3.2 a guard outside the substitution does not reach in" "2" "$(rows_of "$r")"
        r=$(run_case "-euo pipefail" 'if v="$(boom)"; then :; fi; echo SURVIVED')
        assert_eq "control: nor does an if around the assignment" "2" "$(rows_of "$r")"
        ;;
    *)
        echo "  SKIP: the bash 3.2 controls: bash $BASH_VERSION carries the caller's suppression into a substitution"
        ;;
esac
r=$(run_case "-euo pipefail" 's="$(boom || true)"; echo SURVIVED')
assert_eq "an expected failure settled inside its substitution files nothing" "0" "$(rows_of "$r")"
r=$(run_case "-euo pipefail" 'if v="$(boom || exit $?)"; then echo YES; else echo "NO:$?"; fi')
assert_eq "…and '|| exit \$?' inside it keeps the status the if decides on" "NO:127" "$(out_of "$r")"
assert_eq "…silently" "0" "$(rows_of "$r")"

# The macOS host's shape: uptime refuses -s, and sysctl answers kern.boottime.
# The uptime stub records each call, so a case can see whether it was asked.
mkdir -p "$T/stub"
cat > "$T/stub/uptime" <<'STUB'
#!/bin/sh
printf x >> "$HOME/uptime.called"
echo "uptime: illegal option -- s" >&2
exit 1
STUB
cat > "$T/stub/sysctl" <<'STUB'
#!/bin/sh
[ "$1" = "-n" ] && [ "$2" = "kern.boottime" ] || exit 1
echo '{ sec = 1700000000, usec = 164678 } Tue Nov 14 22:13:20 2023'
STUB
chmod +x "$T/stub/uptime" "$T/stub/sysctl"

# plugin_ensure's call, the one that filed 58 rows on one macOS boot.
r=$(run_case "-euo pipefail" 'PATH="$HOME/stub:$PATH"; e="$(resolve_boot_epoch 2>/dev/null || true)"; printf "%s" "$e"')
assert_eq "resolve_boot_epoch answers from kern.boottime when uptime refuses -s" "1700000000" "$(out_of "$r")"
assert_eq "…and files no row" "0" "$(rows_of "$r")"
rm -f "$T/uptime.called"
r=$(run_case "-euo pipefail" 'PATH="$HOME/stub:$PATH"; _OS=Darwin; e="$(resolve_boot_epoch 2>/dev/null || true)"; if [ -e "$HOME/uptime.called" ]; then printf "asked uptime -s"; else printf "%s" "$e"; fi')
assert_eq "on Darwin resolve_boot_epoch never asks the GNU uptime -s" "1700000000" "$(out_of "$r")"

# should_resume_session, as start-bot.sh calls it, on a handoff with no
# last_updated field and on one whose field does not parse: both fall back to
# the file's mtime (#1568).
mkdir -p "$T/sess"
printf -- '---\ntitle: handoff\n---\nnext steps\n' > "$T/sess/no-field.md"
printf -- '---\nlast_updated: not-a-date\n---\nnext steps\n' > "$T/sess/bad-field.md"
r=$(run_case "-euo pipefail" 'if should_resume_session "$HOME/sess/no-field.md" 86400; then echo RESUME; else echo SKIP; fi')
assert_eq "a handoff with no last_updated resumes on its mtime" "RESUME" "$(out_of "$r")"
assert_eq "…and files no row" "0" "$(rows_of "$r")"
r=$(run_case "-euo pipefail" 'if should_resume_session "$HOME/sess/bad-field.md" 86400; then echo RESUME; else echo SKIP; fi')
assert_eq "a handoff whose last_updated does not parse resumes on its mtime" "RESUME" "$(out_of "$r")"
assert_eq "…and files no row" "0" "$(rows_of "$r")"

# bridge_state polled while bot.pid still names the previous boot's poller,
# which is dead: ps finding nothing is the answer, not an error (#1594).
mkdir -p "$T/bots/tg" "$T/tgstate"
printf 'export TELEGRAM_BOT_HANDLE=canary_bot\nexport TELEGRAM_STATE_DIR=%s\n' "$T/tgstate" > "$T/bots/tg/bot.conf"
r=$(run_case "-euo pipefail" 'sleep 0 & dead=$!; wait "$dead"; printf "%s" "$dead" > "$HOME/tgstate/bot.pid"; state="$(bridge_state "$HOME/bots/tg" tok "" 2>/dev/null || true)"; printf "%s" "$state"')
assert_eq "bridge_state on a stale bot.pid answers no_bridge" "no_bridge" "$(out_of "$r")"
assert_eq "…and files no row" "0" "$(rows_of "$r")"

# start-bot.sh's two `if x="$(f)"` sites are top-level lines of a script that
# boots a real session, so no case above can run them; their shape is pinned
# instead. So is the shape of every function fixed here, because under bash 5,
# which the Linux lanes run, the cases above cannot see the class at all.
sb_sites=$(grep -cE '^[[:space:]]*if [A-Za-z_]+="\$\(' "$SCRIPT_DIR/../lib/start-bot.sh" || true)
sb_settled=$(grep -E '^[[:space:]]*if [A-Za-z_]+="\$\(' "$SCRIPT_DIR/../lib/start-bot.sh" | grep -c '|| exit \$?)"; then' || true)
assert_eq "start-bot.sh has if x=\"\$(f)\" sites for this pin to hold" "yes" "$([ "$sb_sites" -gt 0 ] && echo yes || echo no)"
assert_eq "every one of them settles its status inside the substitution" "$sb_sites" "$sb_settled"
for fn in resolve_boot_epoch boot_epoch_from_sysctl session_md_handoff_epoch should_resume_session bridge_state; do
    code=$(awk -v f="$fn" '$0 == f "() {" {p = 1} p {print} p && /^}/ {exit}' "$LIB_COMMON" | grep -v '^[[:space:]]*#')
    assert_eq "$fn is in lib-common.sh for this pin to hold" "yes" "$([ -n "$code" ] && echo yes || echo no)"
    assert_eq "$fn guards no substitution from outside it" "0" "$(printf '%s\n' "$code" | grep -cE '\)"?[[:space:]]*\|\|' || true)"
    assert_eq "$fn has no if x=\"\$(f)\"" "0" "$(printf '%s\n' "$code" | grep -cE 'if [A-Za-z_]+="?\$\(' || true)"
done

echo
echo "  $PASS/$TOTAL passed"
[ "$FAIL" -eq 0 ] || { echo "  $FAIL FAILED"; exit 1; }
