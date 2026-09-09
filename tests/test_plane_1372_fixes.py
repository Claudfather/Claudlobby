"""PR-#1372 review regression battery — the reviewer's counterexamples,
pinned (findings numbered as in the review)."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from claudlobby.plane.contracts import ContractViolation, validate_request
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit
from claudlobby.plane.ids import mint_assignment_id, mint_event_id, mint_msg_id, mint_work_item_id

LIB = Path(__file__).resolve().parent.parent / "lib"


def test_f1_capture_cannot_launder_malformed_wire(tmp_path):
    """The reviewer's exact probe: list-of-pairs payload + privacy='bogus'
    was rejected by validate_request but COMMITTED by emit()."""
    bad = {"event_type": "communication", "emitter": "f1", "fleet": "f",
           "payload": [["msg_id", "msg_" + "1" * 32], ["sender", "bot:f/a"],
                       ["message_class", "notice"], ["privacy", "bogus"]]}
    with pytest.raises(ContractViolation):
        validate_request(bad)
    with pytest.raises(ContractViolation):
        emit(tmp_path, dict(bad))
    assert not db_path(tmp_path).exists() or connect(db_path(tmp_path)).execute(
        "SELECT COUNT(*) FROM communications").fetchone()[0] == 0


def test_f4_workstream_event_replay_is_duplicate_not_divergence(tmp_path):
    emit(tmp_path, {"event_type": "workstream", "emitter": "f4", "fleet": "f",
                    "payload": {"workstream_id": "ws-x", "title": "t",
                                "opened_by": "bot:f/a"}})
    eid = mint_event_id()
    ev = {"event_type": "workstream_event", "emitter": "f4", "fleet": "f",
          "event_id": eid,
          "payload": {"workstream_id": "ws-x", "event": "progressed"}}
    assert emit(tmp_path, dict(ev)).status == "committed"
    assert emit(tmp_path, dict(ev)).status == "duplicate", (
        "lost-ack socket->CLI replay depends on this classification")


def test_f11_anomaly_token_is_not_a_lifecycle_status(tmp_path):
    from claudlobby.plane.emit_api import emit_batch
    from claudlobby.plane.queries import TASK_STATUS_SQL

    wi, aid, msg = mint_work_item_id(), mint_assignment_id(), mint_msg_id()
    emit_batch(tmp_path, [
        {"event_type": "work_item", "emitter": "f11", "fleet": "f",
         "payload": {"work_item_id": wi, "title": "t",
                     "created_by": "bot:f/a"}},
        {"event_type": "assignment", "emitter": "f11", "fleet": "f",
         "payload": {"assignment_id": aid, "work_item_id": wi,
                     "assignee": "bot:f/w", "assigned_by": "bot:f/a",
                     "dispatch_msg_id": msg}},
        {"event_type": "communication", "emitter": "f11", "fleet": "f",
         "payload": {"msg_id": msg, "sender": "bot:f/a",
                     "message_class": "task_request", "privacy": "full"}},
        {"event_type": "transmission", "emitter": "f11", "fleet": "f",
         "payload": {"msg_id": msg, "attempt_no": 1, "carrier": "tmux",
                     "destination": "w", "state": "pane_submitted"}},
        {"event_type": "task", "emitter": "f11", "fleet": "f",
         "payload": {"work_item_id": wi, "assignment_id": aid,
                     "event": "supplied_id_not_open"}},
    ])
    conn = connect(db_path(tmp_path))
    statuses = {r[0]: r[1] for r in conn.execute(TASK_STATUS_SQL)}
    conn.close()
    assert statuses[aid] == "open", (
        "the JOIN anomaly must never surface as the visible lifecycle state")


def test_f12_carrier_state_matrix_enforced(tmp_path):
    def tx(carrier, state):
        return {"event_type": "transmission", "emitter": "f12", "fleet": "f",
                "payload": {"msg_id": mint_msg_id(), "attempt_no": 1,
                            "carrier": carrier, "destination": "d",
                            "state": state}}

    with pytest.raises(ContractViolation):
        emit(tmp_path, tx("telegram-tgpost", "pane_submitted"))
    with pytest.raises(ContractViolation):
        emit(tmp_path, tx("telegram-bridge", "carrier_queued"))
    with pytest.raises(ContractViolation):
        emit(tmp_path, tx("tmux", "carrier_accepted"))
    assert emit(tmp_path, tx("tmux", "pane_submitted")).status == "committed"
    assert emit(tmp_path, tx("telegram-tgpost", "carrier_accepted")).status == "committed"


def test_f15_arbitrary_listener_is_not_a_daemon(tmp_path):
    import socket as s

    from claudlobby.plane.daemon import probe_daemon

    sockdir = Path("/tmp/claude")
    sockdir.mkdir(exist_ok=True)
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="f15", dir=sockdir))
    path = d / "s"
    srv = s.socket(s.AF_UNIX, s.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    stop = threading.Event()

    def sink():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()          # accepts, replies nothing
            except OSError:
                pass

    t = threading.Thread(target=sink, daemon=True)
    t.start()
    try:
        assert probe_daemon(path) is False, (
            "connect-succeeds is not a daemon — doctor needs the typed reply")
    finally:
        stop.set(); t.join(timeout=3); srv.close()
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_f5_wedged_listener_bounds_the_shim(tmp_path):
    """A live listener that never replies must not hold a door past the
    total deadline (2s default) — the client exits 5 and the shim falls back."""
    import socket as s
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="f5", dir="/tmp/claude"))
    path = d / "s"
    srv = s.socket(s.AF_UNIX, s.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)                  # accepts (kernel backlog), never replies
    try:
        t0 = time.monotonic()
        r = subprocess.run(
            ["python3", "-S", "-E", str(LIB / "plane-socket-client.py"),
             "--socket", str(path), "--finalize-to", str(tmp_path / "fin")],
            input='{"events": [{"event_type": "x"}]}',
            capture_output=True, text=True, timeout=15,
        )
        elapsed = time.monotonic() - t0
        assert r.returncode == 5, (r.returncode, r.stderr)
        assert elapsed < 6, f"wedge held the client {elapsed:.1f}s"
        assert "deadline" in r.stderr or "timed out" in r.stderr
    finally:
        srv.close()
        import shutil
        shutil.rmtree(d, ignore_errors=True)