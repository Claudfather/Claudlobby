"""Real HTTP transport against a scratch raw sink; no plane daemon or model."""
import gzip
import http.client
import json
import threading
import time

import pytest

from claudlobby.plane import otel_intake as intake
from claudlobby.plane.otel_sink import RawSink


@pytest.fixture
def receiver(tmp_path):
    server = intake.IntakeServer(('127.0.0.1', 0), RawSink(tmp_path, days=14))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


def post(server, body, path='/v1/logs', headers=None, chunked=False, method='POST'):
    conn = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        conn.request(method, path, body, headers or {'Content-Type': 'application/json'}, encode_chunked=chunked)
        response = conn.getresponse()
        return response.status, response.headers, response.read()
    finally:
        conn.close()


@pytest.mark.parametrize('encoding', ['plain', 'gzip', 'chunked'])
@pytest.mark.parametrize('signal', ['logs', 'metrics'])
def test_framing_writes_one_line_before_ack(receiver, encoding, signal):
    # Minimal synthetic transport envelope; canonical captured requests tested below.
    body = {'resourceLogs' if signal == 'logs' else 'resourceMetrics': []}
    raw = json.dumps(body, indent=2).encode()
    headers = {'Content-Type': 'application/json'}
    if encoding == 'gzip':
        raw = gzip.compress(raw); headers['Content-Encoding'] = 'gzip'
    if encoding == 'chunked':
        raw = [raw[:5], raw[5:]]
    status, headers, response = post(receiver, raw, f'/v1/{signal}', headers, encoding == 'chunked')
    assert (status, response, headers.get_content_type()) == (200, b'{}', 'application/json')
    lines = (receiver.sink.path/f'{signal}.jsonl').read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0]) == body


@pytest.mark.parametrize('body,path,headers,method,status', [
    (b'{}', '/v1/traces', {}, 'POST', 404),
    (None, '/v1/logs', {}, 'GET', 404),
    (b'{}', '/v1/logs', {'Content-Type': 'application/x-protobuf'}, 'POST', 415),
    (b'{', '/v1/logs', {'Content-Type': 'application/json'}, 'POST', 400),
    (b'{}', '/v1/logs', {'Content-Type': 'application/json'}, 'POST', 400),
    (b'{"resourceLogs":[null]}', '/v1/logs', {'Content-Type': 'application/json'}, 'POST', 400),
    (b'', '/v1/logs', {'Content-Type': 'application/json', 'Content-Length': str(intake.MAX_BODY_BYTES+1)}, 'POST', 413),
    (gzip.compress(b'x'*(intake.MAX_BODY_BYTES+1)), '/v1/logs', {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'}, 'POST', 413),
])
def test_refused_requests_never_ack_or_write(receiver, body, path, headers, method, status):
    assert post(receiver, body, path, headers, method=method)[0] == status
    assert list(receiver.sink.path.iterdir()) == []
    assert receiver.stats.refused[status] == 1


def test_failed_append_is_retryable(receiver, monkeypatch):
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(receiver.sink, 'append', fail)
    assert post(receiver, b'{"resourceLogs":[]}')[0] == 503
    assert receiver.stats.unwritten == 1


def test_callback_runs_after_reply(receiver):
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        entered.set(); release.wait(3)
    receiver.on_accepted = slow
    try:
        assert post(receiver, b'{"resourceLogs":[]}')[0] == 200
        assert entered.wait(1)
    finally:
        release.set()
    report = receiver.stats.report()
    assert 'accepted=1' in report and 'p50=' in report and 'p95=' in report and 'max=' in report


def test_serve_refuses_privacy_before_bind(tmp_path, monkeypatch):
    path = tmp_path/'state'/'otel'; path.mkdir(parents=True, mode=0o755)
    monkeypatch.setattr(intake, 'IntakeServer', lambda *args: pytest.fail('bound before privacy check'))
    with pytest.raises(OSError):
        intake.serve(tmp_path, retention_days=14, install_signals=False)


def test_cli_refuses_nonloopback(tmp_path, capsys):
    from claudlobby.__main__ import main
    with pytest.raises(SystemExit) as exc:
        main(['--root', str(tmp_path), 'plane', 'otel-intake', '--host', '0.0.0.0'])
    assert exc.value.code == 2
    assert 'invalid argument' in capsys.readouterr().err


@pytest.mark.parametrize('signal', ['logs', 'metrics'])
def test_captured_native_exporter_body(receiver, signal):
    import copy
    from pathlib import Path
    from claudlobby.plane.otel_sink import strip_content
    fixture = Path(__file__).parent/'fixtures'/'otel'/f'c6a-{signal}-body.json'
    body = json.loads(fixture.read_text())
    expected = copy.deepcopy(body)
    if signal == 'logs':
        strip_content(expected)
    assert post(receiver, fixture.read_bytes(), f'/v1/{signal}')[0] == 200
    assert json.loads((receiver.sink.path/f'{signal}.jsonl').read_text()) == expected


@pytest.mark.parametrize('host', ['0.0.0.0', '127.0.0.2', '::1', 'localhost'])
def test_only_composed_loopback_address_is_accepted(host):
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        intake.loopback_host(host)


def test_invalid_deflate_in_valid_gzip_header_is_refused(receiver):
    # Valid gzip header, reserved DEFLATE block type: zlib.error, not BadGzipFile.
    malformed = b'\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\x07'
    status, _headers, body = post(receiver, malformed, headers={
        'Content-Type': 'application/json', 'Content-Encoding': 'gzip'})
    assert status == 400 and body == b'{}'
    assert receiver.stats.refused[400] == 1
    assert receiver.stats.accepted == 0
    assert list(receiver.sink.path.iterdir()) == []


def test_lone_surrogate_is_400_before_raw_mutation(receiver):
    payload = b'{"resourceLogs":[],"untrusted":"\\ud800"}'
    assert post(receiver, payload)[0] == 400
    assert receiver.stats.refused[400] == 1 and receiver.stats.accepted == 0
    assert not list(receiver.sink.path.iterdir())


def test_recursive_tool_parameters_are_dropped_under_metadata_gate(receiver):
    parameters = '{"skill_name":' + '['*1200 + '0' + ']'*1200 + '}'
    body = {'resourceLogs': [{'scopeLogs': [{'logRecords': [{'attributes': [
        {'key': 'tool_parameters', 'value': {'stringValue': parameters}},
        {'key': 'tool_name', 'value': {'stringValue': 'Skill'}}]}]}]}]}
    assert post(receiver, json.dumps(body).encode())[0] == 200
    saved = json.loads((receiver.sink.path/'logs.jsonl').read_text())
    attrs = saved['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes']
    assert [a['key'] for a in attrs] == ['tool_name']


def test_rollback_failure_never_acknowledges_followup_body(receiver, monkeypatch):
    from tests.test_plane_otel_sink import inject_partial_write
    from claudlobby.plane import otel_sink
    with monkeypatch.context() as patch:
        inject_partial_write(patch)
        patch.setattr(otel_sink.os, 'ftruncate', lambda *args: (_ for _ in ()).throw(OSError('rollback failed')))
        assert post(receiver, b'{"resourceMetrics":[]}', '/v1/metrics')[0] == 503
    fragment = (receiver.sink.path/'metrics.jsonl').read_bytes()
    assert post(receiver, b'{"resourceMetrics":[]}', '/v1/metrics')[0] == 503
    assert receiver.stats.accepted == 0 and receiver.stats.unwritten == 2
    assert (receiver.sink.path/'metrics.jsonl').read_bytes() == fragment


def test_many_tiny_chunks_have_bounded_accumulation():
    import io
    import tracemalloc
    from types import SimpleNamespace
    count = 100000
    wire = b'1\r\nx\r\n'*count + b'0\r\n\r\n'
    reader = SimpleNamespace(headers={'Transfer-Encoding': 'chunked'}, rfile=io.BytesIO(wire))
    reader._exact = lambda n: intake.Handler._exact(reader, n)
    tracemalloc.start()
    try:
        body = intake.Handler._read_body(reader)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert body == b'x'*count
    assert peak < count*4 + 65536


def test_incomplete_headers_do_not_prevent_sigterm_drain(tmp_path):
    import socket
    import subprocess
    import sys
    from tests.conftest import constructed_env
    script = '''
from pathlib import Path
import sys
from claudlobby.plane import otel_intake as intake
root = Path(sys.argv[1])
intake.REQUEST_TIMEOUT_S = .25
original_setup = intake.Handler.setup
def observed_setup(self):
    original_setup(self)
    (root / ("handler-" + str(self.client_address[1]))).touch()
intake.Handler.setup = observed_setup
class ObservedServer(intake.IntakeServer):
    def __init__(self, *args):
        super().__init__(*args)
        (root / "port").write_text(str(self.server_port))
intake.IntakeServer = ObservedServer
intake.serve(root, port=0, retention_days=14)
'''
    proc = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)],
                            env=constructed_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    held = None
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path/'port').exists():
            assert proc.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.02)
        held = socket.create_connection(('127.0.0.1', int((tmp_path/'port').read_text())), timeout=2)
        held.sendall(b'POST /v1/logs HTTP/1.1\r\nHost:')
        mark = tmp_path/f'handler-{held.getsockname()[1]}'
        while not mark.exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        proc.terminate()
        _stdout, stderr = proc.communicate(timeout=3)
        assert proc.returncode == 0 and 'Traceback' not in stderr
    finally:
        if held is not None:
            held.close()
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)
