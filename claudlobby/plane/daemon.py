"""The ingest daemon — the socket front of emit_batch (Phase-2 plan §2).

Scope tripwire (operator ruling 2026-08-24): this process owns INGEST AND
NOTHING ELSE. It never schedules, never supervises, never acts on the world;
the Phase-4 UI is a separate consumer. If a feature wants to "live in the
daemon", that want is the tripwire firing.

Transport can never change semantics: every request runs the SAME
emit_batch() the CLI runs — capture policy, raw-then-transformed validation,
spool-on-retryable, the full failure taxonomy. The daemon adds only (a) the
socket, (b) periodic spool drains, (c) its own lifecycle as kind=system
events. emit_batch opens/closes the db per batch (~1-2ms on WAL) — kept
rather than optimized away, because reusing the one write path IS the
correctness argument.

Protocol (one request per connection, newline-delimited JSON):
  client sends:  {"events": [<EmitRequest>, ...]}\n
  daemon replies:{"ok": true,  "results": [{"event_id","status","detail"?}...]}\n
             or  {"ok": false, "code": "<taxonomy>", "error": "..."}\n
  codes mirror the CLI exits: bad_request/contract_violation -> 2,
  total_failure -> 3, downgrade -> 4, internal -> 1.

Ack semantics are SYNCHRONOUS AND HONEST (plan §1): the reply is written only
after commit (or spool). An ack that precedes validation is a receipt that
isn't — the F9/F6 hazard class this program exists to close.

Trust posture (F22): same-user local trust. The 0700 state dir is the
enforced boundary; the peer-uid check here is defense-in-depth on the two
platforms that expose it (SO_PEERCRED / LOCAL_PEERCRED) and best-effort-open
elsewhere, honestly.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import signal
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Optional

from .contracts import ContractViolation
from .db import connect, db_path
from .emit_api import emit_batch
from .ids import ensure_host_uid
from .migrations import DowngradeError, migrate
from .spool import SpoolWriteError, drain

# One line carries one batch; communications bodies cap at 16KiB each, so
# 4MiB bounds any sane batch while refusing a runaway/hostile writer.
MAX_REQUEST_BYTES = 4 * 1024 * 1024
# macOS caps sun_path at 104 bytes (Linux 108); refuse loudly rather than
# letting bind() truncate or fail obscurely.
MAX_SOCKET_PATH_BYTES = 100
DEFAULT_DRAIN_INTERVAL = 600.0


class DaemonAlreadyRunning(RuntimeError):
    """Another daemon owns this socket (holds its lifetime lock, or a live
    listener answered on the path) — exactly one instance per socket."""


class SocketPathTooLong(RuntimeError):
    pass


class SocketOverrideInvalid(RuntimeError):
    """--socket names a parent the daemon will not create or chmod: it only
    provisions its OWN default dir (state/plane) — mutating an arbitrary
    supplied directory to 0700 is not the daemon's to do."""


def socket_path(root: Path) -> Path:
    p = db_path(Path(root)).parent / "ingest.sock"   # 0700 dir, beside the db
    return p


def _check_sun_path(path: Path) -> None:
    if len(str(path).encode()) > MAX_SOCKET_PATH_BYTES:
        raise SocketPathTooLong(
            f"socket path exceeds {MAX_SOCKET_PATH_BYTES} bytes (sun_path"
            f" limit): {path} — pass a shorter --socket"
        )


def _peer_uid(conn: socket.socket) -> Optional[int]:
    """Best-effort peer uid. None = platform gives us nothing; the 0700
    directory remains the enforced boundary (F22)."""
    try:
        if sys.platform.startswith("linux"):
            data = conn.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", data)
            return uid
        if sys.platform == "darwin":
            # struct xucred: u32 version, u32 uid, i16 ngroups, gid_t[16]
            data = conn.getsockopt(0, 0x0001, 4 + 4 + 4 + 16 * 4)
            _version, uid = struct.unpack_from("II", data)
            return uid
    except OSError:
        return None
    return None


def _probe_live(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _recv_line(conn: socket.socket) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_REQUEST_BYTES:
            raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks)


class PlaneDaemon:
    def __init__(
        self,
        root: Path,
        *,
        socket_override: Optional[Path] = None,
        drain_interval: float = DEFAULT_DRAIN_INTERVAL,
    ):
        self.root = Path(root)
        self._is_default_socket = socket_override is None
        self.sock_path = Path(socket_override) if socket_override else socket_path(self.root)
        self.drain_interval = drain_interval
        self._stop = False
        self._listener: Optional[socket.socket] = None
        self._last_drain = 0.0
        self._own_uid = os.geteuid()
        self._lock_fd: Optional[int] = None
        self._sock_stat: Optional[tuple[int, int]] = None

    # -- lifecycle events (best-effort: the recorder's own heartbeat must
    #    never kill the recorder) ------------------------------------------
    def _emit_system(self, event: str, data: Optional[dict] = None) -> None:
        try:
            host = ensure_host_uid(self.root / "state")
            emit_batch(self.root, [{
                "event_type": "system",
                "emitter": "plane-daemon",
                "payload": {
                    "event": event,
                    "subject_kind": "host",
                    "subject_uid": host,
                    **({"data": data} if data else {}),
                },
            }])
        except Exception as exc:  # noqa: BLE001 — disclosed, never fatal
            print(f"plane-daemon: lifecycle emit failed ({event}): {exc}",
                  file=sys.stderr)

    def _drain_spool(self, *, reason: str) -> None:
        # Deadline advances at ATTEMPT, not success: stamping only on success
        # made a broken db retry once per accept-timeout tick (~1s) forever,
        # ignoring the configured interval.
        self._last_drain = time.monotonic()
        try:
            conn = connect(db_path(self.root))
            try:
                migrate(conn)
                host = ensure_host_uid(self.root / "state")
                report = drain(self.root, conn, host)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — a broken drain is disclosed,
            # the serve loop survives; poison entries quarantine INSIDE drain.
            print(f"plane-daemon: spool drain failed ({reason}): {exc}",
                  file=sys.stderr)
            return
        if report.ingested or report.duplicates or report.quarantined:
            self._emit_system("spool_drain_completed", {
                "reason": reason,
                "ingested": report.ingested,
                "duplicates": report.duplicates,
                "quarantined": report.quarantined,
                "remaining": report.remaining,
            })

    # -- request handling ---------------------------------------------------
    def _handle(self, conn: socket.socket) -> None:
        peer = _peer_uid(conn)
        if peer is not None and peer != self._own_uid:
            self._reply(conn, {"ok": False, "code": "forbidden",
                               "error": f"peer uid {peer} != {self._own_uid}"})
            return
        try:
            raw = _recv_line(conn)
        except ValueError as exc:
            self._reply(conn, {"ok": False, "code": "bad_request", "error": str(exc)})
            return
        if not raw.strip():
            self._reply(conn, {"ok": False, "code": "bad_request",
                               "error": "empty request"})
            return
        try:
            parsed = json.loads(raw)
            events = parsed["events"]
            assert isinstance(events, list) and events
            assert all(isinstance(e, dict) for e in events)
        except (json.JSONDecodeError, KeyError, TypeError, AssertionError) as exc:
            self._reply(conn, {"ok": False, "code": "bad_request",
                               "error": f"expected {{\"events\": [...]}}: {exc}"})
            return
        try:
            outcomes = emit_batch(self.root, events)
        except ContractViolation as exc:
            errors = getattr(exc, "errors", None)
            self._reply(conn, {"ok": False, "code": "contract_violation",
                               "error": str(errors[0] if errors else exc)})
            return
        except SpoolWriteError as exc:
            self._reply(conn, {"ok": False, "code": "total_failure", "error": str(exc)})
            return
        except DowngradeError as exc:
            self._reply(conn, {"ok": False, "code": "downgrade", "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 — mirror the CLI's loud exit 1:
            # the CALLER gets a typed refusal; the traceback goes to our stderr.
            import traceback

            traceback.print_exc()
            self._reply(conn, {"ok": False, "code": "internal", "error": str(exc)})
            return
        self._reply(conn, {
            "ok": True,
            "results": [
                {"event_id": o.event_id, "status": o.status,
                 **({"detail": o.detail} if o.detail else {})}
                for o in outcomes
            ],
        })

    @staticmethod
    def _reply(conn: socket.socket, obj: dict) -> None:
        try:
            conn.sendall(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
        except OSError:
            pass  # client went away; the commit (if any) stands — replay is idempotent

    # -- serve loop -----------------------------------------------------------
    def _acquire_lock(self) -> None:
        """Lifetime flock beside the socket — THE single-instance mechanism.
        The old probe→unlink→bind sequence was a TOCTOU window: two daemons
        racing it both bound hidden sockets, last rename owned the path, the
        loser served an orphaned inode, and the loser's shutdown unlink
        DELETED the winner's socket (a reachable-looking daemon nobody can
        reach). The lock is held (fd open) for the daemon's life and released
        by the kernel on any death, so a crashed holder never wedges the
        next start."""
        lock_path = self.sock_path.with_name(self.sock_path.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise DaemonAlreadyRunning(
                f"another plane daemon holds {lock_path}"
            ) from None
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def _bind(self) -> socket.socket:
        _check_sun_path(self.sock_path)
        if self._is_default_socket:
            # db_path() has already provisioned state/plane at 0700; this is
            # the one directory the daemon owns.
            self.sock_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.sock_path.parent, 0o700)
        elif not self.sock_path.parent.is_dir():
            # An override parent is the OPERATOR'S directory: never created,
            # never chmodded (a 0755 dir silently became 0700 before this).
            raise SocketOverrideInvalid(
                f"--socket parent does not exist: {self.sock_path.parent}"
            )
        self._acquire_lock()
        try:
            if self.sock_path.exists():
                if _probe_live(self.sock_path):
                    # We hold the lock, so no daemon owns this path — a live
                    # listener here is a foreign squatter, not ours to unlink.
                    raise DaemonAlreadyRunning(
                        f"a live listener is squatting {self.sock_path}"
                        " (it does not hold the daemon lock)"
                    )
                self.sock_path.unlink()   # stale socket from a dead daemon
            # Bind on a hidden name and rename into place only AFTER
            # listen(): between bind() and listen() the file exists but
            # connect() gets ECONNREFUSED, so exposing the public path first
            # hands every prober a refused-connection window. A unix socket
            # is its inode — the rename preserves the listener. The hidden
            # name is MINIMAL (".s" + 8 hex): a verbose suffix pushed a
            # public path that FIT sun_path over the limit on macOS's deep
            # TMPDIR roots, refusing serve on exactly the roots the public
            # check had approved (uniqueness: urandom + the exclusive lock
            # already held above).
            tmp = self.sock_path.with_name(f".s{os.urandom(4).hex()}")
            _check_sun_path(tmp)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(tmp))
                os.chmod(tmp, 0o600)
                listener.listen(64)
                os.replace(tmp, self.sock_path)
                st = os.stat(self.sock_path)
                self._sock_stat = (st.st_dev, st.st_ino)
            except BaseException:
                listener.close()
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
        except BaseException:
            self._release_lock()
            raise
        listener.settimeout(1.0)      # poll granularity for stop/drain
        return listener

    def _unlink_owned_socket(self) -> None:
        """Remove the public socket ONLY if it is still the inode we renamed
        in — a replacement (however it got there) is someone else's file."""
        if self._sock_stat is None:
            return
        try:
            st = os.stat(self.sock_path)
            if (st.st_dev, st.st_ino) == self._sock_stat:
                self.sock_path.unlink()
        except OSError:
            pass

    def stop(self, *_args) -> None:
        self._stop = True

    def serve(self, *, install_signals: bool = True) -> None:
        """Serial accept loop (plan §2): SQLite is a single writer, so the
        per-connection batch is the concurrency unit and the kernel's listen
        backlog is the queue."""
        self._listener = self._bind()
        if install_signals:
            signal.signal(signal.SIGTERM, self.stop)
            signal.signal(signal.SIGINT, self.stop)
        print(f"plane-daemon: serving on {self.sock_path}", file=sys.stderr)
        self._emit_system("daemon_started")
        self._drain_spool(reason="startup")
        try:
            while not self._stop:
                if time.monotonic() - self._last_drain >= self.drain_interval:
                    self._drain_spool(reason="interval")
                try:
                    conn, _ = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError as exc:
                    if exc.errno == errno.EBADF or self._stop:
                        break
                    raise
                try:
                    # 5s, not 30: the loop is serial, so one stalled client
                    # holds every healthy one for the whole read deadline —
                    # a partial-sender costs the fleet 5s of ingest, not 30.
                    conn.settimeout(5.0)
                    self._handle(conn)
                except OSError as exc:
                    # A slow/vanished/hostile CLIENT (read timeout, reset) is
                    # that connection's problem, never the daemon's: one bad
                    # peer must not kill the recorder for every good one.
                    print(f"plane-daemon: connection dropped: {exc}",
                          file=sys.stderr)
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
        finally:
            self._emit_system("daemon_stopping")
            try:
                self._listener.close()
            except OSError:
                pass
            self._unlink_owned_socket()
            self._release_lock()


def send_batch(sock_path: Path, events: list[dict], *, timeout: float = 30.0) -> dict:
    """One client round trip — the daemon tests' and any Python caller's door.
    Raises OSError when the socket is absent/refused (the shim's fallback
    signal); returns the parsed response dict otherwise."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(sock_path))
        client.sendall(json.dumps({"events": events}, ensure_ascii=False).encode() + b"\n")
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass  # daemon may have replied+closed already (fast verdicts) —
            # ENOTCONN here is benign, it reads to the newline regardless
        buf = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        return json.loads(buf)
    finally:
        client.close()
