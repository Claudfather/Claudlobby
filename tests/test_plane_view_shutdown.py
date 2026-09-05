"""Chunk L (#1479): `plane view` stops on SIGTERM with a held SSE client.

A real process, because the defect lived in the server's shutdown sequence:
uvicorn waits on in-flight requests with no bound by default, and a held
`/api/stream` connection kept the daemon alive until a SIGKILL (measured 20s+
on the fix's branch before the change; 5.2s after)."""
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


def test_sigterm_ends_the_daemon_within_the_bound_with_a_stream_held(tmp_path):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "claudlobby", "--root", str(tmp_path), "plane", "view",
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("plane view still running 15s after SIGTERM with a stream held")
        assert time.time() - t1 < 10, "exit took longer than the graceful bound"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
