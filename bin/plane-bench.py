#!/usr/bin/env python3
"""plane-bench: cold/warm emit latency + burst behavior (design v2 §14).

Usage: ./bin/plane-bench.py [--root DIR] [--cold N] [--warm N] [--burst N]
Writes a fresh throwaway db under --root (default: a mkdtemp), prints a
markdown results block to paste into the Phase-2 plan.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _request(i: int) -> dict:
    return {
        "event_type": "task",
        "emitter": "bench",
        "fleet": "bench-fleet",
        "payload": {
            "work_item_id": "wi_" + f"{i:032x}",
            "event": "progress",
            "progress": i % 100,
        },
    }


def _pctl(xs: list[float], p: float) -> float:
    # Nearest-rank: ceil(p% of n) as a 1-based rank. The round() form read
    # one sample high (post-review fix; informational metrics only — the
    # gated reads compute their median inline).
    xs = sorted(xs)
    rank = -(-int(p) * len(xs) // 100)          # ceil without math import
    return xs[max(0, rank - 1)]


def bench_cold(root: Path, n: int) -> list[float]:
    """Full subprocess spawn per emit — what a bash door pays."""
    out = []
    for i in range(n):
        payload = json.dumps(_request(i))
        t0 = time.perf_counter()
        r = subprocess.run(
            [sys.executable, "-m", "claudlobby", "--root", str(root),
             "emit", "task", "--json", "-"],
            input=payload, capture_output=True, text=True, cwd=REPO,
        )
        dt = time.perf_counter() - t0
        if r.returncode != 0:
            print(f"cold emit {i} failed rc={r.returncode}: {r.stderr}", file=sys.stderr)
            continue
        out.append(dt)
    return out


def bench_warm(root: Path, n: int) -> list[float]:
    """In-process emit — what a resident daemon would pay per event."""
    from claudlobby.plane.emit_api import emit
    out = []
    for i in range(n):
        t0 = time.perf_counter()
        emit(root, _request(100_000 + i))
        out.append(time.perf_counter() - t0)
    return out


def _burst_worker(root_str: str, i: int, q) -> None:
    # Module-level: multiprocessing spawn (macOS default) must pickle the
    # worker — a nested closure is not spawn-safe (round-2 F7).
    from claudlobby.plane.emit_api import emit as _emit

    try:
        o = _emit(Path(root_str), _request(200_000 + i))
        q.put(o.status)
    except Exception as exc:  # noqa: BLE001
        q.put(f"error:{exc}")


def _seed_realistic(root: Path, n_items: int = 400) -> None:
    """Dispatch triples + transmissions + task histories through emit_batch,
    plus workstream events seeded directly (that family's door is Phase 2b —
    the bench needs the ROWS, not the door)."""
    from claudlobby.plane.db import connect, db_path
    from claudlobby.plane.emit_api import emit_batch
    from claudlobby.plane.ids import (
        mint_assignment_id, mint_msg_id, mint_work_item_id,
    )

    for i in range(n_items):
        wi, asg, msg = mint_work_item_id(), mint_assignment_id(), mint_msg_id()
        who = f"bot:bench-fleet/w{i % 20}"
        batch = [
            {"event_type": "work_item", "emitter": "bench",
             "fleet": "bench-fleet",
             "payload": {"work_item_id": wi, "title": f"objective {i}",
                          "created_by": "bot:bench-fleet/mgr"}},
            {"event_type": "assignment", "emitter": "bench",
             "fleet": "bench-fleet",
             "payload": {"assignment_id": asg, "work_item_id": wi,
                          "assignee": who, "assigned_by": "bot:bench-fleet/mgr",
                          "expected_by": "2026-01-01T00:00:00+00:00",
                          "dispatch_msg_id": msg}},
            {"event_type": "communication", "emitter": "bench",
             "fleet": "bench-fleet",
             "payload": {"msg_id": msg, "sender": "bot:bench-fleet/mgr",
                          "recipient": who, "message_class": "task_request",
                          "command_type": "task", "work_item_id": wi,
                          "assignment_id": asg, "body": "x" * 400,
                          "privacy": "full"}},
            {"event_type": "transmission", "emitter": "bench",
             "fleet": "bench-fleet",
             "payload": {"msg_id": msg, "attempt_no": 1, "carrier": "tmux",
                          "destination": "sock", "state": "pane_submitted"}},
        ]
        if i % 3:
            batch.append({"event_type": "transmission", "emitter": "bench",
                          "fleet": "bench-fleet",
                          "payload": {"msg_id": msg, "attempt_no": 1,
                                       "carrier": "tmux", "destination": "sock",
                                       "state": "recipient_acknowledged"}})
        for p_ in range(i % 4):
            batch.append({"event_type": "task", "emitter": "bench",
                          "fleet": "bench-fleet",
                          "payload": {"work_item_id": wi, "assignment_id": asg,
                                       "event": "progress",
                                       "progress": 25 * (p_ + 1),
                                       "summary": "s" * 200, "actor": who}})
        if i % 5 == 0:
            batch.append({"event_type": "task", "emitter": "bench",
                          "fleet": "bench-fleet",
                          "payload": {"work_item_id": wi, "assignment_id": asg,
                                       "event": "completed", "actor": who}})
        emit_batch(root, batch)
    conn = connect(db_path(root))
    conn.execute("BEGIN IMMEDIATE")
    for w in range(40):   # the workstream CONSTRUCTS (door is Phase 2b; direct SQL)
        cur = conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES (?, 'workstream', 't')", (f"ev_wc{w:030x}",))
        conn.execute(
            "INSERT INTO workstreams (ingest_seq, event_id, schema_version,"
            " occurred_at, ingested_at, host_uid, emitter, workstream_id,"
            " title, opened_by_uid) VALUES (?, ?, '1',"
            " '2026-01-01T00:00:00+00:00', 't', 'h', 'bench', ?, ?, 'actor_b')",
            (cur.lastrowid, f"ev_wc{w:030x}", f"ws-{w}", f"campaign {w}"))
    for j in range(1000):
        cur = conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES (?, 'workstream_event', 't')", (f"ev_ws{j:030x}",))
        conn.execute(
            "INSERT INTO events (ingest_seq, event_id, schema_version,"
            " occurred_at, ingested_at, host_uid, emitter, kind, event,"
            " workstream_id, renewed_until, detail) VALUES (?, ?, '1',"
            " '2026-05-01T00:00:00+00:00', 't', 'h', 'bench', 'workstream',"
            " ?, ?, ?, ?)",
            (cur.lastrowid, f"ev_ws{j:030x}",
             "renewed" if j % 11 == 0 else ("progressed" if j % 7 else "blocked"),
             f"ws-{j % 40}",
             "2099-01-01T00:00:00+00:00" if j % 11 == 0 else None,
             '{"note": "' + "n" * 120 + '"}'))
    conn.execute("COMMIT")
    conn.close()


def bench_reads(root: Path) -> None:
    """Round-3 F7: the four DERIVATION-shaped reads, on realistic mixed
    history, with EXPLAIN QUERY PLAN. Pi gate thresholds printed with the
    numbers: p50 <= 50ms per query at this seed AND no un-indexed full scan
    of events — else the F16-v2 flip condition is on the table."""
    from claudlobby.plane.db import connect, db_path

    _seed_realistic(root)
    conn = connect(db_path(root))
    # ONE definition (round-6 F7): the timed SQL is the fixture-tested SQL.
    from claudlobby.plane.queries import (
        ATTENTION_SQL,
        RECONCILIATION_SQL,
        TASK_STATUS_SQL,
        WORKSTREAM_STATUS_SQL,
        events_aliases,
        is_bare_events_scan,
    )

    QUERIES = {
        "attention: unacked or overdue OPEN assignments": ATTENTION_SQL,
        "task-status: per-assignment, terminal-dominant": TASK_STATUS_SQL,
        "workstream-status: contract x events x clock x policy": WORKSTREAM_STATUS_SQL,
        "reconciliation: submitted-not-acked transmissions": RECONCILIATION_SQL,
    }
    # Binds are CLOCK x POLICY (round-6 F7): renewal horizons vs NOW,
    # activity vs CUTOFF = now − policy_window; attention overdue vs NOW.
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    PARAMS = {
        "attention: unacked or overdue OPEN assignments": (now,),
        "workstream-status: contract x events x clock x policy": (now, cutoff),
    }
    failures = []
    print("\n### read benchmarks (GATE, machine-checked: p50 <= 50ms each;"
          " EQP must never show a bare 'SCAN events')")
    for name, sql in QUERIES.items():
        params = PARAMS.get(name, ())
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            conn.execute(sql, params).fetchall()
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        p50 = times[2]
        plans = [r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
        aliases = events_aliases(sql)
        bare_scan = any(is_bare_events_scan(d.strip(), aliases) for d in plans)
        verdict = "PASS" if (p50 <= 50.0 and not bare_scan) else "FAIL"
        if verdict == "FAIL":
            failures.append(name)
        print(f"- [{verdict}] {name}: p50={p50:.1f}ms max={times[-1]:.1f}ms")
        for d in plans:
            print(f"    EQP: {d}")
    conn.close()
    if failures:
        print(f"\nGATE FAILED ({len(failures)}): " + "; ".join(failures))
        print("On the Pi this formally reopens the F16-v2 flip condition.")
        return 1
    print("\nGATE PASSED")
    return 0


def bench_burst(root: Path, n: int) -> dict:
    import multiprocessing as mp

    q: mp.Queue = mp.Queue()
    t0 = time.perf_counter()
    procs = [mp.Process(target=_burst_worker, args=(str(root), i, q)) for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
    wall = time.perf_counter() - t0
    results = [q.get(timeout=5) for _ in range(n)]
    return {
        "wall_s": round(wall, 2),
        "committed": results.count("committed"),
        "spooled": results.count("spooled"),
        "errors": [r for r in results if str(r).startswith("error:")],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--cold", type=int, default=50)
    ap.add_argument("--warm", type=int, default=1000)
    ap.add_argument("--burst", type=int, default=25)
    args = ap.parse_args()
    root = args.root or Path(tempfile.mkdtemp(prefix="plane-bench-"))

    cold = bench_cold(root, args.cold)
    warm = bench_warm(root, args.warm)
    burst = bench_burst(root, args.burst)
    read_gate = bench_reads(root)

    print("## plane-bench results\n")
    print(f"- host: `{__import__('platform').node()}` "
          f"({__import__('platform').machine()}), python {sys.version.split()[0]}")
    for name, xs in (("cold (subprocess)", cold), ("warm (in-process)", warm)):
        ms = [x * 1000 for x in xs]
        print(f"- {name}: n={len(ms)} p50={_pctl(ms, 50):.1f}ms "
              f"p95={_pctl(ms, 95):.1f}ms max={max(ms):.1f}ms "
              f"mean={statistics.mean(ms):.1f}ms")
    print(f"- burst n={args.burst}: wall={burst['wall_s']}s "
          f"committed={burst['committed']} spooled={burst['spooled']} "
          f"errors={len(burst['errors'])}")
    print("\nGate (Phase-2 ingest choice): Pi cold p95 ≤ 300ms AND burst errors == 0 → direct writer; else socket daemon.")
    return read_gate


if __name__ == "__main__":
    raise SystemExit(main())
