#!/bin/bash
# Hermetic suite for lib/plane-emit.sh (Phase-2 T2): the ladder, the
# disclosures, the disabled no-op, verdict passthrough, and cross-rung
# idempotency material (pre-minted ids in the replayed file).
# Hermetic: fake daemon = an inline python3 unix-socket server on a SHORT
# /tmp path; the CLI rung = a recorder stub via PLANE_EMIT_CLI. No claudlobby
# import, no network, no tmux.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"
SHIM="$LIB_DIR/plane-emit.sh"

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/planeemit.XXXXXX")
sockdir=$(mktemp -d /tmp/pe.XXXXXX)   # short: sun_path limit
trap 'rm -rf "$tmpdir" "$sockdir"; [ -n "${daemon_pid:-}" ] && kill "$daemon_pid" 2>/dev/null || true' EXIT

export CLAUDLOBBY_ROOT="$tmpdir/root"
mkdir -p "$CLAUDLOBBY_ROOT"
# Hermetic against the caller's session (#1693): a bot session exports its
# identity, and a canary bot carries a deadline knob -- either would change
# what the arm-record and default-deadline cases below observe.
unset BOT_ID FLEET_NAME CLAUDLOBBY_FLEET PLANE_EMIT_CLASS \
    PLANE_SOCKET_DEADLINE_HOOK_S PLANE_SOCKET_DEADLINE_BACKGROUND_S PLANE_SOCKET_DEADLINE_DOOR_S

batch='{"events": [{"event_type": "communication", "emitter": "sh-test", "fleet": "f", "payload": {"msg_id": "msg_00000000000000000000000000000000", "sender": "bot:f/a", "message_class": "notice"}}]}'

# --- recorder stub for the CLI rung -----------------------------------------
recorder="$tmpdir/recorder.sh"
cat > "$recorder" <<'REC'
#!/bin/bash
echo "$@" >> "$RECORDER_LOG"
json=""
prev=""
for a in "$@"; do [ "$prev" = "--json" ] && json="$a"; prev="$a"; done
[ -n "$json" ] && cp "$json" "$RECORDER_COPY"
exit "${RECORDER_EXIT:-0}"
REC
chmod +x "$recorder"
export PLANE_EMIT_DISABLED=0
export RECORDER_LOG="$tmpdir/rec.log" RECORDER_COPY="$tmpdir/rec.json"

# --- fake daemon: replies with $RESP_FILE content per connection -------------
fake_daemon="$tmpdir/faked.py"
cat > "$fake_daemon" <<'PY'
import socket, sys, time
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sys.argv[1])
srv.listen(8)
resp = open(sys.argv[2], "rb").read()
delay = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0  # #1693: a slow daemon
open(sys.argv[1] + ".ready", "w").close()
while True:
    c, _ = srv.accept()
    buf = b""
    while b"\n" not in buf:
        chunk = c.recv(65536)
        if not chunk:
            break
        buf += chunk
    open(sys.argv[3], "ab").write(buf)
    if delay and buf:
        time.sleep(delay)  # answers late, as the SD card makes the real one
    try:
        c.sendall(resp)
    except OSError:
        pass  # a liveness probe (#1657) connects and closes without a request
    c.close()
PY

start_daemon() {  # $1=response-json [$2=seconds before each reply]
    printf '%s\n' "$1" > "$tmpdir/resp.json"
    rm -f "$sockdir/s" "$sockdir/s.ready"
    python3 "$fake_daemon" "$sockdir/s" "$tmpdir/resp.json" "$tmpdir/seen.jsonl" "${2:-0}" &
    daemon_pid=$!
    for _ in $(seq 1 100); do [ -e "$sockdir/s.ready" ] && break; sleep 0.05; done
    [ -e "$sockdir/s.ready" ] || { echo "FAIL: fake daemon never bound"; exit 1; }
}
stop_daemon() { kill "$daemon_pid" 2>/dev/null || true; wait "$daemon_pid" 2>/dev/null || true; daemon_pid=""; }

# Test 1: PLANE_EMIT_DISABLED=1 is a byte-identical no-op
out=$(printf '%s' "$batch" | PLANE_EMIT_DISABLED=1 PLANE_EMIT_CLI="$recorder" \
      PLANE_SOCKET="$sockdir/s" bash "$SHIM" 2>&1); rc=$?
[ "$rc" -eq 0 ] || { echo "FAIL(1): disabled rc=$rc"; exit 1; }
[ -z "$out" ] || { echo "FAIL(1): disabled produced output: $out"; exit 1; }
[ ! -e "$RECORDER_LOG" ] || { echo "FAIL(1): disabled invoked the CLI rung"; exit 1; }

# Test 2: rung 1 success — daemon answers, CLI rung untouched
start_daemon '{"ok": true, "results": [{"event_id": "ev_11111111111111111111111111111111", "status": "committed"}]}'
out=$(printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" 2>"$tmpdir/err2"); rc=$?
stop_daemon
[ "$rc" -eq 0 ] || { echo "FAIL(2): rc=$rc"; cat "$tmpdir/err2"; exit 1; }
echo "$out" | grep -q "ev_1111" || { echo "FAIL(2): no event id on stdout: $out"; exit 1; }
[ ! -e "$RECORDER_LOG" ] || { echo "FAIL(2): CLI rung invoked despite daemon success"; exit 1; }
grep -q '"event_id"' "$tmpdir/seen.jsonl" || { echo "FAIL(2): daemon saw no pre-minted id"; exit 1; }

# Test 3: daemon down — disclosed fallback replays the FINALIZED file
out=$(printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/absent" bash "$SHIM" 2>"$tmpdir/err3"); rc=$?
[ "$rc" -eq 0 ] || { echo "FAIL(3): rc=$rc"; cat "$tmpdir/err3"; exit 1; }
grep -q "falling back to cold CLI" "$tmpdir/err3" || { echo "FAIL(3): fallback not disclosed"; exit 1; }
[ -e "$RECORDER_LOG" ] || { echo "FAIL(3): CLI rung never invoked"; exit 1; }
grep -q '"event_id": "ev_' "$RECORDER_COPY" || { echo "FAIL(3): replayed file lacks pre-minted ids"; exit 1; }
grep -q '"occurred_at"' "$RECORDER_COPY" || { echo "FAIL(3): replayed file lacks occurred_at"; exit 1; }

# Tests 4-6 EXPECT nonzero shim exits: `pipeline; rc=$?` under set -e dies at
# the pipeline before rc is read — the `rc=0; ... || rc=$?` form is the guard.

# Test 4: verdict passthrough — contract violation never falls back
rm -f "$RECORDER_LOG" "$RECORDER_COPY"
start_daemon '{"ok": false, "code": "contract_violation", "error": "bad message_class"}'
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" 2>"$tmpdir/err4" || rc=$?
stop_daemon
[ "$rc" -eq 2 ] || { echo "FAIL(4): verdict rc=$rc (want 2)"; exit 1; }
[ ! -e "$RECORDER_LOG" ] || { echo "FAIL(4): fell back on a verdict"; exit 1; }

# Test 5: CLI rung failure surfaces its rc with disclosure
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" RECORDER_EXIT=3 PLANE_SOCKET="$sockdir/absent" bash "$SHIM" 2>"$tmpdir/err5" || rc=$?
[ "$rc" -eq 3 ] || { echo "FAIL(5): rc=$rc (want 3)"; exit 1; }
grep -q "cold CLI rung failed rc=3" "$tmpdir/err5" || { echo "FAIL(5): failure not disclosed"; exit 1; }

# Test 6: unreadable stdin is a verdict (exit 2), not a fallback
rc=0
printf 'not json' | PLANE_EMIT_CLI="$recorder" RECORDER_EXIT=0 PLANE_SOCKET="$sockdir/absent" bash "$SHIM" 2>"$tmpdir/err6" || rc=$?
[ "$rc" -eq 2 ] || { echo "FAIL(6): rc=$rc (want 2)"; exit 1; }

# Test 7 (#1485): a `downgrade` refusal is NOT a verdict. The daemon that
# answers it is running older code than the db it opened; the cold rung is a
# fresh interpreter on the install CURRENT code, so it commits. Live on the
# Mini this cost 261 heartbeat samples in ~15 minutes.
rm -f "$RECORDER_LOG" "$RECORDER_COPY" "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
mkdir -p "$CLAUDLOBBY_ROOT/state/plane"   # where the arm record lands, as on a live root
start_daemon '{"ok": false, "code": "downgrade", "error": "plane.db user_version=10 is newer than this code (supports <=9) - refusing downgrade"}'
rc=0
out=$(printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" 2>"$tmpdir/err7") || rc=$?
stop_daemon
[ "$rc" -eq 0 ] || { echo "FAIL(7): downgrade rc=$rc (want 0 via the cold rung)"; cat "$tmpdir/err7"; exit 1; }
[ -e "$RECORDER_LOG" ] || { echo "FAIL(7): the cold rung was never invoked"; exit 1; }
[ "$(wc -l < "$RECORDER_LOG")" -eq 1 ] || { echo "FAIL(7): cold rung ran $(wc -l < "$RECORDER_LOG") times, want exactly 1"; exit 1; }
grep -q "falling back to cold CLI" "$tmpdir/err7" || { echo "FAIL(7): fallback not disclosed"; exit 1; }
grep -q "older code than the db it opened" "$tmpdir/err7" || { echo "FAIL(7): the stale-daemon condition was not named"; exit 1; }
# Exactly once: the id the daemon SAW is the id the cold rung replayed, so a
# lost ack classifies as duplicate rather than landing a second row.
seen_id=$(grep -o '"event_id": "ev_[0-9a-f]*"' "$tmpdir/seen.jsonl" | tail -1)
[ -n "$seen_id" ] || { echo "FAIL(7): daemon saw no pre-minted id"; exit 1; }
grep -q "$seen_id" "$RECORDER_COPY" || { echo "FAIL(7): replayed batch minted a NEW id ($seen_id absent) - a duplicate row"; exit 1; }
# #1693: the arm it caused is recorded under its own cause, not as a timeout.
[ "$(tail -1 "$CLAUDLOBBY_ROOT/state/plane/.socket-arms" | cut -f6)" = "downgrade" ] || { echo "FAIL(7): the arm record does not name the downgrade"; tail -1 "$CLAUDLOBBY_ROOT/state/plane/.socket-arms"; exit 1; }

# Test 8 (#1485 companion): a contract_violation still passes through with no
# cold attempt. The two must not move together - that is the whole rule.
rm -f "$RECORDER_LOG" "$RECORDER_COPY" "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
start_daemon '{"ok": false, "code": "contract_violation", "error": "bad message_class"}'
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" 2>"$tmpdir/err8" || rc=$?
stop_daemon
[ "$rc" -eq 2 ] || { echo "FAIL(8): verdict rc=$rc (want 2)"; exit 1; }
[ ! -e "$RECORDER_LOG" ] || { echo "FAIL(8): a contract violation fell back to the cold rung"; exit 1; }

# Test 9 (#1657): a genuine transport failure gets its own, self-sufficient
# wording naming "transport failed" -- distinct from the cooldown wording in
# test 10, so a reader (or a grep) can tell the two apart from this ONE line
# without needing the line printed before it.
rm -f "$RECORDER_LOG" "$RECORDER_COPY" "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
out=$(printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/absent" bash "$SHIM" 2>"$tmpdir/err9"); rc=$?
[ "$rc" -eq 0 ] || { echo "FAIL(9): rc=$rc"; cat "$tmpdir/err9"; exit 1; }
grep -q "transport failed (rc=5) — falling back to cold CLI" "$tmpdir/err9" || { echo "FAIL(9): breach not distinctly worded"; cat "$tmpdir/err9"; exit 1; }
# "daemon unavailable" is deliberately absent from BOTH branches now (review
# residual, #1657): the transport-failed except block also catches a
# reachable-but-slow daemon and a reachable-but-garbled reply under the same
# rc=5, so "unavailable" is provably false for those, and the genuine-breach
# mechanism itself is unreproduced -- naming a specific cause here would be
# the same mistake this fix exists to remove.
grep -q "daemon unavailable" "$tmpdir/err9" && { echo "FAIL(9): breach line still claims unavailability the transport-failed path cannot support"; exit 1; }
grep -q "cooldown finalize" "$tmpdir/err9" && { echo "FAIL(9): breach wrongly used the cooldown wording"; exit 1; }

# Test 10 (#1657, the actual defect): during a wedge cooldown the shim
# deliberately skips the socket and runs --finalize-only, which the client
# returns 5 for on SUCCESS (verified directly against plane-socket-client.py:
# that branch has no failure path at all, unlike the transport branch's own
# separate `return 5`). Calling this "daemon unavailable" was not an inflated
# failure, it was a WRONG one -- measured live, 645 of every 719 such lines
# on one host in 24h were this branch (9.7x inflation), and the discriminator
# lived only in a DIFFERENT line above it, invisible to a grep for the error
# string. A REAL daemon is started here (not just an absent socket) so a
# regression that still touches the socket during cooldown would show up as
# a connection in seen.jsonl, not just as wrong text.
rm -f "$RECORDER_LOG" "$RECORDER_COPY"
start_daemon '{"ok": true, "results": [{"event_id": "ev_22222222222222222222222222222222", "status": "committed"}]}'
# seen.jsonl is opened "ab" by the fake daemon and accumulates across every
# start_daemon call in this whole suite, so "empty" is not the right check by
# test 10 -- a before/after LINE COUNT is what actually proves no NEW
# connection happened during this specific run.
seen_before=$(wc -l < "$tmpdir/seen.jsonl" 2>/dev/null || echo 0)
mkdir -p "$CLAUDLOBBY_ROOT/state/plane"
date +%s > "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
out=$(printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" 2>"$tmpdir/err10"); rc=$?
stop_daemon
seen_after=$(wc -l < "$tmpdir/seen.jsonl" 2>/dev/null || echo 0)
[ "$rc" -eq 0 ] || { echo "FAIL(10): rc=$rc"; cat "$tmpdir/err10"; exit 1; }
grep -q "daemon unavailable" "$tmpdir/err10" && { echo "FAIL(10): a deliberate cooldown finalize was reported as daemon unavailable"; cat "$tmpdir/err10"; exit 1; }
grep -q "cooldown finalize succeeded" "$tmpdir/err10" || { echo "FAIL(10): cooldown finalize not distinctly disclosed"; cat "$tmpdir/err10"; exit 1; }
grep -q "daemon not contacted" "$tmpdir/err10" || { echo "FAIL(10): the deliberate-skip framing is missing"; exit 1; }
[ -e "$RECORDER_LOG" ] || { echo "FAIL(10): the cold rung was never invoked"; exit 1; }
[ "$seen_after" -eq "$seen_before" ] || { echo "FAIL(10): the daemon WAS contacted during a cooldown ($seen_before -> $seen_after) -- the message would be right by accident"; exit 1; }
rm -f "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"

# --- #1711: a SPOOLED batch must not read as RECORDED -----------------------
# The daemon replies ok:true for a spooled batch (its `ok` is unconditional on
# outcome kind), so before this the shim exited 0 and every door read
# "recorded" about a row no reader can see.

# Test 11: spooled -> rc 6, a VERDICT (no fallback), disclosed as spooled
rm -f "$RECORDER_LOG" "$RECORDER_COPY"
start_daemon '{"ok": true, "results": [{"event_id": "ev_33333333333333333333333333333333", "status": "spooled"}]}'
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/err11" || rc=$?
stop_daemon
[ "$rc" -eq 6 ] || { echo "FAIL(11): spooled rc=$rc (want 6 — 0 would assert 'recorded' about a row nothing can read)"; cat "$tmpdir/err11"; exit 1; }
[ ! -e "$RECORDER_LOG" ] || { echo "FAIL(11): replayed a batch that is ALREADY on disk — the cold rung would spool it twice"; exit 1; }
grep -qi "spooled" "$tmpdir/err11" || { echo "FAIL(11): spool not disclosed"; cat "$tmpdir/err11"; exit 1; }
grep -qi "fail" "$tmpdir/err11" && { echo "FAIL(11): a spooled batch was worded as a FAILURE — nothing was lost; that is the dead-signal defect"; cat "$tmpdir/err11"; exit 1; }

# Test 12: positive control — committed is STILL 0. Without this a mutation
# that returns 6 unconditionally passes Test 11 and breaks every door.
rm -f "$RECORDER_LOG"
start_daemon '{"ok": true, "results": [{"event_id": "ev_44444444444444444444444444444444", "status": "committed"}]}'
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/err12" || rc=$?
stop_daemon
[ "$rc" -eq 0 ] || { echo "FAIL(12): committed rc=$rc (want 0)"; cat "$tmpdir/err12"; exit 1; }

# Test 13: the COLD rung spools too, and must not be called a failure either
rm -f "$RECORDER_LOG" "$RECORDER_COPY"
rc=0
printf '%s' "$batch" | RECORDER_EXIT=6 PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>"$tmpdir/err13" || rc=$?
[ "$rc" -eq 6 ] || { echo "FAIL(13): cold-rung spool rc=$rc (want 6)"; cat "$tmpdir/err13"; exit 1; }
grep -qi "SPOOLED by the cold rung" "$tmpdir/err13" || { echo "FAIL(13): cold-rung spool not distinctly disclosed"; cat "$tmpdir/err13"; exit 1; }
grep -q "cold CLI rung failed" "$tmpdir/err13" && { echo "FAIL(13): cold-rung spool worded as a failure"; exit 1; }

# --- #1657: a cooldown STAGES an opted-in batch for the daemon --------------
# Under load every cooldown emission spawned the package-importing cold CLI,
# and those spawns kept the CPU the daemon needed pegged. An emitter that set
# PLANE_EMIT_COOLDOWN_STAGE=1 now leaves its finalized batch in state/plane/
# staged/ for the daemon to replay -- but only when a daemon is listening AND
# that dir exists (a daemon that replays it creates it at startup). Every
# other case must still take the cold CLI; Tests 16 and 17 pin two of them.
staged="$CLAUDLOBBY_ROOT/state/plane/staged"
arm_cooldown() { mkdir -p "$CLAUDLOBBY_ROOT/state/plane"; date +%s > "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"; }

# Test 14: opted in, daemon listening, staged dir present -> staged, rc 6, no CLI
rm -f "$RECORDER_LOG" "$RECORDER_COPY"; rm -rf "$staged"; mkdir -p "$staged"
start_daemon '{"ok": true, "results": []}'
arm_cooldown
rc=0
printf '%s' "$batch" | PLANE_EMIT_COOLDOWN_STAGE=1 PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/err14" || rc=$?
stop_daemon
[ "$rc" -eq 6 ] || { echo "FAIL(14): opted-in cooldown rc=$rc (want 6: staged, durable, not yet in the plane)"; cat "$tmpdir/err14"; exit 1; }
[ ! -e "$RECORDER_LOG" ] || { echo "FAIL(14): the cold CLI ran during an opted-in cooldown -- the #1657 amplifier"; exit 1; }
n=$(find "$staged" -name '*.batch' | wc -l)
[ "$n" -eq 1 ] || { echo "FAIL(14): want exactly 1 staged batch, found $n"; ls -la "$staged"; exit 1; }
grep -q '"event_id": "ev_' "$staged"/*.batch || { echo "FAIL(14): the staged batch lacks its pre-minted id"; exit 1; }

# Test 16: daemon listening and dir present, but the caller did NOT opt in ->
# cold CLI as today. The doors that refuse on a non-zero rc (task-act,
# workstream-update, checkin-record) must never be handed a staged rc 6.
rm -f "$RECORDER_LOG" "$RECORDER_COPY"; rm -rf "$staged"; mkdir -p "$staged"
start_daemon '{"ok": true, "results": []}'
arm_cooldown
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/err16" || rc=$?
stop_daemon
[ "$rc" -eq 0 ] || { echo "FAIL(16): rc=$rc"; cat "$tmpdir/err16"; exit 1; }
[ -e "$RECORDER_LOG" ] || { echo "FAIL(16): a caller that did not opt in skipped the cold CLI"; exit 1; }
[ -z "$(ls -A "$staged")" ] || { echo "FAIL(16): staged a batch for a caller that did not opt in"; exit 1; }

# Test 17: opted in and listening, but NO staged dir -- an older daemon that
# cannot replay it (a pull reaches lib/ before the daemon restarts) -> cold CLI,
# and the client must not create the dir itself (the dir IS the handshake).
rm -f "$RECORDER_LOG" "$RECORDER_COPY"; rm -rf "$staged"
start_daemon '{"ok": true, "results": []}'
arm_cooldown
rc=0
printf '%s' "$batch" | PLANE_EMIT_COOLDOWN_STAGE=1 PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/err17" || rc=$?
stop_daemon
[ "$rc" -eq 0 ] || { echo "FAIL(17): rc=$rc"; cat "$tmpdir/err17"; exit 1; }
[ -e "$RECORDER_LOG" ] || { echo "FAIL(17): no staged dir (an old daemon), yet the cold CLI did not run"; exit 1; }
[ ! -e "$staged" ] || { echo "FAIL(17): the client created the staged dir itself -- nothing guarantees a replayer"; exit 1; }

rm -f "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"; rm -rf "$staged"

# --- #1693: the socket deadline follows the caller's class ------------------
# The 1.0 s total deadline was one number for every caller, and on the Pi's SD
# card a plain commit can take longer: every miss arms the host-wide marker.
# A caller names its class (PLANE_EMIT_CLASS: hook | background | door) and
# that class's knob sets its deadline. Every knob defaults to today's 1.0 s,
# so a root pull changes nothing until a bot is given one.
slow_ok='{"ok": true, "results": [{"event_id": "ev_55555555555555555555555555555555", "status": "committed"}]}'
marker="$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
arms="$CLAUDLOBBY_ROOT/state/plane/.socket-arms"
reset_1693() { rm -f "$RECORDER_LOG" "$RECORDER_COPY" "$marker" "$arms"; }

# Test 18 (the default IS today): no knob, a daemon that answers in 1.5 s is a
# miss for a hook -- the cold rung records it and the marker arms, as before.
reset_1693
start_daemon "$slow_ok" 1.5
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLASS=hook PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/err18" || rc=$?
stop_daemon
[ "$rc" -eq 0 ] || { echo "FAIL(18): rc=$rc"; cat "$tmpdir/err18"; exit 1; }
grep -q "transport failed (rc=5)" "$tmpdir/err18" || { echo "FAIL(18): with no knob a 1.5s daemon was not a miss -- the default moved"; cat "$tmpdir/err18"; exit 1; }
[ -e "$RECORDER_LOG" ] || { echo "FAIL(18): the cold rung did not record the miss"; exit 1; }
[ -e "$marker" ] || { echo "FAIL(18): the miss did not arm the marker"; exit 1; }
awk -F'\t' 'NF == 6 && $2 == "hook" && $3 == "1.0" && $4 >= 990 && $6 == "timeout" { ok = 1 } END { exit !ok }' "$arms" \
    || { echo "FAIL(18): want an arm record: hook 1.0 >=990ms ... timeout"; cat -A "$arms" 2>/dev/null; exit 1; }

# Test 19: the class's knob raises ITS deadline. The same 1.5 s daemon answers
# inside 4 s: recorded by the socket, no cold rung, and no marker.
reset_1693
start_daemon "$slow_ok" 1.5
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLASS=hook PLANE_SOCKET_DEADLINE_HOOK_S=4 PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" >"$tmpdir/out19" 2>"$tmpdir/err19" || rc=$?
stop_daemon
[ "$rc" -eq 0 ] || { echo "FAIL(19): rc=$rc"; cat "$tmpdir/err19"; exit 1; }
grep -q "ev_5555" "$tmpdir/out19" || { echo "FAIL(19): the socket's answer never reached stdout"; cat "$tmpdir/err19"; exit 1; }
[ ! -e "$RECORDER_LOG" ] || { echo "FAIL(19): the cold rung ran although the daemon answered inside the class deadline"; cat "$tmpdir/err19"; exit 1; }
[ ! -e "$marker" ] || { echo "FAIL(19): an answered request armed the marker"; exit 1; }
[ ! -e "$arms" ] || { echo "FAIL(19): an answered request wrote an arm record"; exit 1; }

# Test 20: a knob belongs to ONE class. The hook knob does nothing for a door,
# nor for a caller that names no class.
for cls in door ""; do
    reset_1693
    start_daemon "$slow_ok" 1.5
    rc=0
    printf '%s' "$batch" | PLANE_EMIT_CLASS="$cls" PLANE_SOCKET_DEADLINE_HOOK_S=4 PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/err20" || rc=$?
    stop_daemon
    [ "$rc" -eq 0 ] || { echo "FAIL(20:$cls): rc=$rc"; cat "$tmpdir/err20"; exit 1; }
    grep -q "transport failed (rc=5)" "$tmpdir/err20" || { echo "FAIL(20:'$cls'): the hook knob changed the deadline of class '$cls'"; exit 1; }
done

# Test 20b: a class the shim does not know is DISCLOSED (a caller's typo would
# otherwise leave it on the default deadline in silence).
reset_1693
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLASS=hooks PLANE_SOCKET_DEADLINE_HOOK_S=4 PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>"$tmpdir/err20b" || rc=$?
[ "$rc" -eq 0 ] || { echo "FAIL(20b): rc=$rc"; cat "$tmpdir/err20b"; exit 1; }
grep -q "PLANE_EMIT_CLASS=hooks" "$tmpdir/err20b" || { echo "FAIL(20b): an unknown class was not named"; cat "$tmpdir/err20b"; exit 1; }

# Test 21: a knob the client would refuse is DISCLOSED and IGNORED. Passed
# through, the client exits 2 on it, and 2 is a verdict: the record would be
# dropped with no fallback, on every emit, from a typo.
for bad in 3s 0 0.0 inf nan -1 4000 1e3; do
    reset_1693
    rc=0
    printf '%s' "$batch" | PLANE_EMIT_CLASS=door PLANE_SOCKET_DEADLINE_DOOR_S="$bad" PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>"$tmpdir/err21" || rc=$?
    [ "$rc" -eq 0 ] || { echo "FAIL(21:$bad): rc=$rc -- a bad knob dropped the record"; cat "$tmpdir/err21"; exit 1; }
    grep -q "PLANE_SOCKET_DEADLINE_DOOR_S" "$tmpdir/err21" || { echo "FAIL(21:$bad): the ignored knob was not named"; cat "$tmpdir/err21"; exit 1; }
    [ -e "$RECORDER_LOG" ] || { echo "FAIL(21:$bad): the record never reached the cold rung"; exit 1; }
done

# Test 22 (the canary's instrument): every arm is recorded with WHO armed it.
# The marker holds only a time, so arms could be counted per host and never
# per bot -- and a one-bot canary is invisible in a host count.
reset_1693
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLASS=hook BOT_ID=b FLEET_NAME=f PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>"$tmpdir/err22" || rc=$?
[ "$rc" -eq 0 ] || { echo "FAIL(22): rc=$rc"; cat "$tmpdir/err22"; exit 1; }
[ -s "$arms" ] || { echo "FAIL(22): the arm was not recorded"; cat "$tmpdir/err22"; exit 1; }
[ "$(wc -l < "$arms")" -eq 1 ] || { echo "FAIL(22): want 1 arm record"; cat "$arms"; exit 1; }
awk -F'\t' 'NF == 6 && $1 ~ /^[0-9]+$/ && $2 == "hook" && $3 == "1.0" && $4 ~ /^[0-9]+$/ && $5 == "bot:f/b" && $6 == "unreachable" { ok = 1 } END { exit !ok }' "$arms" \
    || { echo "FAIL(22): want <epoch> hook 1.0 <ms> bot:f/b unreachable, got:"; cat -A "$arms"; exit 1; }

# Test 23: the record carries the deadline in force, and a caller outside a
# bot is named by its fleet, else as the host.
reset_1693
printf '%s' "$batch" | PLANE_EMIT_CLASS=background PLANE_SOCKET_DEADLINE_BACKGROUND_S=2.5 CLAUDLOBBY_FLEET=g PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>/dev/null || true
rm -f "$marker"   # or the second emission is in cooldown and never tries the socket
printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>/dev/null || true
awk -F'\t' 'NR == 1 && $2 == "background" && $3 == "2.5" && $5 == "fleet:g" { a = 1 } NR == 2 && $2 == "-" && $3 == "1.0" && $5 == "host" { b = 1 } END { exit !(a && b && NR == 2) }' "$arms" \
    || { echo "FAIL(23): records do not name the deadline and the caller"; cat -A "$arms"; exit 1; }

# Test 24: a cooldown touches no socket, so it arms nothing and records nothing.
reset_1693
arm_cooldown
printf '%s' "$batch" | PLANE_EMIT_CLASS=hook PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>/dev/null || true
[ ! -e "$arms" ] || { echo "FAIL(24): a cooldown emission wrote an arm record"; cat "$arms"; exit 1; }

# Test 25: the arm log keeps 7 days, not the loss file's 24 h: a canary compares
# a day before the knob with a day after, so it needs both days on disk. (It is
# rewritten only once its oldest row is a day past the window, so a busy host
# does not rewrite it on every arm, on the card that is the bottleneck.)
reset_1693
mkdir -p "$CLAUDLOBBY_ROOT/state/plane"
now=$(date +%s)
printf '%s\thook\t1.0\t5\tbot:f/old\ttimeout\n%s\thook\t1.0\t5\tbot:f/kept\ttimeout\n' "$((now - 9 * 86400))" "$((now - 2 * 86400))" > "$arms"
printf '%s' "$batch" | PLANE_EMIT_CLASS=hook PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>/dev/null || true
grep -q "bot:f/old" "$arms" && { echo "FAIL(25): a 9-day-old arm survived rotation"; cat "$arms"; exit 1; }
grep -q "bot:f/kept" "$arms" || { echo "FAIL(25): a 2-day-old arm was rotated away -- the canary loses its before-window"; cat "$arms"; exit 1; }
[ "$(wc -l < "$arms")" -eq 2 ] || { echo "FAIL(25): want the kept row + the new one"; cat "$arms"; exit 1; }
reset_1693

echo "PASS: all plane-emit shim tests passed"
