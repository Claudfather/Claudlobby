#!/bin/bash
# Hermetic shim checks: committed socket ACK versus durable stage pending,
# refusal, cooldown, and pre-minted replay IDs. The fake daemon uses a short
# private socket; a recorder is a tripwire against cold full-CLI invocation.
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
import json, socket, sys, time
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
        reply = json.loads(resp)
        if reply.get("ok") and buf:
            events = json.loads(buf)["events"]
            for result, event in zip(reply.get("results", []), events):
                result["event_id"] = event["event_id"]
        c.sendall(json.dumps(reply).encode() + b"\n")
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

# Disabled isolation: no socket, subprocess, staged batch, or output.
out=$(printf '%s' "$batch" | PLANE_EMIT_DISABLED=1 PLANE_EMIT_CLI="$recorder" \
      PLANE_SOCKET="$sockdir/s" bash "$SHIM" 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && [ ! -e "$RECORDER_LOG" ] || {
    echo "FAIL(disabled): rc=$rc output=$out"; exit 1;
}

# A daemon acknowledgement alone is committed (rc 0).
start_daemon '{"ok": true, "results": [{"event_id": "ev_11111111111111111111111111111111", "status": "committed"}]}'
out=$(printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/s" bash "$SHIM" 2>"$tmpdir/committed.err"); rc=$?
stop_daemon
[ "$rc" -eq 0 ] && echo "$out" | grep -q '^ev_[0-9a-f]*$' || {
    echo "FAIL(committed): rc=$rc output=$out"; exit 1;
}
[ ! -e "$RECORDER_LOG" ] || { echo "FAIL(committed): cold CLI ran"; exit 1; }
grep -q '"event_id"' "$tmpdir/seen.jsonl" || { echo "FAIL(committed): daemon saw no pre-minted id"; exit 1; }

# An ok-shaped but empty reply is not proof that this batch committed.
start_daemon '{"ok": true, "results": []}'
rc=0
printf '%s' "$batch" | PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/empty-ack.err" || rc=$?
stop_daemon
[ "$rc" -eq 6 ] || { echo "FAIL(empty-ack): rc=$rc falsely claimed commit"; exit 1; }
grep -q 'invalid daemon acknowledgement' "$tmpdir/empty-ack.err" || {
    echo "FAIL(empty-ack): malformed reply undisclosed"; exit 1;
}
rm -f "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
rm -rf "$CLAUDLOBBY_ROOT/state/plane/staged"

# Daemon down: raw staged envelope, 0600 and pending rc 6.
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLI="$recorder" PLANE_SOCKET="$sockdir/absent" bash "$SHIM" \
    >"$tmpdir/down.out" 2>"$tmpdir/down.err" || rc=$?
[ "$rc" -eq 6 ] || { echo "FAIL(down): rc=$rc"; cat "$tmpdir/down.err"; exit 1; }
[ ! -e "$RECORDER_LOG" ] || { echo "FAIL(down): full CLI ran"; exit 1; }
staged="$CLAUDLOBBY_ROOT/state/plane/staged"
[ "$(find "$staged" -name '*.batch' | wc -l)" -eq 1 ] || { echo "FAIL(down): no single staged batch"; exit 1; }
first=$(find "$staged" -name '*.batch' -print -quit)
python3 - "$first" <<'PY_CHECK'
import json, os, stat, sys
entry = json.load(open(sys.argv[1]))
assert stat.S_IMODE(os.stat(sys.argv[1]).st_mode) == 0o600
assert len(entry['events']) == 1
assert entry['events'][0]['event_id'].startswith('ev_')
assert entry['events'][0]['occurred_at']
PY_CHECK
grep -q 'NOT in the plane' "$tmpdir/down.err" || { echo "FAIL(down): pending result undisclosed"; exit 1; }

# A contract verdict is not laundered into a staged batch.
rm -f "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
start_daemon '{"ok": false, "code": "contract_violation", "error": "bad message_class"}'
rc=0
printf '%s' "$batch" | PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/contract.err" || rc=$?
stop_daemon
[ "$rc" -eq 2 ] && [ "$(find "$staged" -name '*.batch' | wc -l)" -eq 1 ] || {
    echo "FAIL(contract): rc=$rc"; exit 1;
}

# An unwriteable staged queue must be total failure, not a pending receipt.
rm -f "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
mkdir -p "$tmpdir/bad-root/state/plane"
printf x > "$tmpdir/bad-root/state/plane/staged"
rc=0
printf '%s' "$batch" | CLAUDLOBBY_ROOT="$tmpdir/bad-root" PLANE_SOCKET="$sockdir/absent" \
    bash "$SHIM" >/dev/null 2>"$tmpdir/no-spool.err" || rc=$?
[ "$rc" -eq 3 ] || { echo "FAIL(no-spool): rc=$rc"; cat "$tmpdir/no-spool.err"; exit 1; }
grep -q 'NOT recorded' "$tmpdir/no-spool.err" || { echo "FAIL(no-spool): failure undisclosed"; exit 1; }

# Malformed input is a verdict before any staged write, including cooldown.
rc=0
printf 'not json' | PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>"$tmpdir/bad.err" || rc=$?
[ "$rc" -eq 2 ] || { echo "FAIL(bad-input): rc=$rc"; exit 1; }

# Stale daemon code does not drop telemetry. It stages the exact pre-minted
# id sent to that daemon and marks the socket cooldown for a later retry.
rm -f "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
start_daemon '{"ok": false, "code": "downgrade", "error": "older code"}'
rc=0
printf '%s' "$batch" | PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/downgrade.err" || rc=$?
stop_daemon
[ "$rc" -eq 6 ] || { echo "FAIL(downgrade): rc=$rc"; cat "$tmpdir/downgrade.err"; exit 1; }
seen_id=$(grep -o '"event_id": "ev_[0-9a-f]*"' "$tmpdir/seen.jsonl" | tail -1 | sed 's/.*"\(ev_[0-9a-f]*\)"/\1/')
grep -q "\"$seen_id\"" "$staged"/*.batch || { echo "FAIL(downgrade): staged id differs from daemon-sent id"; exit 1; }
[ -e "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged" ] || { echo "FAIL(downgrade): cooldown not armed"; exit 1; }
[ "$(tail -1 "$CLAUDLOBBY_ROOT/state/plane/.socket-arms" | cut -f6)" = downgrade ] || {
    echo "FAIL(downgrade): arm cause missing"; exit 1;
}

# During cooldown no socket attempt is made, even when one is live; the batch
# is durable pending, not misreported as a commit.
start_daemon '{"ok": true, "results": [{"event_id": "ev_22222222222222222222222222222222", "status": "committed"}]}'
seen_before=$(wc -l < "$tmpdir/seen.jsonl")
rc=0
printf '%s' "$batch" | PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/cooldown.err" || rc=$?
stop_daemon
[ "$rc" -eq 6 ] && [ "$(wc -l < "$tmpdir/seen.jsonl")" -eq "$seen_before" ] || {
    echo "FAIL(cooldown): rc=$rc or daemon contacted"; exit 1;
}
grep -q 'wedge cooldown' "$tmpdir/cooldown.err" || { echo "FAIL(cooldown): skip undisclosed"; exit 1; }

# A daemon-spooled acknowledgement is pending; a committed one remains 0.
rm -f "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
start_daemon '{"ok": true, "results": [{"event_id": "ev_33333333333333333333333333333333", "status": "spooled"}]}'
rc=0
printf '%s' "$batch" | PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/daemon-spool.err" || rc=$?
stop_daemon
[ "$rc" -eq 6 ] || { echo "FAIL(daemon-spool): rc=$rc"; exit 1; }

# An opted-in cooldown may use the daemon staging handshake, but still rc 6.
staged="$CLAUDLOBBY_ROOT/state/plane/staged"
mkdir -p "$staged"
rm -f "$staged"/*.batch
start_daemon '{"ok": true, "results": []}'
date +%s > "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
rc=0
printf '%s' "$batch" | PLANE_EMIT_COOLDOWN_STAGE=1 PLANE_SOCKET="$sockdir/s" \
    bash "$SHIM" >/dev/null 2>"$tmpdir/staged.err" || rc=$?
stop_daemon
[ "$rc" -eq 6 ] && [ "$(find "$staged" -name '*.batch' | wc -l)" -eq 1 ] || {
    echo "FAIL(stage): rc=$rc"; cat "$tmpdir/staged.err"; exit 1;
}

# Deadline miss is bounded, arms once, and stages without a CLI spawn.
rm -f "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
start_daemon '{"ok": true, "results": []}' 1.5
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLASS=hook PLANE_SOCKET="$sockdir/s" bash "$SHIM" \
    >/dev/null 2>"$tmpdir/slow.err" || rc=$?
stop_daemon
[ "$rc" -eq 6 ] && [ -e "$CLAUDLOBBY_ROOT/state/plane/.socket-wedged" ] || {
    echo "FAIL(deadline): rc=$rc"; cat "$tmpdir/slow.err"; exit 1;
}
grep -q 'transport failed' "$tmpdir/slow.err" || { echo "FAIL(deadline): timeout undisclosed"; exit 1; }
[ ! -e "$RECORDER_LOG" ] || { echo "FAIL(deadline): full CLI ran"; exit 1; }

# The hook's own knob permits a 1.5s daemon; the same knob must not change
# another class's deadline. The preceding miss is the default control.
marker="$CLAUDLOBBY_ROOT/state/plane/.socket-wedged"
arms="$CLAUDLOBBY_ROOT/state/plane/.socket-arms"
rm -f "$marker" "$arms"
start_daemon '{"ok": true, "results": [{"status": "committed"}]}' 1.5
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLASS=hook PLANE_SOCKET_DEADLINE_HOOK_S=4 \
    PLANE_SOCKET="$sockdir/s" bash "$SHIM" >"$tmpdir/slow-ok.out" 2>"$tmpdir/slow-ok.err" || rc=$?
stop_daemon
[ "$rc" -eq 0 ] && [ ! -e "$marker" ] && [ ! -e "$arms" ] || {
    echo "FAIL(class-knob): rc=$rc"; cat "$tmpdir/slow-ok.err"; exit 1;
}
grep -q '^ev_[0-9a-f]*$' "$tmpdir/slow-ok.out" || { echo "FAIL(class-knob): no committed id"; exit 1; }

for cls in door ""; do
    rm -f "$marker" "$arms"
    start_daemon '{"ok": true, "results": [{"status": "committed"}]}' 1.5
    rc=0
    printf '%s' "$batch" | PLANE_EMIT_CLASS="$cls" PLANE_SOCKET_DEADLINE_HOOK_S=4 \
        PLANE_SOCKET="$sockdir/s" bash "$SHIM" >/dev/null 2>"$tmpdir/wrong-class.err" || rc=$?
    stop_daemon
    [ "$rc" -eq 6 ] && [ -e "$marker" ] || {
        echo "FAIL(class-isolation:$cls): rc=$rc"; cat "$tmpdir/wrong-class.err"; exit 1;
    }
done

# Bad deadline knobs are disclosed and ignored, not passed to the client as
# rc2 and thus silently dropped. Unknown class names are also disclosed.
for bad in 3s 0 0.0 inf nan -1 4000 1e3; do
    rm -f "$marker"
    rc=0
    printf '%s' "$batch" | PLANE_EMIT_CLASS=door PLANE_SOCKET_DEADLINE_DOOR_S="$bad" \
        PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>"$tmpdir/bad-knob.err" || rc=$?
    [ "$rc" -eq 6 ] && grep -q 'ignoring PLANE_SOCKET_DEADLINE_DOOR_S' "$tmpdir/bad-knob.err" || {
        echo "FAIL(bad-knob:$bad): rc=$rc"; cat "$tmpdir/bad-knob.err"; exit 1;
    }
done
rm -f "$marker"
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLASS=hooks PLANE_SOCKET="$sockdir/absent" \
    bash "$SHIM" >/dev/null 2>"$tmpdir/bad-class.err" || rc=$?
[ "$rc" -eq 6 ] && grep -q 'unknown PLANE_EMIT_CLASS=hooks' "$tmpdir/bad-class.err" || {
    echo "FAIL(bad-class): rc=$rc"; exit 1;
}

# Every socket miss records who, deadline, and cause. Cooldown emits no arm.
rm -f "$marker" "$arms"
rc=0
printf '%s' "$batch" | PLANE_EMIT_CLASS=hook BOT_ID=b FLEET_NAME=f \
    PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>"$tmpdir/arm.err" || rc=$?
[ "$rc" -eq 6 ] || { echo "FAIL(arm): rc=$rc"; exit 1; }
awk -F'\t' 'NF == 6 && $2 == "hook" && $3 == "1.0" && $5 == "bot:f/b" && $6 == "unreachable" { ok = 1 } END { exit !ok }' "$arms" || {
    echo "FAIL(arm): wrong source/deadline/cause"; cat "$arms"; exit 1;
}
before=$(wc -l < "$arms")
printf '%s' "$batch" | PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>/dev/null || true
[ "$(wc -l < "$arms")" -eq "$before" ] || { echo "FAIL(cooldown-arm): added an arm"; exit 1; }

# Seven-day arm retention preserves a before/after canary window.
rm -f "$marker"
now=$(date +%s)
printf '%s\thook\t1.0\t5\tbot:f/old\ttimeout\n%s\thook\t1.0\t5\tbot:f/kept\ttimeout\n' \
    "$((now - 9 * 86400))" "$((now - 2 * 86400))" > "$arms"
printf '%s' "$batch" | PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>/dev/null || true
grep -q 'bot:f/old' "$arms" && { echo "FAIL(arm-retention): old arm survived"; exit 1; }
grep -q 'bot:f/kept' "$arms" || { echo "FAIL(arm-retention): recent arm lost"; exit 1; }
[ "$(wc -l < "$arms")" -eq 2 ] || { echo "FAIL(arm-retention): wrong count"; exit 1; }

# S5a-04: the stage is capture-policy-applied. Under metadata capture the
# body never reaches disk; the proof triple does.
secret='{"events": [{"event_type": "communication", "emitter": "sh-test", "fleet": "f", "payload": {"msg_id": "msg_00000000000000000000000000000001", "sender": "bot:f/a", "message_class": "notice", "body": "SECRET-PLAN"}}]}'
rm -f "$marker" "$CLAUDLOBBY_ROOT/state/plane/.emit-losses"
rm -rf "$staged"
printf '{"*": "metadata"}' > "$CLAUDLOBBY_ROOT/state/plane/capture.json"
rc=0
printf '%s' "$secret" | PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>"$tmpdir/meta.err" || rc=$?
[ "$rc" -eq 6 ] || { echo "FAIL(metadata-stage): rc=$rc"; cat "$tmpdir/meta.err"; exit 1; }
! grep -q 'SECRET-PLAN' "$staged"/*.batch && grep -q '"body_sha256"' "$staged"/*.batch || {
    echo "FAIL(metadata-stage): body persisted or proof missing"; exit 1;
}

# A broken capture.json refuses a content batch (never stages it raw) and
# counts the loss; it is not a contract verdict about the batch.
rm -f "$marker"; rm -rf "$staged"
printf '{"*": "metdata"}' > "$CLAUDLOBBY_ROOT/state/plane/capture.json"
rc=0
printf '%s' "$secret" | PLANE_EMIT_CLASS=hook PLANE_SOCKET="$sockdir/absent" bash "$SHIM" \
    >/dev/null 2>"$tmpdir/badcap.err" || rc=$?
[ "$rc" -eq 3 ] && [ -z "$(find "$staged" -name '*.batch' 2>/dev/null)" ] || {
    echo "FAIL(bad-capture): rc=$rc"; cat "$tmpdir/badcap.err"; exit 1;
}
grep -q 'capture.json is unreadable or invalid' "$tmpdir/badcap.err" \
    && awk -F'\t' '$2 == "stage_refused" && $3 == "hook" { ok = 1 } END { exit !ok }' \
        "$CLAUDLOBBY_ROOT/state/plane/.emit-losses" || {
    echo "FAIL(bad-capture): refusal undisclosed or uncounted"; exit 1;
}
# The disclosure is authored, never the file's own contents.
! grep -q 'metdata' "$tmpdir/badcap.err" "$CLAUDLOBBY_ROOT/state/plane/.emit-losses" || {
    echo "FAIL(bad-capture): capture.json contents echoed"; exit 1;
}
rm -f "$CLAUDLOBBY_ROOT/state/plane/capture.json"

# A client copied away from claudlobby/plane cannot apply the policy, so it
# refuses (rc 3, counted) rather than staging anything as given.
lonely="$tmpdir/lonely-lib"; mkdir -p "$lonely"
cp "$LIB_DIR/plane-emit.sh" "$LIB_DIR/plane-socket-client.py" "$lonely/"
rm -f "$marker" "$CLAUDLOBBY_ROOT/state/plane/.emit-losses"; rm -rf "$staged"
rc=0
printf '%s' "$batch" | PLANE_SOCKET="$sockdir/absent" bash "$lonely/plane-emit.sh" \
    >/dev/null 2>"$tmpdir/nopolicy.err" || rc=$?
[ "$rc" -eq 3 ] && [ -z "$(find "$staged" -name '*.batch' 2>/dev/null)" ] || {
    echo "FAIL(no-policy): rc=$rc"; cat "$tmpdir/nopolicy.err"; exit 1;
}
grep -q 'capture policy module unavailable' "$tmpdir/nopolicy.err" \
    && awk -F'\t' '$2 == "stage_refused" { ok = 1 } END { exit !ok }' \
        "$CLAUDLOBBY_ROOT/state/plane/.emit-losses" || {
    echo "FAIL(no-policy): refusal undisclosed or uncounted"; exit 1;
}

# S5a-01: the staged queue is bounded. At the byte bound the batch is refused
# (rc 3), disclosed and counted; the queue does not grow.
rm -f "$marker"; mkdir -p "$staged"
python3 -c 'import sys; open(sys.argv[1], "wb").truncate(32 * 1024 * 1024)' "$staged/1-ev_full.batch"
rc=0
printf '%s' "$batch" | PLANE_SOCKET="$sockdir/absent" bash "$SHIM" >/dev/null 2>"$tmpdir/full.err" || rc=$?
[ "$rc" -eq 3 ] && [ "$(find "$staged" -name '*.batch' | wc -l)" -eq 1 ] || {
    echo "FAIL(staged-full): rc=$rc"; cat "$tmpdir/full.err"; exit 1;
}
grep -q 'staged queue FULL' "$tmpdir/full.err" \
    && grep -q $'\tstaged_full\t' "$CLAUDLOBBY_ROOT/state/plane/.emit-losses" || {
    echo "FAIL(staged-full): refusal undisclosed or uncounted"; exit 1;
}
rm -rf "$staged"

echo "PASS: plane-emit committed/pending/refusal paths"
