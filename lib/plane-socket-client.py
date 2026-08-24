#!/usr/bin/env python3
"""Socket leg of the plane-emit shim (Phase-2 T2). Stdlib ONLY — the point of
the daemon is dodging the package-import spawn cost, so this client must never
import claudlobby (`dispatch-overdue.py` precedent).

It also owns the shim's idempotency guarantee: event_ids (and occurred_at) are
minted HERE, into a finalized batch file written BEFORE the first transport
attempt — so when the socket rung fails and the shim replays the SAME file
through the cold CLI, a commit that lost its ack classifies as duplicate
instead of landing twice under fresh ids (F6 extended to transport retries).

stdin:  {"events": [...]} or a bare JSON array
stdout: one event_id per line on success (mirrors `claudlobby emit-batch`)
exit:   0 ok (committed/duplicate/spooled)
        2 contract violation / bad request   (verdicts — the shim must NOT
        3 total failure                       fall back on these: the CLI
        4 downgrade refused                   would only repeat them)
        5 transport unavailable/unclassified (the shim's fallback trigger —
          safe to replay by pre-minted-id idempotency)
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
from datetime import datetime, timezone

VERDICT_EXITS = {"contract_violation": 2, "bad_request": 2,
                 "total_failure": 3, "downgrade": 4}


def _finalize(events: list) -> list:
    out = []
    for e in events:
        e = dict(e)
        if not e.get("event_id"):
            e["event_id"] = "ev_" + uuid.uuid4().hex
        if not e.get("occurred_at"):
            e["occurred_at"] = datetime.now(timezone.utc).isoformat()
        out.append(e)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--finalize-to", required=True,
                    help="File the finalized batch is written to (0600) BEFORE"
                         " the send — the CLI-fallback replays this exact file")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    try:
        parsed = json.loads(sys.stdin.read())
        events = parsed["events"] if isinstance(parsed, dict) else parsed
        if not (isinstance(events, list) and events
                and all(isinstance(e, dict) for e in events)):
            raise ValueError("expected a non-empty list of event objects")
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"plane-socket-client: unreadable batch: {exc}", file=sys.stderr)
        return 2

    finalized = _finalize(events)
    payload = json.dumps({"events": finalized}, ensure_ascii=False)
    fd = os.open(args.finalize_to, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload + "\n")

    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(args.timeout)
        client.connect(args.socket)
        client.sendall(payload.encode() + b"\n")
        client.shutdown(socket.SHUT_WR)
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
        client.close()
        resp = json.loads(buf)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"plane-socket-client: transport failed: {exc}", file=sys.stderr)
        return 5

    if resp.get("ok"):
        spooled = False
        for r in resp.get("results", []):
            print(r.get("event_id", ""))
            spooled = spooled or r.get("status") == "spooled"
        if spooled:
            print("plane-socket-client: db unavailable — daemon spooled the batch",
                  file=sys.stderr)
        return 0
    code = resp.get("code", "")
    print(f"plane-socket-client: daemon refused [{code}]: {resp.get('error')}",
          file=sys.stderr)
    # Verdicts pass through; anything else (forbidden/internal/unknown) is
    # transport-ish — replaying through the CLI is safe by idempotency.
    return VERDICT_EXITS.get(code, 5)


if __name__ == "__main__":
    sys.exit(main())
