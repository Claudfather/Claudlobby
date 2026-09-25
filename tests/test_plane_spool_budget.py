"""Bounded recovery uses scratch roots and never starts a service or child process."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from claudlobby.plane import spool
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.ids import ensure_host_uid, mint_event_id
from claudlobby.plane.migrations import migrate


@pytest.fixture
def scene(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PLANE_SOCKET", str(tmp_path / "unused.sock"))
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    root = tmp_path / "root"
    root.mkdir()
    conn = connect(db_path(root))
    migrate(conn)
    host = ensure_host_uid(root / "state")
    yield root, conn, host
    conn.close()
    assert not (tmp_path / "unused.sock").exists()


def put(root, name, *, timestamp="2026-09-25T00:00:00+00:00", padding="", request=None):
    request = request or {
        "event_type": "system", "emitter": "spool-budget-test",
        "event_id": mint_event_id(), "occurred_at": timestamp,
        "payload": {"event": "recovery_probe", "subject_kind": "host",
                    "subject": "host:fixture", "data": {"name": name, "padding": padding}},
    }
    path = spool.spool_dir(root) / f"{name}.json"
    path.write_text(json.dumps({"requests": [request], "spooled_at": timestamp,
                                "attempts": 0, "history": []}))
    return path


def ordered_scan(monkeypatch):
    """Pin enumeration, independently of APFS/ext4 directory ordering."""
    actual = spool.os.scandir
    class Scan:
        def __init__(self, directory):
            with actual(directory) as entries:
                self.entries = iter(sorted(entries, key=lambda entry: entry.name))
        def __next__(self):
            return next(self.entries)
        def __iter__(self):
            return self
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
        def close(self):
            pass
    monkeypatch.setattr(spool.os, "scandir", Scan)


def test_release_failure_attempts_every_claim_and_retries_before_new_work(scene, monkeypatch):
    root, conn, host = scene
    ordered_scan(monkeypatch)
    for name in ("a", "b", "c"):
        put(root, name)
    cursor = spool.RecoveryCursor()
    actual_rename = spool.os.rename
    releases = []
    def fail_one_release(source, target):
        if ".inflight." in str(source):
            releases.append(Path(source).name.split(".json")[0])
            if Path(target).name == "a.json":
                raise OSError("injected release failure")
        return actual_rename(source, target)
    monkeypatch.setattr(spool.os, "rename", fail_one_release)
    def fail_ingest(*args, **kwargs):
        raise RuntimeError("injected interruption")
    actual_ingest = spool._ingest_claim
    monkeypatch.setattr(spool, "_ingest_claim", fail_ingest)
    with pytest.raises(RuntimeError, match="injected interruption"):
        spool.drain_pass(root, conn, host, cursor=cursor,
                         budget=spool.DrainBudget(8, 100000, 100))
    assert releases == ["a", "b", "c"]
    assert len(list(spool.spool_dir(root).glob("*.inflight.*"))) == 1
    cursor.close()  # closing a scan must never forget release ownership
    monkeypatch.setattr(spool, "_ingest_claim", actual_ingest)
    report = spool.drain_pass(root, conn, host, cursor=cursor)
    assert report.release_errors and report.more_work and not report.budget_exhausted
    assert report.processed == 0
    monkeypatch.setattr(spool.os, "rename", actual_rename)
    report = spool.drain_pass(root, conn, host, cursor=cursor,
                             budget=spool.DrainBudget(8, 100000, 100))
    assert report.ingested == 3
    assert not cursor.pending_releases
    assert not list(spool.spool_dir(root).glob("*.inflight.*"))


def test_slow_retry_is_deferred_across_rewinds_while_healthy_work_advances(scene, monkeypatch):
    root, conn, host = scene
    ordered_scan(monkeypatch)
    failed = put(root, "a-failed")
    for name in ("b-good", "c-good"):
        put(root, name)
    clock = [0.0]
    monkeypatch.setattr(spool.time, "monotonic", lambda: clock[0])
    actual_ingest = spool.ingest_many
    calls = []
    def fail_slowly(conn, items, **kwargs):
        name = items[0][1].data["name"]
        calls.append(name)
        if name == "a-failed":
            clock[0] += 1
            raise sqlite3.OperationalError("database is locked")
        return actual_ingest(conn, items, **kwargs)
    monkeypatch.setattr(spool, "ingest_many", fail_slowly)
    cursor = spool.RecoveryCursor()
    reports = [spool.drain_pass(root, conn, host, cursor=cursor) for _ in range(6)]
    assert calls.count("a-failed") == 1
    assert sum(report.ingested for report in reports) == 2
    assert json.loads(failed.read_text())["attempts"] == 1
    assert not list(spool.spool_dir(root).glob("*.inflight.*"))
    cursor.clear_deferred_retries()  # the daemon's next normal interval
    spool.drain_pass(root, conn, host, cursor=cursor)
    assert json.loads(failed.read_text())["attempts"] == 2


@pytest.mark.parametrize("kwargs", [
    {"max_entries": 0}, {"max_entries": True}, {"max_bytes": -1},
    {"max_bytes": 1.5}, {"max_elapsed_s": 0}, {"max_elapsed_s": True},
    {"max_elapsed_s": float("nan")}, {"max_elapsed_s": float("inf")},
])
def test_invalid_budget_refuses(kwargs):
    with pytest.raises(ValueError):
        spool.DrainBudget(**kwargs)


@pytest.mark.parametrize("limiter", ["entries", "bytes"])
def test_admission_bounds_claims_and_body_reads(scene, monkeypatch, limiter):
    root, conn, host = scene
    ordered_scan(monkeypatch)
    paths = [put(root, name) for name in ("a", "b", "c", "d")]
    sizes = [path.stat().st_size for path in paths]
    reads = []
    actual_read = spool._read_claimed_json
    def read(path, size):
        reads.append(size)
        return actual_read(path, size)
    monkeypatch.setattr(spool, "_read_claimed_json", read)
    budget = spool.DrainBudget(2, 100000, 100) if limiter == "entries" else spool.DrainBudget(8, sum(sizes[:2]) - 1, 100)
    report = spool.drain_pass(root, conn, host, budget=budget)
    expected = 2 if limiter == "entries" else 1
    assert report.processed == report.ingested == len(reads) == expected
    assert report.payload_bytes == sum(reads) <= budget.max_bytes
    assert report.budget_exhausted and report.more_work and report.remaining is None
    assert not list(spool.spool_dir(root).glob("*.inflight.*"))
    assert len(list(spool.spool_dir(root).glob("*.json"))) == 4 - expected


@pytest.mark.parametrize("malformed", [False, True])
def test_one_oversized_envelope_progresses_with_explicit_byte_overrun(scene, monkeypatch, malformed):
    root, conn, host = scene
    ordered_scan(monkeypatch)
    first = put(root, "a-large", padding="x" * 16384)
    if malformed:
        first.write_bytes(b"{" + b"x" * 16384)
    size = first.stat().st_size
    put(root, "b-small")
    cursor = spool.RecoveryCursor()
    budget = spool.DrainBudget(8, 1024, 100)
    report = spool.drain_pass(root, conn, host, cursor=cursor, budget=budget)
    assert report.processed == 1
    assert report.payload_bytes == size > budget.max_bytes
    assert report.quarantined == int(malformed)
    assert report.ingested == int(not malformed)
    assert not first.exists()
    assert spool.drain_pass(root, conn, host, cursor=cursor, budget=budget).ingested == 1


@pytest.mark.parametrize("slow_phase", ["read", "ingest"])
def test_time_budget_yields_and_releases_unprocessed_claims(scene, monkeypatch, slow_phase):
    root, conn, host = scene
    ordered_scan(monkeypatch)
    for name in ("a", "b", "c"):
        put(root, name)
    clock = [0.0]
    monkeypatch.setattr(spool.time, "monotonic", lambda: clock[0])
    target = "_read_claimed_json" if slow_phase == "read" else "_ingest_claim"
    original = getattr(spool, target)
    calls = []
    def slow(*args, **kwargs):
        calls.append(1)
        result = original(*args, **kwargs)
        clock[0] += 1
        return result
    monkeypatch.setattr(spool, target, slow)
    cursor = spool.RecoveryCursor()
    first = spool.drain_pass(root, conn, host, cursor=cursor)
    assert first.ingested == first.processed == len(calls) == 1
    assert first.budget_exhausted
    assert not list(spool.spool_dir(root).glob("*.inflight.*"))
    reports = [spool.drain_pass(root, conn, host, cursor=cursor) for _ in range(4)]
    assert sum(report.ingested for report in reports) == 2
    assert not cursor.pending_releases


def test_cursor_passes_more_than_64_live_claims_to_find_healthy_work(scene, monkeypatch):
    root, conn, host = scene
    ordered_scan(monkeypatch)
    sd = spool.spool_dir(root)
    for index in range(130):
        (sd / f"a{index:03d}.json.inflight.{os.getpid()}.abcd").write_text("live owner's bytes")
    put(root, "z-healthy")
    cursor = spool.RecoveryCursor()
    reports = [spool.drain_pass(root, conn, host, cursor=cursor,
                               budget=spool.DrainBudget(64, 100000, 100)) for _ in range(3)]
    assert [report.ingested for report in reports] == [0, 0, 1]
    assert all(report.budget_exhausted for report in reports[:2])
    assert len(list(sd.glob("*.inflight.*"))) == 130
    assert all(path.read_text() == "live owner's bytes" for path in sd.glob("*.inflight.*"))


def test_repeated_passes_preserve_replay_quarantine_and_dead_claim_recovery(scene, monkeypatch):
    root, conn, host = scene
    ordered_scan(monkeypatch)
    first = put(root, "a-original")
    duplicate = spool.spool_dir(root) / "b-duplicate.json"
    duplicate.write_bytes(first.read_bytes())
    dead = put(root, "c-dead")
    dead.rename(dead.with_name(dead.name + ".inflight.123456789.abcd"))
    monkeypatch.setattr(spool, "_pid_alive", lambda pid: False)
    put(root, "d-invalid").write_text("{invalid")
    cursor = spool.RecoveryCursor()
    reports = []
    for _ in range(12):
        reports.append(spool.drain_pass(root, conn, host, cursor=cursor,
                                       budget=spool.DrainBudget(2, 100000, 100)))
        if not reports[-1].more_work:
            break
    assert sum(report.ingested for report in reports) == 2
    assert sum(report.duplicates for report in reports) == 1
    assert sum(report.quarantined for report in reports) == 1
    assert reports[-1].remaining == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE event='recovery_probe'").fetchone()[0] == 2
    assert not list(spool.spool_dir(root).glob("*.inflight.*"))
    assert (spool.quarantine_dir(root) / "d-invalid.json.reason").exists()


def test_order_is_chronological_within_each_admitted_batch_and_late_arrivals_survive(scene, monkeypatch):
    root, conn, host = scene
    ordered_scan(monkeypatch)
    put(root, "a-newer", timestamp="2026-09-25T02:00:00+00:00")
    put(root, "b-older", timestamp="2026-09-25T01:00:00+00:00")
    put(root, "c-oldest", timestamp="2026-09-25T00:00:00+00:00")
    actual = spool.ingest_many
    observed = []
    def record(conn, items, **kwargs):
        observed.append(items[0][1].data["name"])
        return actual(conn, items, **kwargs)
    monkeypatch.setattr(spool, "ingest_many", record)
    cursor = spool.RecoveryCursor()
    budget = spool.DrainBudget(2, 100000, 100)
    spool.drain_pass(root, conn, host, cursor=cursor, budget=budget)
    assert observed == ["b-older", "a-newer"]  # bounded batch, not a global FIFO claim
    put(root, "d-late", timestamp="2026-09-24T00:00:00+00:00")
    for _ in range(4):
        spool.drain_pass(root, conn, host, cursor=cursor, budget=budget)
    assert sorted(observed) == ["a-newer", "b-older", "c-oldest", "d-late"]
    times = conn.execute("SELECT occurred_at FROM events WHERE event='recovery_probe' ORDER BY occurred_at").fetchall()
    assert len(times) == 4 and times[0][0].startswith("2026-09-24")


def test_retry_cap_is_counted_by_normal_intervals_and_quarantines_once(scene, monkeypatch):
    root, conn, host = scene
    pending = put(root, "unavailable")
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(spool, "ingest_many", unavailable)
    cursor = spool.RecoveryCursor()
    for attempt in range(1, spool.MAX_ATTEMPTS + 1):
        cursor.clear_deferred_retries()
        report = spool.drain_pass(root, conn, host, cursor=cursor)
        if attempt < spool.MAX_ATTEMPTS:
            assert json.loads(pending.read_text())["attempts"] == attempt
            assert report.more_work and not report.budget_exhausted
            for _ in range(3):
                assert spool.drain_pass(root, conn, host, cursor=cursor).processed == 0
        else:
            assert report.quarantined == 1
    entry = json.loads((spool.quarantine_dir(root) / pending.name).read_text())
    assert entry["attempts"] == spool.MAX_ATTEMPTS
    assert len(entry["history"]) == spool.HISTORY_LIMIT


def test_one_shot_failure_exposes_cursor_instead_of_forgetting_live_claim(scene, monkeypatch):
    root, conn, host = scene
    put(root, "a")
    actual_rename = spool.os.rename
    actual_ingest = spool._ingest_claim
    def refuse_release(source, target):
        if ".inflight." in str(source):
            raise OSError("release unavailable")
        return actual_rename(source, target)
    def interrupted(*args, **kwargs):
        raise RuntimeError("interrupted ingest")
    monkeypatch.setattr(spool.os, "rename", refuse_release)
    monkeypatch.setattr(spool, "_ingest_claim", interrupted)
    with pytest.raises(spool.SpoolReleaseError) as caught:
        spool.drain_pass(root, conn, host)
    assert isinstance(caught.value.__context__, RuntimeError)
    cursor = caught.value.cursor
    assert len(cursor.pending_releases) == 1
    monkeypatch.setattr(spool.os, "rename", actual_rename)
    monkeypatch.setattr(spool, "_ingest_claim", actual_ingest)
    assert spool.drain_pass(root, conn, host, cursor=cursor).ingested == 1


def test_complete_mixed_family_envelope_is_one_transaction_even_over_budget(scene):
    root, conn, host = scene
    first = json.loads(put(root, "batch").read_text())
    first["requests"].append({
        "event_type": "communication", "emitter": "spool-budget-test",
        "event_id": mint_event_id(), "occurred_at": "2026-09-25T00:00:00+00:00",
        "fleet": "fixture", "payload": {
            "msg_id": "msg_" + "1" * 32, "sender": "bot:fixture/alpha",
            "message_class": "notice", "body": "second member", "privacy": "full",
        },
    })
    path = spool.spool_dir(root) / "batch.json"
    path.write_text(json.dumps(first))
    conn.execute("CREATE TRIGGER reject_communication BEFORE INSERT ON communications "
                 "BEGIN SELECT RAISE(ABORT, 'injected second-family failure'); END")
    conn.commit()
    report = spool.drain_pass(root, conn, host, budget=spool.DrainBudget(1, 1, 100))
    assert report.quarantined == 1 and report.processed == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE event='recovery_probe'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 0
    conn.execute("DROP TRIGGER reject_communication")
    conn.commit()
    # Replaying the entire quarantined envelope with the same ids can succeed:
    # the first family left neither a partial row nor an idempotency tombstone.
    (spool.quarantine_dir(root) / path.name).rename(path)
    cursor = spool.RecoveryCursor()
    reports = [spool.drain_pass(root, conn, host, cursor=cursor,
                               budget=spool.DrainBudget(1, 1, 100)) for _ in range(3)]
    assert sum(report.ingested for report in reports) == 1
    assert sum(report.processed for report in reports) == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE event='recovery_probe'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1
