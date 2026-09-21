#!/usr/bin/env python3
"""The #1693 canary's COMPARATOR, extracted so it can be proven to fire.

It was an inline heredoc inside the harness, which made it untestable in
isolation — and that mattered more than it looked. Under per-batch-close every
acknowledgment is already checkpointed before the reply is sent, so `lost == 0`
was **guaranteed before the harness ever ran**: a comparator that always says
"not lost" and one that correctly says "not lost because nothing was lost"
produce byte-identical output on that run.

So the control validates that the harness has **no false positives** — it does
not cry wolf against a clean run. It says nothing about **false negatives**,
which is the property that actually matters for a gate. Those are different
things, and the #1693 spec (and this harness's first version) conflated them by
calling the control "the falsification test".

`self_test()` closes that: it feeds the comparator a witness holding a known
acknowledged id and a `have` set built from the real ledger MINUS that id, and
requires the comparator to name exactly it. The harness runs this BEFORE
reporting any control result, so no run is ever published by a detector that
has not just demonstrated it fires.
"""

from __future__ import annotations

import argparse, json, sqlite3, sys


def acked_ids(witness_path: str) -> list[str]:
    """Ids the CLIENT recorded as acknowledged — its own log, never the plane."""
    out = []
    for line in open(witness_path):
        p = line.split()
        if len(p) >= 3 and p[2] == "ok":
            out.append(p[1])
    return out


def compare(acked: list[str], have: set[str]) -> list[str]:
    """Acknowledged ids ABSENT from the ledger. The whole verdict."""
    return [e for e in acked if e not in have]


def ledger_ids(db: str) -> set[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT event_id FROM ingest_ledger")}
    finally:
        conn.close()


def self_test(acked: list[str], have: set[str]) -> tuple[bool, str]:
    """Prove the comparator FIRES, by injecting a loss it must detect.

    Injected at the comparator, not the daemon: no second SIGKILL, and the
    property under test is the detector rather than the system.

    Two assertions, because one is not enough. The comparator must NAME the
    withheld id (it fires), and it must name ONLY that id (it does not fire on
    everything, which a comparator returning its whole input would also do
    while passing the first assertion alone).
    """
    if not acked:
        return False, "no acknowledged ids available to inject with"
    victim = acked[0]
    doctored = set(have) - {victim}
    lost = compare(acked, doctored)
    if victim not in lost:
        return False, f"injected loss of {victim} was NOT detected"
    extra = [e for e in lost if e != victim and e in have]
    if extra:
        return False, f"comparator also named ids that are present: {extra[:3]}"
    return True, f"injected loss of {victim} detected, and only it"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--witness", required=True)
    ap.add_argument("--samples")
    a = ap.parse_args()

    acked = acked_ids(a.witness)
    have = ledger_ids(a.db)
    fired, detail = self_test(acked, have)
    lost = compare(acked, have)

    sizes, sizes_ok, sizes_err = [], True, None
    if a.samples:
        try:
            sizes = [w[1] for w in json.load(open(a.samples))["wal"]]
        except Exception as exc:
            sizes_ok, sizes_err = False, str(exc)[:120]

    print(json.dumps({
        "acked": len(acked), "lost": len(lost), "lost_ids": lost[:10],
        "comparator_fires": fired, "comparator_detail": detail,
        "wal_readable": sizes_ok, "wal_error": sizes_err,
        "wal_max": max(sizes) if sizes else None,
        "wal_min": min(sizes) if sizes else None,
        "wal_final": sizes[-1] if sizes else None,
        "wal_collapses_to_zero": (sum(1 for i in range(1, len(sizes))
                                      if sizes[i] == 0 and sizes[i-1] > 0)
                                  if sizes_ok else None),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
