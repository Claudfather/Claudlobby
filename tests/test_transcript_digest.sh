#!/usr/bin/env bash
# tests/test_transcript_digest.sh — transcript-digest SessionEnd hook contract.
# Real python3/awk + a stubbed model binary: asserts the two things that decide
# whether this is safe to run fleet-wide on every session — WHAT reaches the
# model (quota + secrets) and WHAT lands in the log (the monitor's substrate).
#
# The distinction this suite exists to protect: a `skipped` row (below the
# qualifying gate, written with ZERO model spend) and an `ok` row whose rubric
# fields are all empty (the model saying "nothing notable happened") are
# different signals. Collapsing them would either blow the quota or blind the
# monitor to idle bots.
#
# Fully hermetic: stubbed model, scratch CLAUDLOBBY_ROOT, no network, no real
# `claude` invocation, no fleet notices. Standalone bash (not pytest-collected);
# runs under macOS /bin/bash (3.2).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
DIGEST="$LIB_DIR/transcript-digest.sh"
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
mkdir -p "$T/bin" "$T/root"

# make_transcript <file> <pairs> [secret]
make_transcript() {
    TX="$1" N="$2" SECRET="${3:-}" python3 - <<'PY'
import json, os
n = int(os.environ["N"]); secret = os.environ.get("SECRET") or ""
rows = []
for i in range(n):
    rows.append({"type": "user", "message": {"content": "task %d %s" % (i, secret)}})
    rows.append({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "reply %d" % i},
        {"type": "tool_use", "name": "Bash"},
        {"type": "tool_result", "content": "X" * 4000},
    ]}})
rows.append({"type": "attachment", "message": {"content": "Y" * 8000}})
open(os.environ["TX"], "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
PY
}

# stub_model <body>
stub_model() {
    printf '#!/bin/bash\ncat > "%s/prompt-seen.txt"\nprintf "%%s" %s\n' "$T" "$1" > "$T/bin/claude"
    chmod +x "$T/bin/claude"
}

# run_digest <transcript> [env assignments...] -> the JSONL row
run_digest() {
    local tx="$1"; shift
    rm -rf "$T/out"; : > "$T/prompt-seen.txt"
    local pay
    pay="$(TX="$tx" python3 -c 'import json,os;print(json.dumps({"session_id":"sess-1","transcript_path":os.environ["TX"],"cwd":"/tmp","reason":"clear"}))')"
    printf '%s' "$pay" | env CLAUDLOBBY_ROOT="$T/root" BOT_ID=tbot CLAUDLOBBY_FLEET=tfleet \
        PATH="$T/bin:/usr/bin:/bin" CLAUDE_BIN=claude SESSION_DIGEST_LOG_DIR="$T/out" \
        "$@" bash "$DIGEST" >/dev/null 2>&1 || true
    cat "$T/out"/*.jsonl 2>/dev/null || true
}

# field <row> <key>
field() { ROW="$1" K="$2" python3 -c 'import json,os;print(json.loads(os.environ["ROW"]).get(os.environ["K"],""))' 2>/dev/null || true; }

echo "transcript-digest: hook contract"

make_transcript "$T/tx.jsonl" 4 "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

# --- 1. the ok path ----------------------------------------------------------
stub_model "'{\"context\":\"c\",\"worked\":\"w\",\"failed\":\"f\",\"would_change\":\"g\",\"reusable\":\"r\"}'"
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_MIN_TURNS=4)"
assert_eq "qualifying session emits status=ok" ok "$(field "$row" status)"
assert_eq "rubric field 'worked' lands in the row"  w "$(field "$row" worked)"
assert_eq "rubric field 'would_change' lands"       g "$(field "$row" would_change)"
assert_eq "turns counted (user+assistant, 4 pairs)" 8 "$(field "$row" turns)"
assert_eq "tool_use blocks counted"                 4 "$(field "$row" tool_calls)"
assert_eq "bot carried onto the row"            tbot "$(field "$row" bot)"
assert_eq "session_id carried from the payload" sess-1 "$(field "$row" session_id)"

# --- 2. tool_result + attachment must not reach the model --------------------
# They dominate transcript bytes and carry the least digest signal per token.
# Measured on a real transcript: dropping them is a 36x reduction.
seen="$(cat "$T/prompt-seen.txt" 2>/dev/null || true)"
case "$seen" in *XXXXXXXXXX*) r=yes ;; *) r=no ;; esac
assert_eq "tool_result payload is NOT sent to the model" no "$r"
case "$seen" in *YYYYYYYYYY*) r=yes ;; *) r=no ;; esac
assert_eq "attachment record is NOT sent to the model"   no "$r"
case "$seen" in *"[tool:Bash]"*) r=yes ;; *) r=no ;; esac
assert_eq "tool NAME is kept (signal without the payload)" yes "$r"

# --- 3. secrets are scrubbed BEFORE the model sees them ----------------------
# Stronger than asking a model not to echo what it was shown.
case "$seen" in *ghp_AAAA*) r=yes ;; *) r=no ;; esac
assert_eq "credential does NOT reach the model" no "$r"
case "$seen" in *REDACTED*) r=yes ;; *) r=no ;; esac
assert_eq "credential was replaced, not silently dropped" yes "$r"

# --- 4. the qualifying gate: a row, at zero model cost -----------------------
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_MIN_TURNS=99)"
assert_eq "below-gate session still emits a row" skipped "$(field "$row" status)"
assert_eq "below-gate row still carries turns"         8 "$(field "$row" turns)"
[ -s "$T/prompt-seen.txt" ] && r=yes || r=no
assert_eq "below-gate session spends NO model call"   no "$r"

# --- 5. the null row is distinct from the skipped row ------------------------
# A qualified session where the model found nothing notable. "N tool calls,
# nothing notable" is the idle-bot signal the monitor exists to catch, so it
# must be an `ok` row with empty fields, never a skip and never a dropped line.
stub_model "'{\"context\":\"routine triage\",\"worked\":\"\",\"failed\":\"\",\"would_change\":\"\",\"reusable\":\"\"}'"
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_MIN_TURNS=4)"
assert_eq "null row is status=ok, not skipped" ok "$(field "$row" status)"
assert_eq "null row keeps context"  "routine triage" "$(field "$row" context)"
assert_eq "null row has empty worked"           "" "$(field "$row" worked)"
assert_eq "null row still carries tool_calls"    4 "$(field "$row" tool_calls)"

# --- 6. tail-cap bounds what reaches the model -------------------------------
# Correctness, not just cost: a real transcript is ~5M tokens and cannot enter
# a 200K context at all.
make_transcript "$T/big.jsonl" 300
stub_model "'{\"context\":\"c\",\"worked\":\"\",\"failed\":\"\",\"would_change\":\"\",\"reusable\":\"\"}'"
row="$(run_digest "$T/big.jsonl" SESSION_DIGEST_MIN_TURNS=4 SESSION_DIGEST_TAIL_CHARS=5000)"
dc="$(field "$row" digest_chars)"
[ "$dc" -le 5100 ] && r=yes || r=no
assert_eq "tail-cap 5000 bounds digest_chars (<=5100, got $dc)" yes "$r"
sz="$(wc -c < "$T/prompt-seen.txt" | tr -d ' ')"
[ "$sz" -le 7000 ] && r=yes || r=no
assert_eq "prompt sent to the model stays bounded (<=7000, got $sz)" yes "$r"
row="$(run_digest "$T/big.jsonl" SESSION_DIGEST_MIN_TURNS=4 SESSION_DIGEST_TAIL_CHARS=40000)"
dc2="$(field "$row" digest_chars)"
[ "$dc2" -gt "$dc" ] && r=yes || r=no
assert_eq "a larger cap really sends more (cap is honored, not fixed)" yes "$r"

# --- 7. failure is loud in the log and silent to the session -----------------
# A SessionEnd hook that blocks or throws would break session teardown.
stub_model "'not json at all'"
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_MIN_TURNS=4)"
assert_eq "unparseable model output -> status=error" error "$(field "$row" status)"
[ -n "$(field "$row" error)" ] && r=yes || r=no
assert_eq "error row explains itself" yes "$r"

rm -f "$T/bin/claude"
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_MIN_TURNS=4 CLAUDE_BIN=no-such-binary)"
assert_eq "absent model binary -> status=error" error "$(field "$row" status)"

pay='{"session_id":"s","transcript_path":"/nonexistent/nope.jsonl","cwd":"/tmp"}'
rm -rf "$T/out"
printf '%s' "$pay" | env CLAUDLOBBY_ROOT="$T/root" BOT_ID=tbot PATH="$T/bin:/usr/bin:/bin" \
    SESSION_DIGEST_LOG_DIR="$T/out" bash "$DIGEST" >/dev/null 2>&1; rc=$?
assert_eq "missing transcript still exits 0 (never blocks session end)" 0 "$rc"

printf '%s' '' | env CLAUDLOBBY_ROOT="$T/root" BOT_ID=tbot PATH="$T/bin:/usr/bin:/bin" \
    SESSION_DIGEST_LOG_DIR="$T/out" bash "$DIGEST" >/dev/null 2>&1; rc=$?
assert_eq "empty payload still exits 0" 0 "$rc"

# --- 8. the kill switch ------------------------------------------------------
rm -rf "$T/out"
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_ENABLED=0)"
assert_eq "SESSION_DIGEST_ENABLED=0 writes nothing at all" "" "$row"

echo
echo "  $PASS/$TOTAL passed"
[ "$FAIL" -eq 0 ] || exit 1
