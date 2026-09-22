#!/usr/bin/env python3
"""The #1693 canary's CLIENT — and its loss witness.

**The witness is this process's own log, never a query to the plane.** If the
plane is what is lossy, asking the plane whether it lost something is worthless
(dara's binding ruling on #1693). So every line here is written by the client,
on the client's side of the socket, the instant a reply arrives — and it is
compared against the ledger only afterwards, read-only, by a different process.

Each line is `<seq> <event_id> <ok|refused|error> <monotonic_ns>`. A line
exists only if a reply was received; an id that is written `ok` and then absent
from `ingest_ledger` is a confirmed loss with no ambiguity, because the record
of the acknowledgment does not depend on the daemon having survived to report
it.

Traffic is ACCEPTED, not refused (dara §1, and vera's Check 4): a refused
request never reaches `connect(...)`, so it cannot pay the checkpoint. The
original benchmark on this issue measured refusals and is struck; rebuilding it
here would be rebuilding the invalid instrument.

The event_id minting is IMPORTED from `lib/plane-socket-client.py`, not
re-implemented — the ids in the witness must be the same ids the real door
would have minted, or the cross-check is comparing two different populations.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import socket
import sys
import time
from pathlib import Path

LIB = Path(__file__).resolve().parent


def _shipped_client():
    """The real client module, for its id minting (never a second copy)."""
    spec = importlib.util.spec_from_file_location(
        "plane_socket_client", LIB / "plane-socket-client.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _event(seq: int, fleet: str) -> dict:
    """One accepted system event, individually distinguishable by `seq`."""
    return {
        "event_type": "system",
        "emitter": "durability-canary",
        "fleet": fleet,
        "payload": {
            "event": "canary_probe",
            "subject_kind": "actor",
            "subject": f"bot:{fleet}/canary",
            "data": {"seq": seq},
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--witness", required=True, help="the client's own log")
    ap.add_argument("--fleet", default="canaryfleet")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="generous on purpose: this harness measures loss, and a "
                         "client-side deadline miss would be recorded as a "
                         "non-ack rather than as the loss under test")
    args = ap.parse_args()

    client = _shipped_client()
    deadline = time.monotonic() + args.seconds
    seq = 0
    sent = acked = refused = errored = 0

    # Line-buffered and flushed per reply: a witness that is still in a stdio
    # buffer when the daemon is SIGKILLed has not witnessed anything, and this
    # process is deliberately running while something nearby is being killed.
    with open(args.witness, "a", buffering=1) as w:
        while time.monotonic() < deadline:
            events = [_event(seq + i, args.fleet) for i in range(args.batch_size)]
            seq += args.batch_size
            finalized = client._finalize(events)
            ids = [e["event_id"] for e in finalized]
            payload = json.dumps({"events": finalized}, ensure_ascii=False)

            t0 = time.monotonic_ns()
            verdict = "error"
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(args.timeout)
                s.connect(args.socket)
                s.sendall(payload.encode() + b"\n")
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                s.close()
                resp = json.loads(buf) if buf.strip() else {}
                verdict = "ok" if resp.get("ok") else "refused"
            except Exception:                      # noqa: BLE001 - recorded, never raised
                verdict = "error"
            dt = time.monotonic_ns() - t0

            sent += 1
            if verdict == "ok":
                acked += 1
            elif verdict == "refused":
                refused += 1
            else:
                errored += 1
            # ONE line per id, written the instant the reply arrived.
            for eid in ids:
                w.write(f"{seq} {eid} {verdict} {dt}\n")

    print(json.dumps({"sent": sent, "acked": acked,
                      "refused": refused, "errored": errored}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
