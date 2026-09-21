#!/bin/bash
# rehearse-plane-durability.sh — the #1693 canary. THE INSTRUMENT, NOT THE FIX.
#
# vera's four checks, as adopted: loss-witness, WAL-bound, durability-window,
# service-time. It exists to GATE a change to the daemon's checkpoint
# behaviour, and its first job is to prove it can see the failure at all —
# which is why the control run against UNCHANGED code is the deliverable
# before any fix is written. A canary that has not demonstrated it can detect
# the failure is not evidence.
#
# TWO DESIGN POINTS ARE BINDING (dara, #1693), not advisory:
#
#   * THE WITNESS LIVES OUTSIDE THE PLANE. Loss is established from the
#     CLIENT's own log (lib/plane-durability-driver.py), written the instant a
#     reply arrives. Never a query asking the plane whether the plane lost
#     something: if the plane is what is lossy, its own answer is worthless.
#     The ledger is read afterwards, read-only, only to find which acknowledged
#     ids are ABSENT from it.
#   * THE TRAFFIC IS ACCEPTED. A refused request returns before
#     `emit_batch` ever calls connect(), so it cannot pay the checkpoint. The
#     issue's original p95 came from refusals and is struck; re-running it
#     after a fix would show success whether or not the fix worked.
#
# SAFETY IS STRUCTURAL AND ASSERTED, NOT INTENDED. This harness SIGKILLs a
# daemon and deliberately tries to lose data. Every path it touches derives
# from its own disposable root (the db and the socket are BOTH functions of
# `root`, so one private root isolates both), and `_assert_disposable` REFUSES
# rather than warns — checked against the real root's real db path, so the
# check is on the PATH rather than on the operator having remembered which
# instance they meant. A harness that silently fell back to the production
# plane would pass by coincidence and destroy the estate's only record; that is
# the one failure here with no recovery. The `rehearse-env-cascade.sh`
# convention: assert the isolation HELD, never assume it.
#
# Usage: CLAUDLOBBY_ROOT=<checkout> bash lib/rehearse-plane-durability.sh [--seconds N]
# Exit:  0 ran · 1 a check FAILED · 2 precondition/dep missing · 3 isolation refused
set -uo pipefail

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:?set CLAUDLOBBY_ROOT to the checkout under test}"
SRC="$CLAUDLOBBY_ROOT"
SECONDS_SOAK=60
KILL_AFTER_ACKS=40
HOLD_READER=1
while [ $# -gt 0 ]; do
    case "$1" in
        --seconds) SECONDS_SOAK="$2"; shift 2 ;;
        --kill-after) KILL_AFTER_ACKS="$2"; shift 2 ;;
        # dara §4 is an ATTRIBUTION question, so it needs two arms. With the
        # reader held, a WAL that never truncates could be the reader blocking
        # the checkpoint OR the daemon not checkpointing at all; one arm cannot
        # tell those apart, and they have opposite implications for a fix that
        # leans on passive auto-checkpointing.
        --no-reader) HOLD_READER=0; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

[ "$(uname -s)" = "Linux" ] || { echo "Linux only (/proc sampling)" >&2; exit 2; }

WORK="$(mktemp -d)"
ROOT="$WORK/root"
PASS=0; FAIL=0
PY="$SRC/.venv/bin/python"; [ -x "$PY" ] || PY="python3"

say() { printf '%s\n' "$*"; }
ok()  { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }

mkdir -p "$ROOT"
CANARY_DB="$("$PY" -c "from claudlobby.plane.db import db_path;from pathlib import Path;print(db_path(Path('$ROOT')))" 2>/dev/null)"
CANARY_SOCK="$("$PY" -c "from claudlobby.plane.daemon import socket_path;from pathlib import Path;print(socket_path(Path('$ROOT')))" 2>/dev/null)"
REAL_DB="$("$PY" -c "from claudlobby.plane.db import db_file;from pathlib import Path;print(db_file(Path('$SRC')))" 2>/dev/null)"
[ -n "$CANARY_DB" ] && [ -n "$CANARY_SOCK" ] || { echo "cannot resolve canary paths" >&2; exit 2; }

# --- the refusal, on the PATH, before anything destructive ------------------
_assert_disposable() {
    case "$CANARY_DB" in "$WORK"/*) : ;;
        *) echo "REFUSING: canary db '$CANARY_DB' is not under the disposable work dir" >&2; exit 3 ;; esac
    case "$CANARY_SOCK" in "$WORK"/*) : ;;
        *) echo "REFUSING: canary socket '$CANARY_SOCK' is not under the disposable work dir" >&2; exit 3 ;; esac
    if [ -n "$REAL_DB" ] && [ "$CANARY_DB" = "$REAL_DB" ]; then
        echo "REFUSING: canary db is the REAL plane at '$REAL_DB'" >&2; exit 3
    fi
    # The estate's other roots, not just this checkout's: a harness that killed
    # a daemon serving ~/claudlobby would be just as unrecoverable.
    for _other in "$HOME/claudlobby" "${CLAUDLOBBY_ROOT:-}" /home/*/claudlobby; do
        [ -e "$_other/state/plane/plane.db" ] || continue
        if [ "$CANARY_DB" = "$_other/state/plane/plane.db" ]; then
            echo "REFUSING: canary db is a live plane at '$_other'" >&2; exit 3
        fi
    done
}
_assert_disposable

DAEMON_PID=""
RO_PID=""
cleanup() {
    [ -n "$DAEMON_PID" ] && kill -9 "$DAEMON_PID" 2>/dev/null
    [ -n "$RO_PID" ] && kill "$RO_PID" 2>/dev/null
    pkill -f "plane-durability-driver.py --socket $CANARY_SOCK" 2>/dev/null
    case "$WORK" in /tmp/*) rm -rf "$WORK" ;; esac
}
trap cleanup EXIT INT TERM

_start_daemon() {
    "$PY" -m claudlobby --root "$ROOT" plane serve >>"$WORK/daemon.log" 2>&1 &
    DAEMON_PID=$!
    local i
    for i in $(seq 80); do [ -S "$CANARY_SOCK" ] && return 0; sleep 0.25; done
    return 1
}

say "=== #1693 canary — CONTROL RUN (unchanged code) ==="
say "checkout : $SRC"
say "work dir : $WORK"
say "canary db: $CANARY_DB"
say "real db  : ${REAL_DB:-<none>}"

_start_daemon || { echo "daemon did not come up" >&2; tail -5 "$WORK/daemon.log" >&2; exit 2; }

# --- isolation ASSERTED against the running process, not assumed ------------
# /proc/PID/cmdline is what the kernel says this pid was started with; the
# pre-flight path check above proves the paths, this proves the process we are
# about to SIGKILL is the one using them.
_cmd="$(tr '\0' ' ' < "/proc/$DAEMON_PID/cmdline" 2>/dev/null)"
case "$_cmd" in
    *"--root $ROOT"*) ok "isolation: the daemon we will kill was started on the canary root" ;;
    *) bad "isolation: daemon cmdline does not name the canary root: $_cmd"
       echo "REFUSING to continue" >&2; exit 3 ;;
esac
if [ -n "$REAL_DB" ] && [ -e "$REAL_DB" ]; then
    _real_before="$(stat -c %Y "$REAL_DB" 2>/dev/null)"
fi

# dara §4: a read-only connection is held for the WHOLE soak, because
# `plane doctor`, `plane view` and `brief` all hold one during normal
# operation, and SQLite's passive auto-checkpoint may skip while a reader holds
# a snapshot it needs to pass. If a fix comes to depend on passive
# checkpointing, a routine read tool sitting in the way is a fleet-operational
# property, and it gets answered here rather than discovered later.
if [ "$HOLD_READER" -eq 1 ]; then
"$PY" - "$CANARY_DB" >>"$WORK/ro.log" 2>&1 <<'ROEOF' &
import sqlite3, sys, time
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=5)
conn.execute("BEGIN")
conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()   # hold a snapshot
while True:
    time.sleep(1)
ROEOF
RO_PID=$!
say "  reader arm: a mode=ro snapshot is HELD for the whole soak (dara §4)"
else
say "  reader arm: NO reader held (control arm for the §4 attribution)"
fi

# --- Checks 2 and 4: ONE passive sampler, at a rate it can actually hold -----
# A shell loop spawning `date` and `cut` per iteration achieved 25 samples in
# 45s where 50Hz wants ~2250 -- it reported a p95 off one busy run and looked
# like a result. The sampler now reports its ACHIEVED rate so a starved run
# cannot be read as a measurement.
"$PY" "$SRC/lib/plane-canary-sampler.py" --pid "$DAEMON_PID" \
    --wal "${CANARY_DB}-wal" --out "$WORK/samples.json" \
    --hz 50 --seconds "$SECONDS_SOAK" > "$WORK/sampler.json" 2>"$WORK/sampler.err" &
SYS_PID=$!
WAL_PID=""

say ""
say "--- soak: ${SECONDS_SOAK}s of ACCEPTED traffic (refusals cannot pay the checkpoint) ---"
"$PY" "$SRC/lib/plane-durability-driver.py" --socket "$CANARY_SOCK" \
    --witness "$WORK/witness.log" --seconds "$SECONDS_SOAK" > "$WORK/driver.json" 2>"$WORK/driver.err" &
DRIVER_PID=$!

# --- Check 1 + 3: kill just after an ack ------------------------------------
# vera: land the kill AFTER a batch was acknowledged and BEFORE the next
# checkpoint would be expected. Against per-batch-close code every ack is
# already checkpointed, so this should show ZERO loss -- that expectation is
# the control, and a nonzero result here means the harness is wrong OR the
# guarantee does not hold, which need opposite responses.
# The kill lands LATE in the soak, not after the first N acks: killing early
# truncates the very soak Checks 2 and 4 depend on, and the first build did
# exactly that (45s requested, ~4s measured). It still lands "just after an
# ack" -- the driver is mid-stream and acking continuously at this point.
_kill_at=$(awk "BEGIN{d=$SECONDS_SOAK-3; if(d<1)d=1; print d}")
sleep "$_kill_at"
_acks=$(grep -c ' ok ' "$WORK/witness.log" 2>/dev/null | tr -d '\n ' || printf 0)
[ -n "$_acks" ] || _acks=0
KILL_NS=$(date +%s%N)
kill -9 "$DAEMON_PID" 2>/dev/null
say "  SIGKILL sent after $_acks acknowledged events"
wait "$DRIVER_PID" 2>/dev/null
# WAIT for the sampler, never kill it: it exits on its own the moment the
# daemon's procfs entry disappears, and it writes its samples on the way out.
# Killing it raced that write and left a TRUNCATED samples.json -- which the
# analysis below then failed to parse while the harness still printed FAIL=0.
wait "$SYS_PID" 2>/dev/null
kill ${WAL_PID:+"$WAL_PID"} ${RO_PID:+"$RO_PID"} 2>/dev/null; RO_PID=""

# --- verdicts ---------------------------------------------------------------
say ""
say "--- Check 1: loss witness (client's log vs the ledger, read-only) ---"
"$PY" - "$CANARY_DB" "$WORK/witness.log" "$WORK/samples.json" <<'VEOF' > "$WORK/verdict.json"
import json, sqlite3, sys
db, witness, samples_path = sys.argv[1:4]
acked = []
for line in open(witness):
    p = line.split()
    if len(p) >= 3 and p[2] == "ok":
        acked.append(p[1])
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
have = {r[0] for r in conn.execute("SELECT event_id FROM ingest_ledger")}
lost = [e for e in acked if e not in have]
# Unreadable samples are a REFUSAL, not an empty list. Defaulting to [] made
# max() return 0 and the WAL section render "0 bytes" -- a number indistinguishable
# from a genuinely empty WAL, for a file that could not be read at all.
try:
    sizes = [w[1] for w in json.load(open(samples_path))["wal"]]
    sizes_ok = True
except Exception as exc:
    sizes, sizes_ok = [], False
    sizes_err = str(exc)[:120]
out = {"acked": len(acked), "lost": len(lost), "lost_ids": lost[:10],
       "wal_readable": sizes_ok,
       "wal_error": None if sizes_ok else sizes_err,
       "wal_max": max(sizes) if sizes else None, "wal_min": min(sizes) if sizes else None,
       "wal_final": sizes[-1] if sizes else None,
       "wal_collapses_to_zero": sum(1 for i in range(1, len(sizes))
                                    if sizes[i] == 0 and sizes[i-1] > 0) if sizes_ok else None}
print(json.dumps(out))
VEOF
cat "$WORK/verdict.json"
LOST=$("$PY" -c "import json;print(json.load(open('$WORK/verdict.json'))['lost'])")
ACKED=$("$PY" -c "import json;print(json.load(open('$WORK/verdict.json'))['acked'])")
if [ "$ACKED" -eq 0 ]; then
    bad "Check 1: the witness recorded ZERO acknowledgments — the harness saw nothing, so it proves nothing"
elif [ "$LOST" -eq 0 ]; then
    ok "Check 1: $ACKED acknowledged, 0 absent from the ledger (the current guarantee holds)"
else
    bad "Check 1: $LOST of $ACKED acknowledged events are ABSENT from the ledger"
fi

say ""
say "--- Check 4: service time, daemon-side, ACCEPTED traffic ---"
if ! "$PY" - "$WORK/samples.json" "$WORK/sampler.json" <<'SEOF'
import json, sys
rows = json.load(open(sys.argv[1]))["samples"]
try:
    rate = json.load(open(sys.argv[2]))
    print(f"  achieved rate: {rate['achieved_hz']} Hz over {rate['seconds']}s "
          f"({rate['n_samples']} samples)")
    if rate["achieved_hz"] < 25:
        print("  *** SAMPLER STARVED -- below half the requested 50Hz. These")
        print("  *** numbers are NOT a measurement; re-run on a quieter host.")
except Exception:
    print("  achieved rate: UNKNOWN (sampler wrote no summary)")
# A "busy run" is a maximal stretch where the sampler saw the SAME syscall
# continuously -- otis's structure. -1 is "could not read", excluded rather
# than counted as a stall.
runs, cur, start = [], None, None
for ts, sc in rows:
    if sc != cur:
        if cur is not None and cur >= 0 and start is not None:
            runs.append((cur, (ts - start) / 1e6))
        cur, start = sc, ts
runs = [r for r in runs if r[1] >= 50]          # >=50ms, otis's busy-run floor
runs.sort(key=lambda r: r[1])
n = len(runs)
p95 = runs[int(n * 0.95)][1] if n else 0.0
print(f"  samples      : {len(rows)}")
print(f"  busy runs    : {n}  (>=50ms)")
print(f"  p95 duration : {p95:.0f} ms")
print(f"  max duration : {runs[-1][1]:.0f} ms" if n else "  max duration : n/a")
SEOF
then
    # A check that CRASHED must not leave the harness reporting FAIL=0. The
    # first build did exactly that -- a JSONDecodeError traceback printed above
    # a clean PASS=3 FAIL=0 -- which is a gate scoring clean on its own failure.
    bad "Check 4: the service-time analysis did not complete (see traceback above)"
else
    ok "Check 4: the service-time analysis completed"
fi

say ""
say "--- Check 2: WAL bound ---"
if "$PY" -c "
import json,sys; v=json.load(open('$WORK/verdict.json'))
if not v['wal_readable']:
    print('  UNREADABLE — the WAL samples could not be parsed:', v['wal_error'])
    print('  This is NOT \'the WAL stayed at 0\'. No bound is measured.')
    sys.exit(1)
print(f\"  wal max      : {v['wal_max']} bytes\")
print(f\"  wal final    : {v['wal_final']} bytes\")
print(f\"  collapses->0 : {v['wal_collapses_to_zero']}  (TRUNCATE checkpoint fingerprint)\")
"; then ok "Check 2: WAL bound measured"; else bad "Check 2: WAL samples unreadable — no bound measured"; fi

say ""
say "--- Check 3: durability window ---"
say "  Against per-batch-close code the window is expected to be ZERO: the"
say "  checkpoint happens inside the call that produces the outcomes, so an"
say "  ok:True is already fsync'd. Check 1's loss count IS this measurement."
say "  A fix that widens it must state the worst case in wall-clock terms."
say ""
say "  BOUND — what SIGKILL can and cannot establish. Killing the daemon tests"
say "  durability against DAEMON death: the WAL is a file, and its contents"
say "  survive the process regardless of whether anything was fsync'd. The"
say "  ruling on this issue is about ok:True surviving HOST death, which is"
say "  what an fsync buys and what this harness does NOT simulate. So a zero"
say "  in Check 1 means NO ACKNOWLEDGED EVENT IS LOST WHEN THE DAEMON DIES."
say "  It is not evidence about power loss, and must not be cited as if it"
say "  were. Testing that needs a host-level fault (power cut or a VM snapshot"
say "  discarding the page cache) and is named here as a gap, not guessed past."

# --- the isolation held, asserted after the fact ---------------------------
say ""
if [ -n "${_real_before:-}" ]; then
    _real_after="$(stat -c %Y "$REAL_DB" 2>/dev/null)"
    if [ "$_real_before" = "$_real_after" ]; then
        ok "isolation: the real plane's mtime is unchanged ($REAL_DB)"
    else
        bad "isolation: the REAL plane was written during this run"
    fi
else
    ok "isolation: no real plane exists at this checkout to disturb"
fi

say ""
say "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
