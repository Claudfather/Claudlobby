"""A real Unix listener standing in for the Plane ingest daemon's socket (#2086).

The receipt check and `plane doctor` probe the daemon with an empty request and
read its typed bad_request answer. These listeners answer that handshake as the
daemon does, answer it some other way, or take each connection and never
answer, which is what a daemon past the probe's deadline looks like from outside.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import tempfile
import threading

#: The daemon's answer to an empty request, as `probe_daemon` checks it.
DAEMON_ANSWER = b'{"ok": false, "code": "bad_request", "error": "empty request"}\n'


def short_socket_dir(prefix: str = "il-") -> Path:
    """A fresh short directory: macOS sun_path cannot hold pytest's basetemp."""
    for base in (os.environ.get("CLAUDLOBBY_TEST_TMPDIR"), "/tmp", tempfile.gettempdir()):
        if not base:
            continue
        try:
            return Path(tempfile.mkdtemp(prefix=prefix, dir=base))
        except OSError:
            continue
    raise OSError("no writable short directory for a test socket")


@contextmanager
def listening_socket(path: Path, *, answer: bytes | None = DAEMON_ANSWER):
    """Listen on `path` for the block. With `answer`, read each request and
    send it; with None, hold every connection open and never answer."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(16)
    server.settimeout(0.05)
    held, stop = [], threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            held.append(conn)
            if answer is not None:
                conn.settimeout(5)
                try:
                    conn.recv(65536)
                    conn.sendall(answer)
                except OSError:
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield path
    finally:
        stop.set()
        thread.join(timeout=5)
        for conn in held:
            conn.close()
        server.close()
        path.unlink(missing_ok=True)
