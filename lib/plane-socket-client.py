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
exit:   0 ok (committed/duplicate) — RECORDED, in the plane, queryable now
        6 spooled (#1711): accepted and durable on disk, NOT in the plane and
          invisible to every reader until a drain. A verdict, not a fallback
          trigger — the batch is already written, so replaying it through the
          cold rung would only spool it a second time. Also the exit for a
          cooldown batch --stage-to left for the daemon to replay (#1657).
        2 contract violation / bad request   (verdicts — the shim must NOT
        3 total failure                       fall back on these: the CLI
                                              would only repeat them)
        5 transport unavailable/unclassified (the shim's fallback trigger —
          safe to replay by pre-minted-id idempotency) — AND the daemon's
          `downgrade` refusal, see VERDICT_EXITS

With --arm-log, every exit 5 from a socket attempt appends who it was and why
(#1693, see _arm_record): the shim arms the host-wide wedge marker on exactly
those exits, and the marker holds only a time.
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
import time
import uuid
from datetime import datetime, timezone

TRANSPORT_UNAVAILABLE = 5

# A verdict is a refusal the COLD RUNG WOULD REPEAT — that is the whole test,
# and it is a claim about the batch, not about the daemon. A contract
# violation is malformed input; a total failure is a plane that cannot be
# written; the CLI runs the same validator and the same db, so replaying
# either costs a spawn and buys nothing.
#
# `downgrade` fails that test and is therefore NOT here (#1485). It says the
# db is newer than the DAEMON'S LOADED CODE — and the daemon is a long-lived
# process on an editable install, so after a pull-plus-migration its modules
# are the only stale thing on the host. The cold rung is a fresh interpreter
# on the install's CURRENT code: it commits. Treating it as a verdict is what
# lost 261 heartbeat samples across 18 bots in ~15 minutes on the Mini
# (2026-09-06) — every emit refused, nothing retried, nothing spooled. So it
# maps to the transport-unavailable exit and the shim falls to rung 2.
VERDICT_EXITS = {"contract_violation": 2, "bad_request": 2,
                 "total_failure": 3}

# The arm log (#1693). The shim arms the wedge marker on every exit 5 from a
# socket attempt, and the marker holds only a time, so arms could be counted
# per host and never per caller: a canary of the deadline on one bot was
# invisible in the host's count. Each such exit appends one tab-separated row,
#   <epoch> <class> <deadline s> <elapsed ms> <caller> <cause>
# where cause is `timeout` (the deadline fired), `unreachable` (nothing is
# listening), `downgrade`, `code:<x>` (any other refusal the cold rung can
# answer) or the exception's class name. Kept 7 days, because a canary
# compares a day before a knob with a day after; rewritten only once its
# oldest row is a day past that, not on every arm, on the card that is the
# bottleneck. Best-effort: it never changes the exit, and a row appended
# while another client rotates can be lost.
ARM_LOG_WINDOW_S = 7 * 86400
ARM_LOG_SLACK_S = 86400


def _parse_argv(argv: list):
    """--socket S --finalize-to F [--timeout T] [--arm-log L] [--finalize-only [--stage-to D]] — hand-rolled
    (see header). Owns EVERY refusal message and returns None after printing
    one: the old split (parser printed some refusals, main re-diagnosed with a
    generic line) stacked two errors and misattributed unknown-arg failures."""
    sock = fin = None
    timeout = 1.0   # TOTAL deadline (see main) — Pi p95 under load is ~190ms,
                    # so 1s is 5x headroom; anything slower is a wedge and the
                    # fallback rung (+ the shim's cooldown marker) is the fix
    finalize_only = False
    stage_to = ""   # empty: this caller did not opt in to staging (#1657)
    arm_log = ""    # empty: record no arm (#1693)
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--socket" and i + 1 < len(argv):
            sock = argv[i + 1]; i += 2
        elif a == "--finalize-to" and i + 1 < len(argv):
            fin = argv[i + 1]; i += 2
        elif a == "--finalize-only":
            finalize_only = True; i += 1
        elif a == "--stage-to" and i + 1 < len(argv):
            stage_to = argv[i + 1]; i += 2
        elif a == "--arm-log" and i + 1 < len(argv):
            arm_log = argv[i + 1]; i += 2
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
    return sock, fin, timeout, finalize_only, stage_to, arm_log


def _cause(exc: BaseException) -> str:
    # socket.timeout is TimeoutError only from 3.10; a 3.9 host has both.
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, (FileNotFoundError, ConnectionRefusedError)):
        return "unreachable"
    return type(exc).__name__


def _arm_record(path: str, timeout: float, started: float, cause: str) -> None:
    """One row per arm, with WHO: a bot, else its fleet, else the host."""
    if not path:
        return
    env = os.environ
    bot = env.get("BOT_ID", "")
    fleet = env.get("FLEET_NAME") or env.get("CLAUDLOBBY_FLEET", "")
    who = f"bot:{fleet or '?'}/{bot}" if bot else (f"fleet:{fleet}" if fleet else "host")
    now = int(time.time())
    row = [str(now), env.get("PLANE_EMIT_CLASS", ""), str(timeout),
           str(int((time.monotonic() - started) * 1000)), who, cause]
    try:
        with open(path, "a") as f:
            f.write("\t".join(" ".join(v.split()) or "-" for v in row) + "\n")
        _arm_rotate(path, now)
    except Exception:  # noqa: BLE001 -- best-effort: it never changes the exit
        pass


def _arm_rotate(path: str, now: int) -> None:
    def epoch(line: str) -> int:
        try:
            return int(line.split("\t", 1)[0])
        except ValueError:
            return 0    # an unreadable row goes with the old ones
    with open(path, errors="replace") as f:     # a torn row only ages out
        if epoch(f.readline()) >= now - ARM_LOG_WINDOW_S - ARM_LOG_SLACK_S:
            return
        keep = [line for line in f if epoch(line) >= now - ARM_LOG_WINDOW_S]
    tmp = f"{path}.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            f.writelines(keep)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _stage(stage_dir: str, sock_path: str, payload: str, lead: str) -> bool:
    """#1657: leave a cooldown batch for the daemon to replay, instead of the
    cold CLI rung, whose package-import spawn per event on every bot is what
    kept a loaded host pegged and the daemon missing its deadline. Only where
    something WILL replay it: the dir exists (a daemon that replays staged
    batches creates it at startup, so an older daemon still running after a
    pull never gets one) and a listener takes the connect. A stale socket or
    none refuses it, and the caller keeps the cold rung."""
    if not os.path.isdir(stage_dir):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.setblocking(False)
    try:
        probe.connect(sock_path)
    except BlockingIOError:
        pass            # a full backlog is still a listener
    except OSError:
        return False
    finally:
        probe.close()
    tmp = os.path.join(stage_dir, f".{lead}.tmp")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(payload + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, os.path.join(stage_dir, f"{time.time_ns()}-{lead}.batch"))
        dfd = os.open(stage_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        return False    # the cold rung still records it
    return True


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
    sock_path, finalize_to, timeout, finalize_only, stage_to, arm_log = parsed

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
        # for the CLI rung, no socket attempt at all -- unless the caller
        # opted in and a daemon will replay it from disk (#1657).
        if stage_to and _stage(stage_to, sock_path, payload, finalized[0]["event_id"]):
            print("plane-socket-client: cooldown -- batch STAGED for the daemon to"
                  " replay: durable on disk, NOT in the plane until it does",
                  file=sys.stderr)
            return 6
        return 5

    # HARD TOTAL deadline, not per-operation (#1372 review F5): a live-but-
    # wedged listener accepts the connect and never replies — a per-op 30s
    # timeout let it hold an intent-first DOOR hostage for the full window
    # (armed tg-post made zero Telegram calls under a 2s alarm). The deadline
    # bounds connect+send+recv TOGETHER; on breach the client exits 5 and the
    # shim's cold-CLI rung does the real work.
    import time as _time

    started = _time.monotonic()
    deadline = started + timeout

    def _remaining():
        left = deadline - _time.monotonic()
        if left <= 0:
            # A TimeoutError (still an OSError) so the arm log calls it one.
            raise TimeoutError("shim deadline exceeded (wedged daemon?)")
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
        _arm_record(arm_log, timeout, started, _cause(exc))
        return 5

    if resp.get("ok"):
        spooled = False
        for r in resp.get("results", []):
            print(r.get("event_id", ""))
            spooled = spooled or r.get("status") == "spooled"
        if spooled:
            # #1711. The daemon's `ok` is unconditional on outcome kind, so this
            # is the ONLY place the socket rung can still tell the two apart —
            # one frame later the caller has an exit code and nothing else.
            # Returning 0 here asserted "recorded" about a row no reader can
            # see, which is the one thing the estate's disclosure contract
            # exists to prevent.
            print("plane-socket-client: db unavailable — daemon SPOOLED the batch: "
                  "durable on disk, NOT in the plane until a drain",
                  file=sys.stderr)
            return 6
        return 0
    code = resp.get("code", "")
    print(f"plane-socket-client: daemon refused [{code}]: {resp.get('error')}",
          file=sys.stderr)
    if code == "downgrade":
        # Said in its own words because the generic "daemon unavailable" line
        # the shim prints next would misname the condition an operator has to
        # act on: the DAEMON is stale, not the socket. (The daemon now exits
        # on this so its supervisor relaunches it on the current install; this
        # rung must hold regardless, since a daemon too old to know that is
        # exactly the one an operator meets.)
        print("plane-socket-client: the daemon is running older code than the"
              " db it opened — replaying through the cold rung, which runs"
              " the install's current code", file=sys.stderr)
        _arm_record(arm_log, timeout, started, "downgrade")
        return TRANSPORT_UNAVAILABLE
    # Verdicts pass through; anything else (forbidden/internal/unknown) is
    # transport-ish — replaying through the CLI is safe by idempotency.
    rc = VERDICT_EXITS.get(code, TRANSPORT_UNAVAILABLE)
    if rc == TRANSPORT_UNAVAILABLE:
        _arm_record(arm_log, timeout, started, f"code:{code or '-'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
