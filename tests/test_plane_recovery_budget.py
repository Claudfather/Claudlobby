"""Recovery makes progress while leaving accept opportunities for fresh work."""

import json
from pathlib import Path
import socket
import tempfile
import threading

import pytest

from claudlobby.plane import daemon as daemon_mod
from claudlobby.plane import spool as spool_mod
from claudlobby.plane.ids import mint_event_id


def _request(index):
    return {"event_type": "system", "emitter": "budget-test", "event_id": mint_event_id(),
            "occurred_at": "2026-09-25T00:00:00+00:00", "payload": {
                "event": "recovery_probe", "subject_kind": "host", "subject": "host:fixture",
                "data": {"index": index}}}


@pytest.mark.parametrize("lane", ["spool", "staged"])
@pytest.mark.parametrize("phase", ["startup", "interval"])
def test_backlog_yields_to_a_queued_fresh_client(tmp_path, monkeypatch, lane, phase):
    # Unique short socket path fits native macOS sun_path independently of pytest's root.
    with tempfile.TemporaryDirectory(prefix="plane-budget-") as short:
        sock_path = Path(short) / "plane.sock"
        root = tmp_path / "root"
        root.mkdir()
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("PLANE_SOCKET", str(sock_path))
        started, release, empty_start, seeded = [threading.Event() for _ in range(4)]
        daemon = daemon_mod.PlaneDaemon(root, socket_override=sock_path,
                                        drain_interval=0 if phase == "interval" else 600)
        processed = []
        observed_at_accept = []
        failures = []
        def populate():
            for index in range(130):
                request = _request(index)
                if lane == "spool":
                    spool_mod.spool_write(root, [request], "synthetic backlog")
                else:
                    stage = daemon_mod.staged_dir(root)
                    stage.mkdir(parents=True, exist_ok=True)
                    (stage / f"{index:04d}.batch").write_text(json.dumps({"events": [request]}))
        if phase == "startup":
            populate()
        else:
            optimize = daemon._optimize
            def pause_after_empty_start():
                optimize()
                if not empty_start.is_set():
                    empty_start.set()
                    assert seeded.wait(10), "interval backlog was not populated"
            monkeypatch.setattr(daemon, "_optimize", pause_after_empty_start)
        original = spool_mod.ingest_many if lane == "spool" else daemon_mod.emit_batch
        def slow_first(*args, **kwargs):
            requests = args[1]
            # Spool passes validated objects; staged replay passes dictionaries.
            emitter = (requests[0][0].emitter if lane == "spool" else requests[0].get("emitter"))
            if emitter == "budget-test":
                processed.append(1)
                if len(processed) == 1:
                    started.set()
                    assert release.wait(10), "fresh client was not queued"
            return original(*args, **kwargs)
        monkeypatch.setattr(spool_mod if lane == "spool" else daemon_mod,
                            "ingest_many" if lane == "spool" else "emit_batch", slow_first)
        handle = daemon._handle
        def observe_handle(conn):
            observed_at_accept.append(len(processed))
            return handle(conn)
        monkeypatch.setattr(daemon, "_handle", observe_handle)
        def serve():
            try:
                daemon.serve(install_signals=False)
            except BaseException as exc:
                failures.append(exc)
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            if phase == "interval":
                assert empty_start.wait(10)
                populate()
                seeded.set()
            assert started.wait(10), failures
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(10)
                client.connect(str(sock_path))
                client.sendall(b"\n")  # typed fresh handshake, no extra ingest
                release.set()
                reply = json.loads(client.recv(65536))
            assert reply["code"] == "bad_request"
            assert observed_at_accept and observed_at_accept[0] < 130
        finally:
            release.set()
            seeded.set()
            daemon.stop()
            thread.join(10)
        assert not thread.is_alive()
        assert not failures


@pytest.fixture
def recovery_daemon(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PLANE_SOCKET", str(tmp_path / "unused.sock"))
    daemon = daemon_mod.PlaneDaemon(root, socket_override=tmp_path / "unused.sock")
    monkeypatch.setattr(daemon, "_emit_system", lambda *args: None)
    yield daemon
    daemon.writer.close()
    daemon._stage_cursor.close()
    daemon._spool_cursor.close()
    assert not (tmp_path / "unused.sock").exists()


@pytest.mark.parametrize("operation", ["quarantine", "unlink"])
def test_staged_cleanup_failure_retains_batch_and_backs_off(recovery_daemon, monkeypatch, capsys, operation):
    daemon = recovery_daemon
    stage = daemon_mod.staged_dir(daemon.root)
    stage.mkdir(parents=True)
    batch = stage / "1.batch"
    body = "{invalid" if operation == "quarantine" else json.dumps({"events": [_request(1)]})
    batch.write_text(body)
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: 10.0)
    with monkeypatch.context() as fault:
        if operation == "quarantine":
            def cannot_quarantine(*args, **kwargs):
                raise OSError("injected quarantine failure")
            fault.setattr(daemon_mod, "quarantine_entry", cannot_quarantine)
        else:
            original_unlink = Path.unlink
            def cannot_unlink(path, *args, **kwargs):
                if path == batch:
                    raise OSError("injected unlink failure")
                return original_unlink(path, *args, **kwargs)
            fault.setattr(Path, "unlink", cannot_unlink)
        daemon._replay_staged()
    assert batch.read_text() == body
    assert daemon._next_replay == 40.0 and not daemon._stage_backlog
    assert f"injected {operation} failure" in capsys.readouterr().err
    daemon._replay_staged()  # failure removed; unchanged bytes can now recover
    assert not batch.exists()
    if operation == "unlink":
        conn = daemon.writer.connection()
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event='recovery_probe'").fetchone()[0] == 1
    else:
        assert (spool_mod.quarantine_dir(daemon.root) / "1.json").read_text() == body


def test_continuous_backlog_still_releases_retry_eligibility_each_normal_interval(recovery_daemon, monkeypatch):
    daemon = recovery_daemon
    daemon.drain_interval = 4.0
    clock = [0.0]
    eligible_at = []
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: clock[0])
    marker = daemon.root / "deferred-retry.json"
    def endless_backlog(*args, cursor, **kwargs):
        if marker not in cursor.deferred_retries:
            eligible_at.append(clock[0])
            cursor.deferred_retries.add(marker)
        return spool_mod.DrainReport(processed=1, budget_exhausted=True, more_work=True)
    monkeypatch.setattr(daemon_mod, "drain", endless_backlog)
    monkeypatch.setattr(daemon, "_optimize", lambda: None)
    class Listener:
        def settimeout(self, timeout):
            pass
        def accept(self):
            clock[0] += 1
            if clock[0] >= 15:
                daemon.stop()
            raise socket.timeout()
        def close(self):
            pass
    monkeypatch.setattr(daemon, "_bind", Listener)
    daemon.serve(install_signals=False)
    assert eligible_at[0] == 0.0
    assert len(eligible_at) >= 3, eligible_at
    assert all(4 <= later - earlier <= 5 for earlier, later in zip(eligible_at, eligible_at[1:]))


def test_staged_repeated_passes_complete_more_than_64_files(recovery_daemon, monkeypatch):
    daemon = recovery_daemon
    stage = daemon_mod.staged_dir(daemon.root)
    stage.mkdir(parents=True)
    for index in range(130):
        (stage / f"{index:04d}.batch").write_text(json.dumps({"events": [_request(index)]}))
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: 1.0)
    reports = []
    monkeypatch.setattr(daemon, "_emit_system", lambda event, report: reports.append(report))
    for _ in range(4):
        daemon._replay_staged()
        if not daemon._stage_backlog:
            break
    assert [report["processed"] for report in reports] == [64, 64, 2]
    assert not list(stage.glob("*.batch"))
    conn = daemon.writer.connection()
    assert conn.execute("SELECT COUNT(*) FROM events WHERE event='recovery_probe'").fetchone()[0] == 130


@pytest.mark.parametrize("oversized", [False, True])
def test_staged_byte_target_keeps_one_complete_envelope(recovery_daemon, monkeypatch, oversized):
    daemon = recovery_daemon
    stage = daemon_mod.staged_dir(daemon.root)
    stage.mkdir(parents=True)
    first = stage / "1.batch"
    second = stage / "2.batch"
    events = [_request(1), _request(2)] if oversized else [_request(1)]
    second_events = [_request(3), _request(4)] if oversized else [_request(3)]
    first.write_text(json.dumps({"events": events}))
    second.write_text(json.dumps({"events": second_events}))
    first_bytes, second_bytes = first.stat().st_size, second.stat().st_size
    maximum = 1 if oversized else first_bytes + second_bytes - 1
    monkeypatch.setattr(daemon_mod, "DrainBudget", lambda: spool_mod.DrainBudget(64, maximum, 100))
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: 1.0)
    reads = []
    original_read = daemon_mod._read_claimed_json
    def read(path, size):
        reads.append((path.name, size))
        return original_read(path, size)
    monkeypatch.setattr(daemon_mod, "_read_claimed_json", read)
    calls = []
    original_emit = daemon_mod.emit_batch
    def emit(root, events, **kwargs):
        calls.append(len(events))
        return original_emit(root, events, **kwargs)
    monkeypatch.setattr(daemon_mod, "emit_batch", emit)
    daemon._replay_staged()
    # Directory admission has no global filename-order promise.
    assert len(reads) == 1 and reads[0][1] in (first_bytes, second_bytes)
    assert calls == [len(events)] and first.exists() != second.exists()
    assert (reads[0][1] > maximum) == oversized
    daemon._replay_staged()
    assert not first.exists() and not second.exists()
    conn = daemon.writer.connection()
    assert conn.execute("SELECT COUNT(*) FROM events WHERE event='recovery_probe'").fetchone()[0] == len(events) + len(second_events)
