"""Chunk L (#1479): `plane view` stops on SIGTERM with a held SSE client.

A real process, because the defect lived in the server's shutdown sequence:
uvicorn waits on in-flight requests with no bound by default, and a held
`/api/stream` connection kept the daemon alive until a SIGKILL (measured 20s+
before the change).

The fold asks for more than "eventually": the stream ENDS ITSELF when the
daemon is asked to stop, so the exit is immediate and nothing is cancelled.
Both halves are pinned, because they fail independently — a ceiling alone
still exits (at the ceiling, cancelling the stream and printing a traceback),
and a released stream with no ceiling would still hang on a wedged one.
Measured on this branch: 5.18s + one CancelledError traceback before the
fold, 0.26s + none after.
"""
from __future__ import annotations

import http.client
import signal
import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_sigterm_ends_the_daemon_at_once_with_a_stream_held(tmp_path):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "claudlobby", "--root", str(tmp_path), "plane", "view",
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        t0 = time.time()
        up = False
        while time.time() - t0 < 20:
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                c.request("GET", "/healthz")
                c.getresponse().read()
                up = True
                break
            except OSError:
                time.sleep(0.25)
        assert up, "the view never answered /healthz"
        held = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        held.request("GET", "/api/stream")
        resp = held.getresponse()
        assert resp.fp.readline()          # the stream is live: its first bytes arrived
        t1 = time.time()
        proc.send_signal(signal.SIGTERM)
        try:
            # ONE bound (the fold): the assertion below is the real gate, and
            # this only keeps a regression from hanging the suite forever.
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("plane view still running 10s after SIGTERM"
                        " with a stream held")
        elapsed = time.time() - t1
        err = proc.stderr.read() or ""
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stderr and not proc.stderr.closed:
            proc.stderr.close()
    # the stream ENDED — it was not waited out to the graceful ceiling (5s)
    assert elapsed < 3, f"exit took {elapsed:.2f}s with a stream held\n{err}"
    # ...and nothing was cancelled on the way: a CancelledError traceback is
    # the shape of a stream that had to be killed
    assert "CancelledError" not in err, err
    assert "Traceback" not in err, err
