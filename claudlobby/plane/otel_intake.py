"""Loopback OTLP/HTTP-JSON receiver; acknowledge only written raw bodies."""
from __future__ import annotations

from collections import Counter, deque
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import zlib

from ..known_values import OTEL_INTAKE_PORT
from .otel_sink import RawSink

PATHS = {"/v1/logs": "logs", "/v1/metrics": "metrics"}
MAX_BODY_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT_S = 10.0


class Refused(Exception):
    def __init__(self, status: int):
        self.status = status


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.accepted = 0
        self.refused = Counter()
        self.unwritten = 0
        self.latencies = deque(maxlen=10000)

    def record(self, status: int, latency: float):
        with self.lock:
            if status == 200:
                self.accepted += 1
            else:
                self.refused[status] += 1
            if status == 503:
                self.unwritten += 1
            self.latencies.append(latency)

    def report(self) -> str:
        with self.lock:
            values = sorted(self.latencies) or [0.0]
            return (f"plane-otel accepted={self.accepted} refused={dict(self.refused)} unwritten={self.unwritten} "
                    f"reply_ms p50={values[int((len(values)-1)*.5)]*1000:.2f} "
                    f"p95={values[int((len(values)-1)*.95)]*1000:.2f} max={values[-1]*1000:.2f}")


def _valid_body(body, signal_name):
    """Validate the traversed OTLP containers before the privacy filter sees them."""
    key, scopes, records = (("resourceLogs", "scopeLogs", "logRecords") if signal_name == "logs"
                            else ("resourceMetrics", "scopeMetrics", "metrics"))
    if not isinstance(body, dict) or not isinstance(body.get(key), list):
        return False
    def attributes(value):
        return isinstance(value, list) and all(isinstance(a, dict) and isinstance(a.get("key"), str)
                                              and isinstance(a.get("value", {}), dict) for a in value)
    for resource in body[key]:
        if not isinstance(resource, dict) or not isinstance(resource.get("resource", {}), dict):
            return False
        if not attributes(resource.get("resource", {}).get("attributes", [])):
            return False
        if not isinstance(resource.get(scopes, []), list):
            return False
        for scope in resource.get(scopes, []):
            if not isinstance(scope, dict) or not isinstance(scope.get(records, []), list):
                return False
            for record in scope.get(records, []):
                if not isinstance(record, dict) or not attributes(record.get("attributes", [])):
                    return False
    return True


def _check_unicode(value):
    """JSON accepts escaped lone surrogates; the persisted UTF-8 stream cannot."""
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_unicode(key)
            _check_unicode(item)
    elif isinstance(value, list):
        for item in value:
            _check_unicode(item)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.timeout = REQUEST_TIMEOUT_S
        super().setup()             # sets the timeout before parsing any headers

    def log_message(self, *args):
        pass                         # no content or account identity in access logs

    def do_GET(self):
        self._reply(404, time.monotonic())

    def _reply(self, status, start):
        self.server.stats.record(status, time.monotonic() - start)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")
        self.wfile.flush()

    def _exact(self, count):
        body = self.rfile.read(count)
        if len(body) != count:
            raise Refused(400)
        return body

    def _read_body(self):
        encoding = self.headers.get("Transfer-Encoding", "").lower()
        if encoding:
            if encoding != "chunked" or self.headers.get("Content-Length") is not None:
                raise Refused(400)
            body = bytearray()
            while True:
                line = self.rfile.readline(128)
                try:
                    count = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise Refused(400) from None
                if count < 0:
                    raise Refused(400)
                if count == 0:
                    # Bound trailers too; they are not telemetry data.
                    trailer_bytes = 0
                    while True:
                        trailer = self.rfile.readline(8193)
                        trailer_bytes += len(trailer)
                        if trailer_bytes > 8192 or not trailer:
                            raise Refused(400)
                        if trailer == b"\r\n":
                            return bytes(body)
                if len(body) + count > MAX_BODY_BYTES:
                    raise Refused(413)
                body.extend(self._exact(count))
                if self._exact(2) != b"\r\n":
                    raise Refused(400)
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise Refused(400) from None
        if size < 0:
            raise Refused(400)
        if size > MAX_BODY_BYTES:
            raise Refused(413)
        return self._exact(size)

    def do_POST(self):
        start = time.monotonic()
        signal_name = PATHS.get(self.path)
        try:
            if signal_name is None:
                raise Refused(404)
            if self.headers.get_content_type() != "application/json":
                raise Refused(415)
            raw = self._read_body()
            encoding = self.headers.get("Content-Encoding", "identity").lower()
            if encoding == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                    raw = stream.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise Refused(413)
            elif encoding != "identity":
                raise Refused(415)
            body = json.loads(raw)
            _check_unicode(body)
            if not _valid_body(body, signal_name):
                raise Refused(400)
        except Refused as exc:
            self._reply(exc.status, start)
            return
        except (ValueError, UnicodeError, EOFError, OSError, RecursionError, zlib.error):
            self._reply(400, start)
            return
        try:
            self.server.sink.append(signal_name, body)
        except (ValueError, TypeError, RecursionError):
            self._reply(400, start)
            return
        except OSError:
            self._reply(503, start)
            return
        self._reply(200, start)
        self.server.on_accepted(signal_name, body)


class IntakeServer(ThreadingHTTPServer):
    # server_close waits for handlers: no torn line on SIGTERM.
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, sink):
        self.sink = sink
        self.stats = Stats()
        self.on_accepted = lambda signal_name, body: None
        super().__init__(address, Handler)


def loopback_host(value: str) -> str:
    import argparse
    if value == "127.0.0.1":
        return value
    raise argparse.ArgumentTypeError("--host must be 127.0.0.1 (loopback only)")


def serve(root: Path, host: str = "127.0.0.1", port: int = OTEL_INTAKE_PORT, *,
          retention_days: int, install_signals=True):
    loopback_host(host)
    os.umask(0o077)
    sink = RawSink(root, days=retention_days)   # privacy refusal precedes bind
    server = IntakeServer((host, port), sink)
    stopped = threading.Event()
    def stop(*_):
        stopped.set()
    previous = {}
    if install_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, stop)
    server.timeout = .25
    last_stats = last_prune = time.monotonic()
    try:
        while not stopped.is_set():
            server.handle_request()
            now = time.monotonic()
            if now - last_stats >= 600:
                print(server.stats.report(), file=sys.stderr, flush=True)
                last_stats = now
            if now - last_prune >= 3600:
                sink.prune(time.time())
                last_prune = now
    finally:
        server.server_close()
        print(server.stats.report(), file=sys.stderr, flush=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
