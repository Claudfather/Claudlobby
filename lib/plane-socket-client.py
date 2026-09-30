#!/usr/bin/env python3
"""Stdlib-only socket and durable stage leg of the private Plane shim.

Event IDs and occurrence times are minted before the socket attempt. A lost
acknowledgement can therefore be replayed from the staged queue as a duplicate.
A staged batch is capture-policy-applied and the queue is bounded; both rules
come from claudlobby/plane/capture_policy.py, imported in this process.
Exit 0 is committed/duplicate; 6 is daemon-spooled or cooldown-staged; 7 is
client-staged after a transport miss (the shell maps it to pending exit 6 and
arms the cooldown). Exits 2 and 3 are refusal/total failure; 3 includes a full
staged queue and an untrusted capture policy, each counted in `.emit-losses`.
Exit 5 is only for a direct client invocation without a staging target.
"""

from __future__ import annotations

# T10 budget lever (measured on the Pi, 2026-08-27): interpreter spawn is the
# door-felt cost — plain python3 ~45ms, `-S -E` ~12ms, and argparse alone adds
# ~15ms of import. The shim invokes this file with `python3 -S -E`, so imports
# here stay minimal-stdlib and argv is parsed by hand.
import json
import os
import socket
import stat
import sys
import time
import uuid
from datetime import datetime, timezone

TRANSPORT_UNAVAILABLE = 5


class StagedFull(Exception):
    """The staged queue is at its bound (capture_policy.STAGED_MAX_*)."""

# Contract and storage verdicts do not become pending queue entries. A stale
# daemon, transport miss, or unclassified refusal does: the selected daemon
# will validate it when it replays the existing staged queue.
VERDICT_EXITS = {"contract_violation": 2, "bad_request": 2,
                 "total_failure": 3}

# The arm log (#1693). The shim arms the wedge marker on every client-staged
# socket attempt, and the marker holds only a time, so arms could be counted
# per host and never per caller: a canary of the deadline on one bot was
# invisible in the host's count. Each such exit appends one tab-separated row,
#   <epoch> <class> <deadline s> <elapsed ms> <caller> <cause>
# where cause is `timeout` (the deadline fired), `unreachable` (nothing is
# listening), `downgrade`, `code:<x>` (an unclassified refusal), or the
# exception's class name. Kept 7 days because a canary compares a day before
# a knob with a day after; rewritten only once its
# oldest row is a day past that, not on every arm, on the card that is the
# bottleneck. Best-effort: it never changes the exit, and a row appended
# while another client rotates can be lost.
ARM_LOG_WINDOW_S = 7 * 86400
ARM_LOG_SLACK_S = 86400


def _parse_argv(argv: list):
    """--socket S [--finalize-to F] [--timeout T] [--arm-log L]
    [--finalize-only] [--stage-to D] — hand-rolled
    (see header). Owns EVERY refusal message and returns None after printing
    one: the old split (parser printed some refusals, main re-diagnosed with a
    generic line) stacked two errors and misattributed unknown-arg failures."""
    sock = fin = None
    timeout = 1.0   # TOTAL deadline (see main) — Pi p95 under load is ~190ms,
                    # so 1s is 5x headroom; anything slower stages and arms
                    # the shim's cooldown marker
    finalize_only = False
    stage_to = ""   # empty: direct client invocation has no pending route
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
    if not sock:
        print("plane-socket-client: --socket is required",
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


def _arm_rotate(path: str, now: int, window: int = ARM_LOG_WINDOW_S,
                slack: int = ARM_LOG_SLACK_S) -> None:
    def epoch(line: str) -> int:
        try:
            return int(line.split("\t", 1)[0])
        except ValueError:
            return 0    # an unreadable row goes with the old ones
    with open(path, errors="replace") as f:     # a torn row only ages out
        if epoch(f.readline()) >= now - window - slack:
            return
        keep = [line for line in f if epoch(line) >= now - window]
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


def _capture_policy():
    """THE capture policy and staged bound (claudlobby/plane/capture_policy.py),
    imported in this process: stdlib-only, so -S -E and no second interpreter.
    Installed, this file is <package>/_native/; in a checkout it is lib/ beside
    the package. realpath: harnesses symlink the client into a scratch lib."""
    here = os.path.dirname(os.path.realpath(__file__))
    if os.path.basename(here) == "_native":
        parent = os.path.dirname(os.path.dirname(here))
    else:
        parent = os.path.dirname(here)
    if not os.path.isfile(os.path.join(parent, "claudlobby", "plane", "capture_policy.py")):
        raise ImportError(f"capture policy not found beside {here}")
    sys.path.insert(0, parent)
    from claudlobby.plane import capture_policy
    return capture_policy


def _staged_usage(path: str):
    """(batches, bytes) already pending. Temp names count: they age into
    replay. An unreadable queue raises OSError, which refuses the stage."""
    count = size = 0
    with os.scandir(path) as it:
        for entry in it:
            if entry.name.endswith((".batch", ".tmp")):
                count += 1
                try:
                    size += entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    pass    # replayed or renamed since the listing
    return count, size


def _record_loss(stage_dir: str, kind: str, detail: str) -> None:
    """One `.emit-losses` row (lib-common plane_emit_loss's format) for a batch
    this client refused to stage. Its fate is stated — NOT recorded — but a
    stderr line alone reaches no durable surface for a hook or a timer. Rotated
    by the arm log's rule with a one-day window. Best-effort."""
    path = os.path.join(os.path.dirname(os.path.abspath(stage_dir)), ".emit-losses")
    door = os.environ.get("PLANE_EMIT_CLASS") or "-"
    now = int(time.time())
    try:
        with open(path, "a") as f:
            f.write("\t".join((str(now), kind, door, " ".join(detail.split()))) + "\n")
        _arm_rotate(path, now, window=86400, slack=3600)
    except Exception:  # noqa: BLE001 -- best-effort: it never changes the exit
        pass


def _stage(stage_dir: str, payload: str, lead: str, bound=None) -> None:
    """Durably stage events for daemon replay through emit_batch.

    The payload is already capture-policy-applied (S5a-04): the replay applies
    the policy again, but the file itself never holds a fuller body than the
    policy allows. `bound` = (max batches, max bytes); at or past it this
    raises StagedFull and writes nothing.
    """
    path = os.path.abspath(stage_dir)
    for directory in (os.path.dirname(os.path.dirname(path)), os.path.dirname(path), path):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        else:
            parent = os.open(os.path.dirname(directory), os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            raise OSError(f"staged queue parent is not an owned directory: {directory}")
    if bound is not None:
        count, size = _staged_usage(path)
        if count >= bound[0] or size + len(payload.encode()) > bound[1]:
            raise StagedFull(f"{count} batches, {size} bytes pending (bound "
                             f"{bound[0]} batches, {bound[1]} bytes)")
    name = f"{time.time_ns()}-{lead}.batch"
    tmp = os.path.join(path, f".{name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                 getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(payload + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(path, name))
        dfd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def _stage_pending(stage_dir: str, finalized: list, cause: str) -> int:
    """Stage the policy-applied batch, within the queue's bound. Every refusal
    names what was not recorded; none writes a fuller body than the policy
    allows (S5a-01, S5a-04)."""
    if not stage_dir:
        return TRANSPORT_UNAVAILABLE  # direct client without a staging target
    # <root>/state/plane/staged: capture.json lives two levels up from root/state.
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(stage_dir))))
    try:
        policy = _capture_policy()
    except Exception as exc:  # noqa: BLE001 -- ImportError, or a broken module
        # Without the shared policy nothing can prove what may be persisted,
        # so nothing is staged. The exception class names the fault; its text
        # could carry a path or source line, so it is not echoed.
        print("plane-socket-client: capture policy module unavailable "
              f"({type(exc).__name__}) — batch NOT staged or recorded (a broken "
              "install: the client must ship beside claudlobby/plane)", file=sys.stderr)
        _record_loss(stage_dir, "stage_refused",
                     f"capture policy module unavailable ({type(exc).__name__})")
        return 3
    bound = (policy.STAGED_MAX_BATCHES, policy.STAGED_MAX_BYTES)
    try:
        captured = policy.apply_to_batch(root, finalized)
    except policy.CaptureConfigInvalid:
        # An environment fault, not a batch fault — but staging the raw batch
        # could keep content the operator opted out of storing. Generic text:
        # the parser's message can quote the file's contents.
        print("plane-socket-client: state/plane/capture.json is unreadable or invalid "
              "— batch NOT staged or recorded (fix the file; `claudlobby plane doctor` "
              "names the fault)", file=sys.stderr)
        _record_loss(stage_dir, "stage_refused", "capture.json unreadable or invalid")
        return 3
    except (ValueError, TypeError) as exc:
        print(f"plane-socket-client: contract violation — batch NOT staged: {exc}",
              file=sys.stderr)
        return 2
    payload = json.dumps({"events": captured}, ensure_ascii=False)
    try:
        _stage(stage_dir, payload, finalized[0]["event_id"], bound=bound)
    except StagedFull as exc:
        print(f"plane-socket-client: staged queue FULL ({exc}) — batch NOT recorded. "
              "The daemon is its only consumer: check `claudlobby plane doctor`",
              file=sys.stderr)
        _record_loss(stage_dir, "staged_full", str(exc))
        return 3
    except OSError as exc:
        print(f"plane-socket-client: staged write failed — batch NOT recorded: {exc}",
              file=sys.stderr)
        _record_loss(stage_dir, "stage_failed", str(exc))
        return 3
    print(f"plane-socket-client: batch STAGED after {cause} — durable on disk, "
          "NOT in the plane until a drain", file=sys.stderr)
    return 7


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
    if finalize_to:  # private direct-client seam; the shim needs no temp file
        fd = os.open(finalize_to, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(payload + "\n")

    if finalize_only:
        # During cooldown, persist the policy-applied batch for daemon replay
        # without a socket attempt.
        staged = _stage_pending(stage_to, finalized, "socket cooldown")
        return 6 if staged == 7 else staged

    # HARD TOTAL deadline, not per-operation (#1372 review F5): a live-but-
    # wedged listener accepts the connect and never replies — a per-op 30s
    # timeout held a door hostage. The deadline bounds connect+send+recv
    # together; on breach the client durably stages and returns pending.
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
        return _stage_pending(stage_to, finalized, f"socket transport: {_cause(exc)}")

    if resp.get("ok"):
        results = resp.get("results")
        if (not isinstance(results, list) or len(results) != len(finalized)
                or any(not isinstance(r, dict) or r.get("event_id") != e["event_id"]
                       or r.get("status") not in {"committed", "duplicate", "spooled"}
                       for r, e in zip(results, finalized))):
            print("plane-socket-client: invalid daemon acknowledgement — "
                  "commit unconfirmed", file=sys.stderr)
            _arm_record(arm_log, timeout, started, "invalid-ack")
            return _stage_pending(stage_to, finalized, "invalid daemon acknowledgement")
        spooled = False
        for r in results:
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
        # The daemon's loaded code is stale. Its supervisor relaunches it;
        # until then the batch remains durable pending, not committed.
        print("plane-socket-client: daemon code is older than its database — "
              "waiting for a refreshed daemon to replay the staged batch", file=sys.stderr)
        _arm_record(arm_log, timeout, started, "downgrade")
        return _stage_pending(stage_to, finalized, "daemon downgrade")
    if code == "capture_config":
        # An environment fault, not this batch's: staging it could keep
        # content the operator opted out of, so it is a counted loss.
        if stage_to:
            _record_loss(stage_to, "stage_refused", "capture.json unreadable or invalid")
        return 3
    # Verdicts pass through; anything else (forbidden/internal/unknown) is
    # transport-ish — staging the pre-minted batch is safe by idempotency.
    rc = VERDICT_EXITS.get(code, TRANSPORT_UNAVAILABLE)
    if rc == TRANSPORT_UNAVAILABLE:
        _arm_record(arm_log, timeout, started, f"code:{code or '-'}")
        return _stage_pending(stage_to, finalized, f"daemon refusal: {code or 'unknown'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
