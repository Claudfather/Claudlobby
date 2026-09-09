#!/usr/bin/env bash
# tests/test_transcript_digest.sh — transcript-digest SessionEnd hook contract.
# Real python3/awk + a stubbed model binary + a stubbed plane CLI: asserts the
# two things that decide whether this is safe to run fleet-wide on every session
# — WHAT reaches the model (quota + secrets) and WHAT lands on the PLANE (the
# monitor's substrate).
#
# #1503 moved the SINK: the hook no longer appends a `transcript-digest-<date>`
# JSONL row (the last production JSONL data record outside the plane). It emits
# a `system` event (event=session_digest) on the bot's actor through the shim
# (lib/plane-emit.sh). This suite captures the emitted batch via the cold-rung
# stub (tests/plane_capture_cli.sh) driven through the real shim, and asserts on
# the event and its `data` object — never a file, which it also proves is gone.
#
# The distinction this suite exists to protect: a `skipped` fact (below the
# qualifying gate, emitted with ZERO model spend) and an `ok` fact whose rubric
# fields are all empty (the model saying "nothing notable happened") are
# different signals. Collapsing them would either blow the quota or blind the
# monitor to idle bots.
#
# Fully hermetic: stubbed model, stubbed plane CLI, dead socket, scratch
# CLAUDLOBBY_ROOT, no network, no real `claude`/plane, no fleet notices.
# Standalone bash (not pytest-collected); runs under macOS /bin/bash (3.2).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
DIGEST="$LIB_DIR/transcript-digest.sh"
CAPTURE_CLI="$SCRIPT_DIR/plane_capture_cli.sh"
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
mkdir -p "$T/bin" "$T/root/state/plane" "$T/botdir/data"

# A published session identity (the SessionStart hook's file) so the digest can
# carry the transcript-stable session_uid, as it does on a live bot. 32 hex
# chars is the sess_ shape plane-session-start.sh publishes.
SESS_UID="sess_$(python3 -c 'print("a"*32)')"
printf '{"session_uid":"%s","process_uid":"proc_x","derived_at":"now"}\n' \
    "$SESS_UID" > "$T/botdir/data/.plane-session"

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

# run_digest <transcript> [env assignments...] -> the captured plane event
# (one JSON line per emitted event; the hook emits one, so the last line is it).
# The shim's socket rung fails against a dead socket and falls back to the cold
# CLI, which is the capture stub — so this drives the REAL recording spine.
run_digest() {
    local tx="$1"; shift
    : > "$T/capture.jsonl"; : > "$T/err.txt"; rm -f "$T/prompt-seen.txt"
    local pay
    pay="$(TX="$tx" python3 -c 'import json,os;print(json.dumps({"session_id":"sess-1","transcript_path":os.environ["TX"],"cwd":"/tmp","reason":"clear"}))')"
    # ENABLED=1 first so a caller's explicit assignment in "$@" still wins (env
    # takes the last). The hook is dormant by default, so every behavioural case
    # below has to arm it — which is itself the point being asserted in §9.
    printf '%s' "$pay" | env CLAUDLOBBY_ROOT="$T/root" BOT_ID=tbot CLAUDLOBBY_FLEET=tfleet \
        BOT_DIR="$T/botdir" PATH="$T/bin:/usr/bin:/bin" CLAUDE_BIN=claude \
        SESSION_DIGEST_ENABLED=1 \
        PLANE_EMIT_CLI="bash $CAPTURE_CLI" PLANE_SOCKET="$T/root/state/plane/nope.sock" \
        PLANE_CAPTURE="$T/capture.jsonl" \
        "$@" bash "$DIGEST" >/dev/null 2>"$T/err.txt" || true
    tail -n 1 "$T/capture.jsonl" 2>/dev/null || true
}

# run_digest_unarmed <transcript> — no SESSION_DIGEST_ENABLED at all, i.e. what
# an un-opted-in fleet actually runs after generate composes the hook.
run_digest_unarmed() {
    : > "$T/capture.jsonl"; rm -f "$T/prompt-seen.txt"
    local pay
    pay="$(TX="$1" python3 -c 'import json,os;print(json.dumps({"session_id":"sess-1","transcript_path":os.environ["TX"],"cwd":"/tmp","reason":"clear"}))')"
    printf '%s' "$pay" | env CLAUDLOBBY_ROOT="$T/root" BOT_ID=tbot CLAUDLOBBY_FLEET=tfleet \
        BOT_DIR="$T/botdir" PATH="$T/bin:/usr/bin:/bin" CLAUDE_BIN=claude \
        PLANE_EMIT_CLI="bash $CAPTURE_CLI" PLANE_SOCKET="$T/root/state/plane/nope.sock" \
        PLANE_CAPTURE="$T/capture.jsonl" \
        bash "$DIGEST" >/dev/null 2>&1 || true
    cat "$T/capture.jsonl" 2>/dev/null || true
}

# field <event-json> <key>  — a TOP-LEVEL key on the captured event
field() { ROW="$1" K="$2" python3 -c 'import json,os;print(json.loads(os.environ["ROW"]).get(os.environ["K"],""))' 2>/dev/null || true; }
# dfield <event-json> <key> — a key inside the event's `data` object
dfield() { ROW="$1" K="$2" python3 -c 'import json,os;print((json.loads(os.environ["ROW"]).get("data") or {}).get(os.environ["K"],""))' 2>/dev/null || true; }
# no_jsonl_written — the whole point of #1503: NO transcript-digest file exists
no_jsonl_written() { [ -z "$(find "$T" -name 'transcript-digest-*.jsonl' 2>/dev/null)" ] && echo yes || echo no; }

echo "transcript-digest: hook contract"

make_transcript "$T/tx.jsonl" 4 "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

# --- 1. the ok path: a session_digest system event on the bot's actor --------
stub_model "'{\"context\":\"c\",\"worked\":\"w\",\"failed\":\"f\",\"would_change\":\"g\",\"reusable\":\"r\"}'"
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_MIN_TURNS=4)"
assert_eq "emits a system event"            system "$(field "$row" event_type)"
assert_eq "event token is session_digest"   session_digest "$(field "$row" event)"
assert_eq "subject is the bot's actor alias" bot:tfleet/tbot "$(field "$row" subject)"
assert_eq "subject_kind is actor"           actor "$(field "$row" subject_kind)"
assert_eq "qualifying session -> data.status=ok" ok "$(dfield "$row" status)"
assert_eq "rubric field 'worked' lands in data"  w "$(dfield "$row" worked)"
assert_eq "rubric field 'would_change' lands"    g "$(dfield "$row" would_change)"
assert_eq "turns counted (user+assistant, 4 pairs)" 8 "$(dfield "$row" turns)"
assert_eq "tool_use blocks counted"                 4 "$(dfield "$row" tool_calls)"
assert_eq "session_id carried from the payload" sess-1 "$(dfield "$row" session_id)"
assert_eq "session_uid carried from .plane-session" "$SESS_UID" "$(dfield "$row" session_uid)"
# THE #1503 pin: the sink is the plane, NOT a JSONL file.
assert_eq "NO transcript-digest-*.jsonl is written" yes "$(no_jsonl_written)"
[ -n "$row" ] && r=yes || r=no
assert_eq "the plane event actually landed"          yes "$r"

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

# --- 3b. the rest of the credential families ---------------------------------
# Every value below is SYNTHETIC or a vendor's own published documentation
# example — never a real credential. Each is embedded in a transcript and driven
# through the real script; the assertion is that it never reaches the model.
# Added after an adversarial battery found six families walking straight past
# the original list: the `sk-` rule is hyphen-anchored and missed Stripe's
# underscore forms, and AWS / Slack / JWT / env-dump / PEM had no rule at all.
# This hook runs fleet-wide and its digest lands on a shared plane, so the blast
# radius of a miss is every session on the host.
check_family() {  # <label> <synthetic-secret> [needle]
    local label="$1" secret="$2" needle="${3:-$2}"
    make_transcript "$T/sec.jsonl" 4 "$secret"
    stub_model "'{\"context\":\"c\",\"worked\":\"\",\"failed\":\"\",\"would_change\":\"\",\"reusable\":\"\"}'"
    run_digest "$T/sec.jsonl" SESSION_DIGEST_MIN_TURNS=4 >/dev/null
    local seen r
    seen="$(cat "$T/prompt-seen.txt" 2>/dev/null || true)"
    case "$seen" in *"$needle"*) r=yes ;; *) r=no ;; esac
    assert_eq "$label does NOT reach the model" no "$r"
}

# Vendor-shaped fixtures are ASSEMBLED AT RUNTIME from fragments, never written
# as contiguous literals. Two reasons, and the first one bit: GitHub push
# protection rejected this file when the literals were inline (Stripe x2, Slack)
# — and the right answer to a secret-scanner hit is a fixture no scanner can
# mistake for real, never an unblock URL. Second, it keeps the repo honest: no
# line here is a credential shape even out of context. The runtime value is
# byte-identical to what the rule must catch, so the test loses nothing.
_SK="sk"; _RK="rk"; _U="_"; _A="AKIA"; _XOX="xox"; _EY="ey"
check_family "AWS access-key id"   "${_A}IOSFODNN7EXAMPLE"
check_family "AWS secret (env-dump form)" "aws_secret_access_key=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY" "wJalrXUtnFEMI"
check_family "Stripe sk_live"      "${_SK}${_U}live${_U}4eC39HqLyjWDarjtT1zdp7dc"
check_family "Stripe sk_test"      "${_SK}${_U}test${_U}4eC39HqLyjWDarjtT1zdp7dc"
check_family "Stripe rk_ key"      "${_RK}${_U}live${_U}51H8xQzExampleRestrictedKey0000"
check_family "Slack xox token"     "${_XOX}b-0000000000-0000000000-AAAAAAAAAAAAAAAAAAAAAAAA"
check_family "JWT"                 "${_EY}JhbGciOiJIUzI1NiJ9.${_EY}JzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1rXwW1gFWFOEjXk"
check_family "env-dump PASSWORD"   "PASSWORD=hunter2hunter2hunter2" "hunter2hunter2"
check_family "env-dump *_SECRET"   "APP_SECRET=s3cr3tvaluethatislong" "s3cr3tvaluethat"
check_family "env-dump *_PAT"      "GITHUB_PAT=abcd1234abcd1234abcd" "abcd1234abcd1234"
check_family "PEM private key"     "-----BEGIN RSA PRIVATE KEY-----MIIEowIBAAKCAQEA0Zx-----END RSA PRIVATE KEY-----" "MIIEowIBAAKCAQEA"

# --- 3c. over-redaction guard ------------------------------------------------
# The generic name=value rule must not eat ordinary session content. PATH and
# PATTERN both contain "PAT"; redacting them would blind the digest to exactly
# the kind of detail these facts exist to carry.
make_transcript "$T/keep.jsonl" 4 "PATH=/usr/local/bin:/usr/bin PATTERN=chromium min_turns=6"
stub_model "'{\"context\":\"c\",\"worked\":\"\",\"failed\":\"\",\"would_change\":\"\",\"reusable\":\"\"}'"
run_digest "$T/keep.jsonl" SESSION_DIGEST_MIN_TURNS=4 >/dev/null
seen="$(cat "$T/prompt-seen.txt" 2>/dev/null || true)"
case "$seen" in *"PATH=/usr/local/bin"*) r=yes ;; *) r=no ;; esac
assert_eq "PATH= survives the generic rule"    yes "$r"
case "$seen" in *"PATTERN=chromium"*) r=yes ;; *) r=no ;; esac
assert_eq "PATTERN= survives the generic rule" yes "$r"

# --- 4. the qualifying gate: a fact, at zero model cost ----------------------
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_MIN_TURNS=99)"
assert_eq "below-gate session still emits a fact" skipped "$(dfield "$row" status)"
assert_eq "below-gate fact is still session_digest" session_digest "$(field "$row" event)"
assert_eq "below-gate fact still carries turns"        8 "$(dfield "$row" turns)"
[ -s "$T/prompt-seen.txt" ] && r=yes || r=no
assert_eq "below-gate session spends NO model call"   no "$r"
assert_eq "below-gate path writes NO JSONL either"   yes "$(no_jsonl_written)"

# --- 5. the null fact is distinct from the skipped fact ----------------------
# A qualified session where the model found nothing notable. "N tool calls,
# nothing notable" is the idle-bot signal the monitor exists to catch, so it
# must be an `ok` fact with empty fields, never a skip and never a dropped one.
stub_model "'{\"context\":\"routine triage\",\"worked\":\"\",\"failed\":\"\",\"would_change\":\"\",\"reusable\":\"\"}'"
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_MIN_TURNS=4)"
assert_eq "null fact is status=ok, not skipped" ok "$(dfield "$row" status)"
assert_eq "null fact keeps context"  "routine triage" "$(dfield "$row" context)"
assert_eq "null fact has empty worked"           "" "$(dfield "$row" worked)"
assert_eq "null fact still carries tool_calls"    4 "$(dfield "$row" tool_calls)"

# --- 6. tail-cap bounds what reaches the model -------------------------------
# Correctness, not just cost: a real transcript is ~5M tokens and cannot enter
# a 200K context at all.
make_transcript "$T/big.jsonl" 300
stub_model "'{\"context\":\"c\",\"worked\":\"\",\"failed\":\"\",\"would_change\":\"\",\"reusable\":\"\"}'"
row="$(run_digest "$T/big.jsonl" SESSION_DIGEST_MIN_TURNS=4 SESSION_DIGEST_TAIL_CHARS=5000)"
dc="$(dfield "$row" digest_chars)"
# The distillation header embeds transcript_path, so digest_chars carries the
# fixture dir's length on top of the tail-capped text. A fixed slack silently
# assumed a short ambient /tmp; the bound names the path term instead (#846 —
# suites now run under a constructed, longer TMPDIR).
_dc_bound=$((5100 + ${#T}))
[ "$dc" -le "$_dc_bound" ] && r=yes || r=no
assert_eq "tail-cap 5000 bounds digest_chars (<=$_dc_bound, got $dc)" yes "$r"
sz="$(wc -c < "$T/prompt-seen.txt" | tr -d ' ')"
[ "$sz" -le 7000 ] && r=yes || r=no
assert_eq "prompt sent to the model stays bounded (<=7000, got $sz)" yes "$r"
row="$(run_digest "$T/big.jsonl" SESSION_DIGEST_MIN_TURNS=4 SESSION_DIGEST_TAIL_CHARS=40000)"
dc2="$(dfield "$row" digest_chars)"
[ "$dc2" -gt "$dc" ] && r=yes || r=no
assert_eq "a larger cap really sends more (cap is honored, not fixed)" yes "$r"

# --- 7. failure is loud on the plane and silent to the session ---------------
# A SessionEnd hook that blocks or throws would break session teardown.
stub_model "'not json at all'"
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_MIN_TURNS=4)"
assert_eq "unparseable model output -> data.status=error" error "$(dfield "$row" status)"
[ -n "$(dfield "$row" error)" ] && r=yes || r=no
assert_eq "error fact explains itself" yes "$r"

rm -f "$T/bin/claude"
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_MIN_TURNS=4 CLAUDE_BIN=no-such-binary)"
assert_eq "absent model binary -> data.status=error" error "$(dfield "$row" status)"
stub_model "'{\"context\":\"c\"}'"

pay='{"session_id":"s","transcript_path":"/nonexistent/nope.jsonl","cwd":"/tmp"}'
: > "$T/capture.jsonl"
printf '%s' "$pay" | env CLAUDLOBBY_ROOT="$T/root" BOT_ID=tbot CLAUDLOBBY_FLEET=tfleet BOT_DIR="$T/botdir" \
    PATH="$T/bin:/usr/bin:/bin" CLAUDE_BIN=claude SESSION_DIGEST_ENABLED=1 \
    PLANE_EMIT_CLI="bash $CAPTURE_CLI" PLANE_SOCKET="$T/root/state/plane/nope.sock" \
    PLANE_CAPTURE="$T/capture.jsonl" bash "$DIGEST" >/dev/null 2>&1; rc=$?
assert_eq "missing transcript still exits 0 (never blocks session end)" 0 "$rc"

printf '%s' '' | env CLAUDLOBBY_ROOT="$T/root" BOT_ID=tbot CLAUDLOBBY_FLEET=tfleet BOT_DIR="$T/botdir" \
    PATH="$T/bin:/usr/bin:/bin" CLAUDE_BIN=claude SESSION_DIGEST_ENABLED=1 \
    PLANE_EMIT_CLI="bash $CAPTURE_CLI" PLANE_SOCKET="$T/root/state/plane/nope.sock" \
    PLANE_CAPTURE="$T/capture.jsonl" bash "$DIGEST" >/dev/null 2>&1; rc=$?
assert_eq "empty payload still exits 0" 0 "$rc"

# --- 7b. a plane the shim cannot reach is DISCLOSED, hook still exits 0 -------
# The cold rung fails (its command exits nonzero) after the dead socket; the
# record is lost but SAID LOUDLY on stderr — never silently dropped — and the
# SessionEnd hook must still exit 0 (non-blocking).
printf '#!/bin/bash\nexit 1\n' > "$T/bin/failcli"; chmod +x "$T/bin/failcli"
stub_model "'{\"context\":\"c\",\"worked\":\"\",\"failed\":\"\",\"would_change\":\"\",\"reusable\":\"\"}'"
: > "$T/capture.jsonl"; : > "$T/err.txt"
pay="$(TX="$T/tx.jsonl" python3 -c 'import json,os;print(json.dumps({"session_id":"s","transcript_path":os.environ["TX"],"cwd":"/tmp"}))')"
printf '%s' "$pay" | env CLAUDLOBBY_ROOT="$T/root" BOT_ID=tbot CLAUDLOBBY_FLEET=tfleet BOT_DIR="$T/botdir" \
    PATH="$T/bin:/usr/bin:/bin" CLAUDE_BIN=claude SESSION_DIGEST_ENABLED=1 SESSION_DIGEST_MIN_TURNS=4 \
    PLANE_EMIT_CLI="bash $T/bin/failcli" PLANE_SOCKET="$T/root/state/plane/nope.sock" \
    PLANE_CAPTURE="$T/capture.jsonl" bash "$DIGEST" >/dev/null 2>"$T/err.txt"; rc=$?
assert_eq "plane failure: hook still exits 0" 0 "$rc"
[ ! -s "$T/capture.jsonl" ] && r=yes || r=no
assert_eq "plane failure: nothing was recorded" yes "$r"
case "$(cat "$T/err.txt")" in *"plane record failed"*) r=yes ;; *) r=no ;; esac
assert_eq "plane failure is DISCLOSED on stderr, not silent" yes "$r"
stub_model "'{\"context\":\"c\"}'"

# --- 8. the kill switch: SESSION_DIGEST_ENABLED=0 ----------------------------
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_ENABLED=0)"
assert_eq "SESSION_DIGEST_ENABLED=0 records nothing at all" "" "$row"

# --- 8b. PLANE_EMIT_DISABLED=1 is the plane silencer -------------------------
# The hook self-gates on it up front (the plane is now its only sink): exit 0,
# nothing recorded, and — the point — NO model call spent on a digest nobody
# keeps.
stub_model "'{\"context\":\"c\",\"worked\":\"\",\"failed\":\"\",\"would_change\":\"\",\"reusable\":\"\"}'"
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_MIN_TURNS=4 PLANE_EMIT_DISABLED=1)"
assert_eq "PLANE_EMIT_DISABLED=1 records nothing" "" "$row"
[ -s "$T/prompt-seen.txt" ] && r=yes || r=no
assert_eq "PLANE_EMIT_DISABLED=1 spends NO model call" no "$r"

# --- 9. dormant by default (the rollout contract) ----------------------------
# The hook composes into every bot on every fleet the moment this merges. If it
# were default-on, the next generate or daily reload would arm ~17 bots across
# 4 fleets simultaneously — an uncanaried estate-wide Haiku roll. It must do
# NOTHING until a fleet opts in, so that rollout is one fleet at a time.
stub_model "'{\"context\":\"c\",\"worked\":\"\",\"failed\":\"\",\"would_change\":\"\",\"reusable\":\"\"}'"
out="$(run_digest_unarmed "$T/tx.jsonl")"
assert_eq "unarmed fleet records NOTHING" "" "$out"
[ -s "$T/prompt-seen.txt" ] && r=yes || r=no
assert_eq "unarmed fleet spends NO model call" no "$r"
assert_eq "unarmed fleet writes no JSONL either" yes "$(no_jsonl_written)"

# Only an explicit "1" arms it — a stray truthy-looking value must not.
for v in 0 yes true ""; do
    row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_ENABLED="$v")"
    assert_eq "SESSION_DIGEST_ENABLED='$v' stays dormant" "" "$row"
done
row="$(run_digest "$T/tx.jsonl" SESSION_DIGEST_ENABLED=1 SESSION_DIGEST_MIN_TURNS=4)"
assert_eq "SESSION_DIGEST_ENABLED=1 arms it" ok "$(dfield "$row" status)"

# --- 9. the non-blocking SAFETY NET: the ERR trap arms before set -e ----------
# Every real path is guarded (|| true, :- defaults), so an unhandled error is
# meant to be impossible -- but the `trap 'exit 0' ERR` is the net for one that
# slips through, and a SessionEnd hook that exits NONZERO breaks a session end.
# The net is only observable on an unanticipated set -e violation, which a
# robust hook gives no clean way to force from a test; pin it STRUCTURALLY
# instead -- present, and armed BEFORE set -e (a violation before the trap arms
# would still exit nonzero). Kills the mutant that drops `exit 0` from the trap.
_trap_ln=$(grep -n "trap 'exit 0' ERR" "$DIGEST" | head -1 | cut -d: -f1)
_sete_ln=$(grep -n "set -euo pipefail" "$DIGEST" | head -1 | cut -d: -f1)
[ -n "$_trap_ln" ] && r=yes || r=no
assert_eq "the ERR->exit 0 non-blocking safety net is present" yes "$r"
{ [ -n "$_trap_ln" ] && [ -n "$_sete_ln" ] && [ "$_trap_ln" -lt "$_sete_ln" ]; } && r=yes || r=no
assert_eq "the safety net arms BEFORE set -e" yes "$r"

echo
echo "  $PASS/$TOTAL passed"
[ "$FAIL" -eq 0 ] || exit 1
