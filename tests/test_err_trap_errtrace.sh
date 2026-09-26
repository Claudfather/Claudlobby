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
#   * direct suppressed contexts (`f || true`, `if f`) must stay SILENT; a
#     command substitution is a version-dependent boundary, so portable silence
#     requires guarding the expected failure INSIDE the substitution
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
# Use the interpreter running this suite for every case. Selecting expectations
# from this shell but resolving another bash through PATH would certify a lie.
CASE_BASH="$BASH"
CASE_BASH_MINOR="${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"
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
run_case() {
    local opts="$1" body="$2" out rows
    : > "$CAPTURE"
    out=$(
        env -i PATH="$PATH" HOME="$T" \
            CLAUDLOBBY_ROOT="$T" BOT_DIR="$BOTDIR" BOT_ID=canary FLEET_NAME=f \
            PLANE_EMIT_DISABLED=0 PLANE_EMIT_CLI="$SCRIPT_DIR/plane_capture_cli.sh" PLANE_CAPTURE="$CAPTURE" PLANE_SOCKET="$T/no.sock" \
            "$CASE_BASH" -c "
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
echo "case interpreter: $CASE_BASH ($BASH_VERSION)"

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
# Direct guards suppress the ERR trap, including through nested function calls.
# These cases pin that contract on both supported CI interpreters. Command
# substitutions form a separate boundary, characterized below; an outer guard
# alone does not promise their silence on native macOS Bash 3.2.
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
# Preserve the raw outer-guard shape as an interpreter characterization. Native
# macOS Bash 3.2.57 still reports twice here; hosted Linux Bash 5.2.21 suppresses
# both reports. Only 3.2 gets that measured exception; other versions retain
# the prior zero-receipt expectation, not a claimed measured version threshold.
# A different result still fails rather than accepting either count.
r=$(run_case "-euo pipefail" 'f() { echo "$(boom)"; }; f || true')
case "$CASE_BASH_MINOR" in
    3.2) outer_guard_rows=2 ;;
    *) outer_guard_rows=0 ;;
esac
assert_eq "outer-guard substitution behavior on Bash $CASE_BASH_MINOR" "$outer_guard_rows" "$(rows_of "$r")"

# Portable deliberate tolerance puts the guard INSIDE the substitution. Keep
# the outer function call unguarded so removing the inner guard cannot be hidden
# by modern Bash propagating an outer OR-list into the substitution.
r=$(run_case "-euo pipefail" 'f() { printf "[%s]" "$(boom || true)"; }; f; echo SURVIVED')
assert_eq "an inner guard silences the expected substitution failure" "0" "$(rows_of "$r")"
assert_eq "the inner guard preserves the value and caller continuation" "[]SURVIVED" "$(out_of "$r")"

echo
echo "  $PASS/$TOTAL passed"
[ "$FAIL" -eq 0 ] || { echo "  $FAIL FAILED"; exit 1; }
