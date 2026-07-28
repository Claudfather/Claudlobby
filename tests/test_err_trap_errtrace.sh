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
# emit_script_error reaches only emit_fleet_event (a file append) — no tmux, no
# network, no Telegram path. Runs under macOS /bin/bash (3.2).
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
mkdir -p "$BOTDIR/data/events" "$T/state/events"

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
    rm -f "$BOTDIR/data/events/"*.jsonl 2>/dev/null || true
    out=$(
        env -i PATH="$PATH" HOME="$T" \
            CLAUDLOBBY_ROOT="$T" BOT_DIR="$BOTDIR" BOT_ID=canary \
            bash -c "
                set $opts
                . '$LIB_COMMON'
                install_error_trap '$BOTDIR'
                boom() { /nonexistent-command-xyz-844; }
                $body
            " 2>/dev/null
    )
    rows=$(cat "$BOTDIR/data/events/"*.jsonl 2>/dev/null | wc -l | tr -d ' ')
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
assert_eq "…and stays silent when the caller tolerates it" "0" "$(rows_of "$r")"

echo
echo "  $PASS/$TOTAL passed"
[ "$FAIL" -eq 0 ] || { echo "  $FAIL FAILED"; exit 1; }
