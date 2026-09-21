#!/usr/bin/env python3
"""Daemon-side passive sampler for the #1693 canary (vera's Checks 2 and 4).

otis's instrument, unchanged in METHOD — `/proc/PID/syscall` says what the
process is executing right now, which is a direct observation rather than an
inference — but in Python rather than a shell loop. That is not a style
preference: a bash sampler spawning `date` and `cut` per iteration measured
**25 samples in 45 seconds** where 50 Hz wants ~2,250. An instrument that
cannot keep its own sample rate produces a number that looks like a
measurement and is not one, which is the class of defect this whole issue is
about.

Purely passive: reads two procfs files and stats the WAL. Nothing is sent to
the daemon, so the instrument cannot be the load.
"""
from __future__ import annotations

import argparse, json, os, sys, time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--wal", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--hz", type=float, default=50.0)
    ap.add_argument("--seconds", type=float, default=120.0)
    a = ap.parse_args()

    period = 1.0 / a.hz
    end = time.monotonic() + a.seconds
    sc_path = f"/proc/{a.pid}/syscall"
    samples, wal = [], []
    next_wal = 0.0
    while time.monotonic() < end:
        t = time.monotonic_ns()
        # A PARSE failure and a GONE process are different conditions and must
        # not share a handler. The first build caught ValueError alongside
        # OSError and broke the loop on it -- and `/proc/PID/syscall` reads
        # "running\n" whenever the target is on-CPU rather than in a syscall,
        # so the very first such sample ended the run. It reported 7 samples in
        # 60s as a completed measurement: a check that stopped operating
        # returning its negative verdict, which is the class of defect this
        # whole harness exists to catch.
        try:
            with open(sc_path) as f:
                first = f.read().strip().split(" ", 1)[0]
        except OSError:
            break                                  # the daemon is gone; stop cleanly
        try:
            samples.append((t, int(first)))        # decimal syscall number
        except ValueError:
            samples.append((t, -1))                # "running" / unparseable: on-CPU

        now = time.monotonic()
        if now >= next_wal:
            try: wal.append((t, os.path.getsize(a.wal)))
            except OSError: wal.append((t, 0))
            next_wal = now + 1.0
        slack = period - (time.monotonic_ns() - t) / 1e9
        if slack > 0:
            time.sleep(slack)

    json.dump({"samples": samples, "wal": wal}, open(a.out, "w"))
    print(json.dumps({"n_samples": len(samples), "n_wal": len(wal),
                      "seconds": a.seconds,
                      "achieved_hz": round(len(samples) / a.seconds, 1) if a.seconds else 0}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
