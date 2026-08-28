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

# T10 budget lever (measured on the Pi, 2026-08-27): interpreter spawn is the
# door-felt cost — plain python3 ~45ms, `-S -E` ~12ms, and argparse alone adds
# ~15ms of import. The shim invokes this file with `python3 -S -E`, so imports
# here stay minimal-stdlib and argv is parsed by hand (two fixed flags).
import json
import os
import socket
import sys
import uuid
from datetime import datetime, timezone

VERDICT_EXITS = {"contract_violation": 2, "bad_request": 2,
                 "total_failure": 3, "downgrade": 4}


def _parse_argv(argv: list):
    """--socket S --finalize-to F [--timeout T] [--finalize-only] — hand-rolled
    (see header). Owns EVERY refusal message and returns None after printing
    one: the old split (parser printed some refusals, main re-diagnosed with a
    generic line) stacked two errors and misattributed unknown-arg failures."""
    sock = fin = None
    timeout = 1.0   # TOTAL deadline (see main) — Pi p95 under load is ~190ms,
                    # so 1s is 5x headroom; anything slower is a wedge and the
                    # fallback rung (+ the shim's cooldown marker) is the fix
    finalize_only = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--socket" and i + 1 < len(argv):
            sock = argv[i + 1]; i += 2
        elif a == "--finalize-to" and i + 1 < len(argv):
            fin = argv[i + 1]; i += 2
        elif a == "--finalize-only":
            finalize_only = True; i += 1
        elif a == "--timeout" and i + 1 < len(argv):
            try:
                timeout = float(argv[i + 1])
            except ValueError:
                timeout = -1.0
            i += 2
        else:
            print(f"plane-socket-client: unknown arg {a!r}", file=sys.stderr)
            return None
    # Finite positive only (#1372 re-verify: 'inf' reached settimeout and
    # died on OverflowError at exit 1 — a laundered verdict code).
    if not (0 < timeout < 3600):
        print(f"plane-socket-client: --timeout must be a finite positive"
              f" number of seconds < 3600", file=sys.stderr)
        return None
    if not sock or not fin:
        print("plane-socket-client: --socket and --finalize-to are required",
              file=sys.stderr)
        return None
    return sock, fin, timeout, finalize_only


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
    parsed = _parse_argv(sys.argv[1:])
    if parsed is None:  # the parser already printed the one refusal
        return 2
    sock_path, finalize_to, timeout, finalize_only = parsed

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
    fd = os.open(finalize_to, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload + "\n")

    if finalize_only:
        # The shim's wedge-cooldown path: mint + persist the idempotent batch
        # for the CLI rung, no socket attempt at all.
        return 5

    # HARD TOTAL deadline, not per-operation (#1372 review F5): a live-but-
    # wedged listener accepts the connect and never replies — a per-op 30s
    # timeout let it hold an intent-first DOOR hostage for the full window
    # (armed tg-post made zero Telegram calls under a 2s alarm). The deadline
    # bounds connect+send+recv TOGETHER; on breach the client exits 5 and the
    # shim's cold-CLI rung does the real work.
    import time as _time

    deadline = _time.monotonic() + timeout

    def _remaining():
        left = deadline - _time.monotonic()
        if left <= 0:
            raise OSError("shim deadline exceeded (wedged daemon?)")
        return left

    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(_remaining())
        client.connect(sock_path)
        client.settimeout(_remaining())
        client.sendall(payload.encode() + b"\n")
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass  # daemon may have replied+closed already — benign, it reads
            # to the newline regardless
        buf = b""
        while b"\n" not in buf:
            client.settimeout(_remaining())
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 65536:
                # probe_daemon's cap (F15 re-verify), mirrored: a same-uid
                # squatter streaming newline-free bytes must cost bounded
                # memory, not GBs inside the deadline.
                raise OSError("oversized reply (not our daemon?)")
        client.close()
        resp = json.loads(buf)
        if not isinstance(resp, dict):
            # a non-object reply would AttributeError below at exit 1 — an
            # undefined code; whatever sent it is not our daemon.
            raise ValueError(f"non-object reply: {type(resp).__name__}")
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
