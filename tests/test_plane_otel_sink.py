"""Private raw storage, privacy and bound; OTLP fixture provenance lives beside fixtures."""
import copy
import json
import os
import stat
import time

import pytest

from claudlobby.plane import otel_sink as sink


def log_body(content='metadata'):
    # Synthetic privacy stress case; deliberately not a canonical producer fixture.
    attrs = [{'key': k, 'value': {'stringValue': 'sensitive'}} for k in sink.CONTENT_KEYS + sink.IDENTITY_KEYS]
    attrs.extend([{'key': 'tool_name', 'value': {'stringValue': 'mcp_tool'}},
                  {'key': 'tool_parameters', 'value': {'stringValue': json.dumps({'mcp_server_name': 'example', 'mcp_tool_name': 'query', 'full_command': 'secret'})}}])
    return {'resourceLogs': [{'resource': {'attributes': [{'key': 'claudlobby.content', 'value': {'stringValue': content}}]},
                             'scopeLogs': [{'logRecords': [{'body': {'stringValue': 'claude_code.tool_result'}, 'attributes': attrs}]}]}]}


@pytest.mark.parametrize('content', ['metadata', None, 'full'])
def test_content_gate_and_modes(tmp_path, content):
    raw = sink.RawSink(tmp_path, days=14)
    body = log_body(content)
    original = copy.deepcopy(body)
    raw.append('logs', body)
    path = sink.sink_dir(tmp_path) / 'logs.jsonl'
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    actual = json.loads(lines[0])
    if content == 'full':
        assert actual == original
    else:
        attrs = actual['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes']
        assert {a['key'] for a in attrs} == {'tool_name', 'tool_parameters'}
        assert json.loads(attrs[1]['value']['stringValue']) == {'mcp_server_name': 'example', 'mcp_tool_name': 'query'}


def test_metrics_unchanged_and_rotation_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(sink, 'MAX_MEGABYTES', .0001)
    raw = sink.RawSink(tmp_path, days=14)
    body = {'resourceMetrics': [], 'padding': 'x'*90}
    for _ in range(20):
        raw.append('metrics', body)
    files = list(raw.path.glob('metrics*.jsonl'))
    assert len(files) == sink.MAX_FILES
    assert json.loads((raw.path/'metrics.jsonl').read_text()) == body
    assert sink.RAW_SINK_CEILING_MIB == 64*15*2 == 1920
    old = time.time() - 16*86400
    for file in files:
        os.utime(file, (old, old))
    raw.prune(time.time())
    assert not list(raw.path.iterdir())


@pytest.mark.parametrize('kind', ['symlink', 'public', 'foreign'])
def test_refuses_nonprivate_sink(tmp_path, monkeypatch, kind):
    path = sink.sink_dir(tmp_path)
    path.parent.mkdir()
    if kind == 'symlink':
        target = tmp_path/'elsewhere'; target.mkdir(mode=0o700); path.symlink_to(target)
    else:
        path.mkdir(mode=0o755 if kind == 'public' else 0o700)
        if kind == 'foreign':
            monkeypatch.setattr(sink.os, 'getuid', lambda: -1)
    with pytest.raises(OSError, match='not_private'):
        sink.RawSink(tmp_path, days=14)
    assert not list(path.iterdir())


def test_scan_unreadable_and_rungs(tmp_path, monkeypatch):
    assert sink.scan_sink(tmp_path).state == 'absent'
    sink.RawSink(tmp_path, days=14)
    monkeypatch.setattr(sink, 'scan_dir', lambda p: (_ for _ in ()).throw(OSError('EIO')))
    scan = sink.scan_sink(tmp_path)
    assert scan.state == 'unreadable'
    assert sink.sink_rung(scan, max_days=14)[0] is False
    assert 'gap, not a zero' in sink.sink_rung(scan, max_days=14)[1]
    assert not sink.sink_rung(sink.SinkScan('not_private'), max_days=14)[0]
    assert not sink.sink_rung(sink.SinkScan('ok', size=1921*1024*1024), max_days=14)[0]
    assert not sink.sink_rung(sink.SinkScan('ok', oldest_mtime=time.time()-16*86400), max_days=14)[0]
    assert 'near the ceiling' in sink.sink_rung(sink.SinkScan('ok', size=1800*1024*1024), max_days=14)[1]


@pytest.mark.parametrize('value', ['x', '0', '-1'])
def test_retention_refuses_empty_window(value):
    with pytest.raises(SystemExit) as exc:
        sink.retention_parser().parse_args(['--retention-days', value])
    assert exc.value.code == 2


def test_retention_scriptline():
    assert sink.retention_parser().parse_args([]).retention_days == 14
    assert sink.retention_days('$CLAUDLOBBY_NATIVE_DIR/plane-otel.sh --port 4319 --retention-days 21') == 21


def test_file_pruned_during_scan_does_not_hide_the_sink(tmp_path, monkeypatch):
    raw = sink.RawSink(tmp_path, days=14)
    from types import SimpleNamespace
    missing = raw.path/'logs-old.jsonl'
    monkeypatch.setattr(sink, 'scan_dir', lambda path: (SimpleNamespace(state='ok'), [missing]))
    assert sink.scan_sink(tmp_path).state == 'ok'
