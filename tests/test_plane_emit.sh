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
export RECORDER_LOG="$tmpdir/rec.log" RECORDER_COPY="$tmpdir/rec.json"

# --- fake daemon: replies with $RESP_FILE content per connection -------------
fake_daemon="$tmpdir/faked.py"
cat > "$fake_daemon" <<'PY'
import socket, sys
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sys.argv[1])
srv.listen(8)
resp = open(sys.argv[2], "rb").read()
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
    c.sendall(resp)
    c.close()
PY

start_daemon() {  # $1=response-json
    printf '%s\n' "$1" > "$tmpdir/resp.json"
    rm -f "$sockdir/s" "$sockdir/s.ready"
    python3 "$fake_daemon" "$sockdir/s" "$tmpdir/resp.json" "$tmpdir/seen.jsonl" &
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

echo "PASS: all plane-emit shim tests passed"
