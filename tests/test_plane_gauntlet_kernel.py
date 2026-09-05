"""Gauntlet-round regression pins — kernel tier.

Pins for the post-merge review gauntlet's kernel findings: intra-batch
duplicate ids as a contract verdict, the per-batch identity memo, the
capture-config single load, the migrate steady-state guard, project_key
anchoring, the WIRE_TO_KIND single source, the one activation constant,
the unified byte-cap enforcement, the telegram-carrier derivation fixture
(§6b #1 at the derivation level, not just the DDL), the daemon total read
deadline, and the socket client's reply-shape/size guards.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from claudlobby.plane import contracts, emit_api, ingest, migrations, queries
from claudlobby.plane.contracts import (
    ContractViolation,
    TaskEvent,
    WorkItem,
    Workstream,
    WorkstreamEvent,
)
from claudlobby.plane.daemon import _recv_line
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch

LIB = Path(__file__).resolve().parent.parent / "lib"
H = "a" * 32


def _sys_event(event_id=None):
    req = {"event_type": "system", "emitter": "gauntlet",
           "payload": {"event": "daemon_started"}}
    if event_id:
        req["event_id"] = event_id
    return req


def _dispatch_triple():
    """The dispatch door's construct triple with deliberately REPEATED
    aliases (sender == created_by == assigned_by; assignee == recipient)."""
    sender = "bot:f/lead"
    worker = "bot:f/w1"
    return [
        {"event_type": "work_item", "emitter": "gauntlet", "fleet": "f",
         "payload": {"work_item_id": f"wi_{H}", "title": "t",
                     "created_by": sender}},
        {"event_type": "assignment", "emitter": "gauntlet", "fleet": "f",
         "payload": {"assignment_id": f"asg_{H}", "work_item_id": f"wi_{H}",
                     "assignee": worker, "assigned_by": sender,
                     "dispatch_msg_id": f"msg_{H}"}},
        {"event_type": "communication", "emitter": "gauntlet", "fleet": "f",
         "payload": {"msg_id": f"msg_{H}", "sender": sender,
                     "recipient": worker, "message_class": "task_request",
                     "body": "do the thing"}},
    ]


# ---------------------------------------------------------------------------
# Intra-batch duplicate event_id -> contract verdict, zero rows (S7)
# ---------------------------------------------------------------------------
def test_intra_batch_duplicate_id_is_a_contract_verdict(tmp_path):
    dup = "ev_" + "b" * 32
    with pytest.raises(ContractViolation, match="intra-batch duplicate"):
        emit_batch(tmp_path, [_sys_event(dup), _sys_event(dup)])
    conn = connect(db_path(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0] == 0
    conn.close()


def test_distinct_ids_still_land(tmp_path):
    out = emit_batch(tmp_path, [_sys_event(), _sys_event()])
    assert [o.status for o in out] == ["committed", "committed"]


# ---------------------------------------------------------------------------
# Per-batch identity memo (S11): one resolve per unique alias
# ---------------------------------------------------------------------------
def test_batch_resolves_each_alias_once(tmp_path, monkeypatch):
    calls = []
    real = ingest.resolve_party

    def counting(conn, alias, now):
        calls.append(alias)
        return real(conn, alias, now)

    monkeypatch.setattr(ingest, "resolve_party", counting)
    emit_batch(tmp_path, _dispatch_triple())
    # 5 resolve sites (created_by, assignee, assigned_by, sender, recipient)
    # over 2 unique aliases -> exactly 2 calls, not 5.
    assert sorted(set(calls)) == ["bot:f/lead", "bot:f/w1"]
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Capture config loads at most once per batch, lazily (S14)
# ---------------------------------------------------------------------------
class TestCaptureLoadOnce:
    def test_content_batch_loads_once(self, tmp_path, monkeypatch):
        calls = []
        real = emit_api._load_capture_config

        def counting(root):
            calls.append(root)
            return real(root)

        monkeypatch.setattr(emit_api, "_load_capture_config", counting)
        emit_batch(tmp_path, _dispatch_triple())
        assert len(calls) == 1  # work_item + communication share one load

    def test_content_free_batch_never_loads(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            emit_api, "_load_capture_config",
            lambda root: calls.append(root) or {},
        )
        emit_batch(tmp_path, [_sys_event()])
        assert calls == []

    def test_broken_config_still_passes_content_free_batches(self, tmp_path):
        """Semantic preservation: pre-fix, a broken capture.json failed only
        content-bearing batches; the single-load must stay LAZY so a pure
        transmission/system batch keeps succeeding."""
        cfg = tmp_path / "state" / "plane"
        cfg.mkdir(parents=True)
        (cfg / "capture.json").write_text("{not json")
        out = emit_batch(tmp_path, [_sys_event()])
        assert out[0].status == "committed"
        with pytest.raises(emit_api.CaptureConfigError):
            emit_batch(tmp_path, _dispatch_triple())


# ---------------------------------------------------------------------------
# migrate(): steady state never re-reads migration files (S10)
# ---------------------------------------------------------------------------
def test_migrate_steady_state_skips_file_enumeration(tmp_path, monkeypatch):
    conn = connect(db_path(tmp_path))
    assert migrations.migrate(conn) == migrations.SCHEMA_USER_VERSION

    def boom():
        raise AssertionError("steady-state migrate must not read files")

    monkeypatch.setattr(migrations, "_migration_files", boom)
    assert migrations.migrate(conn) == migrations.SCHEMA_USER_VERSION
    conn.close()


# ---------------------------------------------------------------------------
# project_key anchoring (S4) — pydantic pattern SEARCHES without anchors
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["Foo-Bar", "UPPER", "x Y", "-lead", "9lead"])
def test_project_key_rejects_unanchored_matches(bad):
    with pytest.raises(Exception):
        Workstream(workstream_id="ws-x", title="t", opened_by="a",
                   project_key=bad)
    with pytest.raises(Exception):
        WorkItem(work_item_id=f"wi_{H}", title="t", created_by="a",
                 project_key=bad)


def test_project_key_accepts_slugs():
    Workstream(workstream_id="ws-x", title="t", opened_by="a",
               project_key="foo-bar2")
    WorkItem(work_item_id=f"wi_{H}", title="t", created_by="a",
             project_key="alpha")


# ---------------------------------------------------------------------------
# WIRE_TO_KIND: one declaration, both consumers (C8)
# ---------------------------------------------------------------------------
def test_wire_to_kind_is_single_sourced():
    assert ingest.WIRE_TO_KIND is contracts.WIRE_TO_KIND
    table, values = ingest._family_values(
        WorkstreamEvent(workstream_id="ws-x", event="progressed"),
        lambda alias: "uid_x",
    )
    assert table == "events"
    assert values["kind"] == contracts.WIRE_TO_KIND["workstream_event"]


def test_every_stream_family_kind_agrees_with_the_map():
    """For each stream family, the kind _family_values stamps must be what
    WIRE_TO_KIND says (identity for unsuffixed names) — F4 was exactly a
    consumer missing this mapping."""
    samples = {
        "transmission": contracts.Transmission(
            msg_id=f"msg_{H}", state="send_attempted", carrier="tmux",
            attempt_no=1, destination="w1"),
        "task": TaskEvent(event="progress", work_item_id=f"wi_{H}"),
        "workstream_event": WorkstreamEvent(
            workstream_id="ws-x", event="progressed"),
        "system": contracts.SystemEvent(event="daemon_started"),
    }
    for family, payload in samples.items():
        table, values = ingest._family_values(payload, lambda alias: "uid_x")
        assert table == "events"
        assert values["kind"] == contracts.WIRE_TO_KIND.get(family, family), family


# ---------------------------------------------------------------------------
# ONE activation constant (C2)
# ---------------------------------------------------------------------------
def test_activation_set_has_one_definition():
    assert queries._TX_OPEN is queries._TX_ACTIVATION
    assert set(queries.ACTIVATION_TX_EVENTS) == {
        "pane_submitted", "carrier_accepted", "recipient_acknowledged",
    }
    for sql in (queries.ATTENTION_SQL, queries.TASK_STATUS_SQL):
        for token in queries.ACTIVATION_TX_EVENTS:
            assert token in sql


# ---------------------------------------------------------------------------
# Unified byte-cap enforcement still rejects over-cap (S23)
# ---------------------------------------------------------------------------
def test_content_caps_still_reject():
    from claudlobby.plane.registries import FIELD_POLICY
    over = "x" * (FIELD_POLICY[("work_item", "body")]["cap"] + 1)
    with pytest.raises(Exception, match="exceeds"):
        WorkItem(work_item_id=f"wi_{H}", title="t", created_by="a", body=over)
    over = "x" * (FIELD_POLICY[("task", "summary")]["cap"] + 1)
    with pytest.raises(Exception, match="exceeds"):
        TaskEvent(event="progress", work_item_id=f"wi_{H}", summary=over)
    over = "x" * (FIELD_POLICY[("workstream_event", "note")]["cap"] + 1)
    with pytest.raises(Exception, match="exceeds"):
        WorkstreamEvent(workstream_id="ws-x", event="progressed", note=over)


# ---------------------------------------------------------------------------
# §6b #1 telegram fixture at the DERIVATION level (spec-lens minor #3)
# ---------------------------------------------------------------------------
def test_carrier_accepted_reads_open_at_derivation(tmp_path):
    """The DDL matrix covers ingest; this covers the reducer: a telegram
    dispatch whose only activation evidence is carrier_accepted must read
    'open' in TASK_STATUS_SQL and stay OUT of attention."""
    emit_batch(tmp_path, _dispatch_triple() + [{
        "event_type": "transmission", "emitter": "gauntlet", "fleet": "f",
        "payload": {"msg_id": f"msg_{H}", "attempt_no": 1,
                    "carrier": "telegram-tgpost", "destination": "-1001",
                    "state": "carrier_accepted"},
    }])
    conn = connect(db_path(tmp_path))
    status = {r[0]: r[1] for r in conn.execute(queries.TASK_STATUS_SQL)}
    attention = [r[0] for r in conn.execute(
        queries.ATTENTION_SQL,
        queries.attention_params("1970-01-01T00:00:00+00:00")).fetchall()]
    conn.close()
    assert status[f"asg_{H}"] == "open"
    assert attention == []


# ---------------------------------------------------------------------------
# Daemon: TOTAL read deadline beats a trickling client (S1, probed Major)
# ---------------------------------------------------------------------------
def test_recv_line_total_deadline_defeats_trickle():
    server, client = socket.socketpair()
    stop = threading.Event()

    def trickle():
        while not stop.is_set():
            try:
                client.sendall(b"x")
            except OSError:
                return
            time.sleep(0.1)

    t = threading.Thread(target=trickle, daemon=True)
    t.start()
    start = time.monotonic()
    try:
        with pytest.raises(OSError):
            _recv_line(server, timeout=0.5)
    finally:
        stop.set()
        server.close()
        client.close()
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"deadline did not bound the read ({elapsed:.1f}s)"


def test_recv_line_normal_request_unaffected():
    server, client = socket.socketpair()
    client.sendall(b'{"events": []}\n')
    try:
        assert _recv_line(server, timeout=5.0) == b'{"events": []}\n'
    finally:
        server.close()
        client.close()


# ---------------------------------------------------------------------------
# Socket client: reply-shape and reply-size guards (S5, S6)
# ---------------------------------------------------------------------------
def _client_against(tmp_path, reply_fn) -> subprocess.CompletedProcess:
    # Short socket dir — pytest tmp_path overruns the 104-byte sun_path cap
    # (the same reason the daemon binds .s<8hex> names).
    import tempfile
    sock_dir = Path(tempfile.mkdtemp(prefix="pg-", dir="/tmp/claude"
                    if Path("/tmp/claude").is_dir() else None))
    sock_path = sock_dir / "s.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(1)

    def serve():
        conn, _ = listener.accept()
        conn.settimeout(5)
        try:
            reply_fn(conn)
        except OSError:
            pass  # client hung up first (deadline/size guard) — expected
        finally:
            conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        return subprocess.run(
            [sys.executable, "-S", "-E", str(LIB / "plane-socket-client.py"),
             "--socket", str(sock_path),
             "--finalize-to", str(tmp_path / "fin.json"),
             "--timeout", "3"],
            input=json.dumps({"events": [_sys_event()]}),
            capture_output=True, text=True, timeout=30,
        )
    finally:
        listener.close()
        import shutil as _sh
        _sh.rmtree(sock_dir, ignore_errors=True)


def test_non_object_reply_is_transport_rc5(tmp_path):
    def reply(conn):
        while b"\n" not in conn.recv(65536):
            pass
        conn.sendall(b"[]\n")

    r = _client_against(tmp_path, reply)
    assert r.returncode == 5, (r.returncode, r.stderr)
    assert "non-object reply" in r.stderr


def test_oversized_reply_is_bounded_rc5(tmp_path):
    def reply(conn):
        while b"\n" not in conn.recv(65536):
            pass
        conn.sendall(b"a" * 200_000)

    r = _client_against(tmp_path, reply)
    assert r.returncode == 5, (r.returncode, r.stderr)
    assert "oversized reply" in r.stderr


def test_parse_refusals_are_single_owner(tmp_path):
    r = subprocess.run(
        [sys.executable, "-S", "-E", str(LIB / "plane-socket-client.py"),
         "--bogus-flag"],
        input="", capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 2
    assert r.stderr.count("plane-socket-client:") == 1  # one refusal, not two
