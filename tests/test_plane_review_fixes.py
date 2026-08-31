"""PR #1341 external-review regression battery — every counterexample the
review produced, pinned as a fixture (findings numbered as in the review).

F1  short os.write reported as durable success
F2  first-use spool tree creation not crash-durable
F3  capture policy silently discarding (broken config; capture-before-validate)
F4  unversioned wire/spool envelopes
F5  drainers racing read-then-unlink (claim protocol)
F6  cross-family duplicate classification
F7  task-status ignoring dispatch/acknowledgement evidence
F8  malformed spool shapes crashing or silently deleting
F9  CLI taxonomy escaping as tracebacks
F10 raced migration bypassing the newer-db guard
F11 wrong oldest-entry derivation
F12 predictable tmp adoption publishing foreign modes
F13 headline claims unpinned (SQLITE_FULL path, fsync spies, batch rollback)
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.plane import PLANE_SCHEMA_VERSION
from claudlobby.plane.contracts import ContractViolation, FIELD_POLICY
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import CaptureConfigError, emit, emit_batch
from claudlobby.plane.ids import (
    ensure_host_uid,
    mint_assignment_id,
    mint_event_id,
    mint_msg_id,
    mint_work_item_id,
)
from claudlobby.plane.migrations import DowngradeError, migrate
from claudlobby.plane import migrations as migrations_mod
from claudlobby.plane import spool as spool_mod
from claudlobby.plane.spool import (
    _claim,
    _write_bytes_secure,
    drain,
    quarantine_dir,
    spool_dir,
    spool_write,
)


def _comm(msg_suffix="2", body="hello", privacy="full") -> dict:
    return {
        "event_type": "communication",
        "emitter": "review-battery",
        "fleet": "example-fleet",
        "payload": {
            "msg_id": "msg_" + msg_suffix * 32,
            "sender": "bot:example-fleet/alpha",
            "message_class": "notice",
            "body": body,
            "privacy": privacy,
        },
    }


def _fin(req: dict, eid: str) -> dict:
    return {
        **req,
        "event_id": eid,
        "occurred_at": "2026-08-24T00:00:00+00:00",
        "schema_version": PLANE_SCHEMA_VERSION,
    }


@pytest.fixture()
def env(tmp_path: Path):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    host = ensure_host_uid(tmp_path / "state")
    yield tmp_path, conn, host
    conn.close()


# --- F1: short writes ------------------------------------------------------

def test_f1_short_os_write_still_persists_every_byte(env, monkeypatch):
    root, conn, host = env
    real_write = os.write
    calls = []

    def short_write(fd, data):
        chunk = bytes(data)[:11]
        calls.append(len(chunk))
        return real_write(fd, chunk)

    monkeypatch.setattr(os, "write", short_write)
    p = spool_write(root, [_fin(_comm(), mint_event_id())], "db locked")
    monkeypatch.undo()
    assert len(calls) > 1, "the short-write path was never exercised"
    data = json.loads(p.read_text())     # truncated JSON would raise here
    assert data["requests"][0]["payload"]["body"] == "hello"


def test_f1_zero_progress_write_raises_not_loops(env, monkeypatch):
    root, conn, host = env
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(spool_mod.SpoolWriteError):
        spool_write(root, [_fin(_comm(), mint_event_id())], "db locked")


# --- F2: durable first-use directory creation ------------------------------

def test_f2_spool_tree_creation_fsyncs_every_parent(tmp_path: Path, monkeypatch):
    synced: set[int] = set()
    real_fsync = os.fsync

    def spy(fd):
        synced.add(os.fstat(fd).st_ino)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    spool_write(tmp_path, [_fin(_comm(), mint_event_id())], "db locked")
    monkeypatch.undo()
    for d in (tmp_path,                       # gained state/
              tmp_path / "state",             # gained plane/
              tmp_path / "state" / "plane"):  # gained spool/
        assert os.stat(d).st_ino in synced, f"parent never fsynced: {d}"


# --- F3: capture policy ----------------------------------------------------

def _capture_path(root: Path) -> Path:
    p = root / "state" / "plane"
    p.mkdir(parents=True, exist_ok=True)
    return p / "capture.json"


def test_f3_broken_capture_config_fails_loud_not_metadata(tmp_path: Path):
    _capture_path(tmp_path).write_text('{"*": "full"')          # invalid JSON
    with pytest.raises(CaptureConfigError):
        emit(tmp_path, _comm())


def test_f3_unknown_mode_value_fails_loud(tmp_path: Path):
    _capture_path(tmp_path).write_text('{"*": "ful"}')          # typo'd mode
    with pytest.raises(CaptureConfigError):
        emit(tmp_path, _comm())


def test_f3_absent_config_still_defaults_metadata(tmp_path: Path):
    emit(tmp_path, _comm())
    conn = connect(db_path(tmp_path))
    row = conn.execute("SELECT body, privacy FROM communications").fetchone()
    conn.close()
    assert row["privacy"] == "metadata" and row["body"] is None


def test_f3_overcap_task_summary_rejects_before_capture_strips_it(tmp_path: Path):
    cap = FIELD_POLICY[("task", "summary")]["cap"]
    req = {
        "event_type": "task",
        "emitter": "review-battery",
        "fleet": "example-fleet",
        "payload": {
            "work_item_id": mint_work_item_id(),
            "event": "progress",
            "summary": "x" * (cap + 1),
        },
    }
    with pytest.raises(ContractViolation):
        emit(tmp_path, req)              # metadata mode used to strip + accept
    assert not db_path(tmp_path).exists(), "refused before any db access"


def test_f3_overcap_work_item_body_rejects(tmp_path: Path):
    cap = FIELD_POLICY[("work_item", "body")]["cap"]
    req = {
        "event_type": "work_item",
        "emitter": "review-battery",
        "fleet": "example-fleet",
        "payload": {
            "work_item_id": mint_work_item_id(),
            "title": "t",
            "created_by": "bot:example-fleet/alpha",
            "body": "y" * (cap + 1),
        },
    }
    with pytest.raises(ContractViolation):
        emit(tmp_path, req)


# --- F4: versioned wire + spool --------------------------------------------

def test_f4_emit_stamps_and_stores_schema_version(tmp_path: Path):
    emit(tmp_path, _comm())
    conn = connect(db_path(tmp_path))
    v = conn.execute("SELECT schema_version FROM communications").fetchone()[0]
    conn.close()
    assert v == PLANE_SCHEMA_VERSION


def test_f4_future_schema_version_refused_on_emit(tmp_path: Path):
    req = {**_comm(), "schema_version": "9.0.0"}
    with pytest.raises(ContractViolation):
        emit(tmp_path, req)
    assert not list(spool_dir(tmp_path).glob("*.json")), "must never spool"


def test_f4_spool_preserves_version_and_future_version_quarantines(env, monkeypatch):
    root, conn, host = env
    # Spooled entries carry the envelope's version verbatim:
    p = spool_write(root, [_fin(_comm(), mint_event_id())], "db locked")
    stored = json.loads(p.read_text())
    assert stored["requests"][0]["schema_version"] == PLANE_SCHEMA_VERSION
    # A future-version entry (newer writer spooled it, older code drains):
    future = _fin(_comm("7"), mint_event_id())
    future["schema_version"] = "9.0.0"
    spool_write(root, [future], "db locked")
    report = drain(root, conn, host)
    assert report.quarantined == 1 and report.ingested == 1
    reasons = list(quarantine_dir(root).glob("*.reason"))
    assert any("schema_version" in r.read_text() for r in reasons)


# --- F5: claim protocol ----------------------------------------------------

def test_f5_claimed_entry_invisible_to_second_drainer(env):
    root, conn, host = env
    p = spool_write(root, [_fin(_comm(), mint_event_id())], "db locked")
    claimed = _claim(p)                  # drainer A holds the claim (live pid)
    assert claimed is not None
    report = drain(root, conn, host)     # drainer B
    assert report.ingested == 0 and report.quarantined == 0
    assert claimed.exists(), "a live claim is the claimant's property"
    os.rename(claimed, p)                # A releases
    assert drain(root, conn, host).ingested == 1


def test_f5_lost_claim_race_returns_none_not_error(env):
    root, conn, host = env
    p = spool_write(root, [_fin(_comm(), mint_event_id())], "db locked")
    first = _claim(p)
    assert first is not None
    assert _claim(p) is None             # the loser skips; no FileNotFoundError
    os.rename(first, p)


def test_f5_dead_pid_inflight_recovered_and_replayed(env):
    root, conn, host = env
    p = spool_write(root, [_fin(_comm(), mint_event_id())], "db locked")
    stale = p.with_name(p.name + ".inflight.999999999.deadbeef")
    os.rename(p, stale)                  # a drainer that died mid-claim
    report = drain(root, conn, host)
    assert report.ingested == 1
    assert not list(spool_dir(root).glob("*.inflight.*"))


# --- F6: cross-family duplicates -------------------------------------------

def test_f6_replay_under_other_family_is_conflict_not_duplicate(tmp_path: Path):
    eid = mint_event_id()
    emit(tmp_path, {**_comm(), "event_id": eid})
    task = {
        "event_type": "task",
        "emitter": "review-battery",
        "fleet": "example-fleet",
        "event_id": eid,                 # a communication's id
        "payload": {"work_item_id": mint_work_item_id(), "event": "progress"},
    }
    with pytest.raises(ContractViolation, match="idempotency conflict"):
        emit(tmp_path, task)
    conn = connect(db_path(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='task'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1
    conn.close()


def test_f6_same_family_replay_still_reports_duplicate(tmp_path: Path):
    eid = mint_event_id()
    req = {**_comm(), "event_id": eid}
    assert emit(tmp_path, req).status == "committed"
    assert emit(tmp_path, req).status == "duplicate"


# --- F7: evidence-based activation -----------------------------------------

def _seed_assignment(root: Path, *, dispatch_msg, tx_events=()) -> str:
    """tx_events: (state, attempt_no) pairs — a real attempt emits several
    rows under ONE attempt_no (send_attempted then its verdict)."""
    aid = mint_assignment_id()
    wi = mint_work_item_id()
    batch = [
        {"event_type": "work_item", "emitter": "fx", "fleet": "example-fleet",
         "payload": {"work_item_id": wi, "title": "t",
                     "created_by": "bot:example-fleet/mgr"}},
        {"event_type": "assignment", "emitter": "fx", "fleet": "example-fleet",
         "payload": {"assignment_id": aid, "work_item_id": wi,
                     "assignee": "bot:example-fleet/w1",
                     "assigned_by": "bot:example-fleet/mgr",
                     "dispatch_msg_id": dispatch_msg}},
    ]
    if dispatch_msg:
        batch.append(
            {"event_type": "communication", "emitter": "fx",
             "fleet": "example-fleet",
             "payload": {"msg_id": dispatch_msg,
                         "sender": "bot:example-fleet/mgr",
                         "recipient": "bot:example-fleet/w1",
                         "message_class": "task_request",
                         "command_type": "task", "privacy": "full"}})
    for state, attempt_no in tx_events:
        batch.append(
            {"event_type": "transmission", "emitter": "fx",
             "fleet": "example-fleet",
             "payload": {"msg_id": dispatch_msg, "attempt_no": attempt_no,
                         "carrier": "tmux", "destination": "w1",
                         "state": state}})
    emit_batch(root, batch)
    return aid


def _statuses(root: Path) -> dict:
    from claudlobby.plane.queries import TASK_STATUS_SQL
    conn = connect(db_path(root))
    out = dict(conn.execute(TASK_STATUS_SQL).fetchall())
    conn.close()
    return out


def test_f7_activation_ladder_fixtures(tmp_path: Path):
    unsent = _seed_assignment(tmp_path, dispatch_msg=None)
    failed = _seed_assignment(
        tmp_path, dispatch_msg=mint_msg_id(),
        tx_events=(("send_attempted", 1), ("failed", 1)))
    submitted = _seed_assignment(
        tmp_path, dispatch_msg=mint_msg_id(),
        tx_events=(("send_attempted", 1), ("pane_submitted", 1)))
    acked = _seed_assignment(
        tmp_path, dispatch_msg=mint_msg_id(),
        tx_events=(("send_attempted", 1), ("pane_submitted", 1),
                   ("recipient_acknowledged", 1)))
    in_flight = _seed_assignment(
        tmp_path, dispatch_msg=mint_msg_id(),
        tx_events=(("send_attempted", 1), ("failed", 1), ("send_attempted", 2)))
    unresolved = _seed_assignment(
        tmp_path, dispatch_msg=mint_msg_id(),
        tx_events=(("send_attempted", 1),))
    statuses = _statuses(tmp_path)
    assert statuses[unsent] == "created_not_sent"
    assert statuses[failed] == "dispatch_failed", (
        "an attempt whose own verdict is failed has concluded")
    # §6b #1 (PR-B) deliberately moved this rung: submission-class evidence IS
    # activation for the tmux carrier — pane_submitted occupies 'open', and a
    # real ack row tightens onto the same rung rather than gating it.
    assert statuses[submitted] == "open"
    assert statuses[acked] == "open"
    assert statuses[in_flight] == "pending_unacknowledged", (
        "a retry in flight after a failed attempt is outstanding, not failed")
    assert statuses[unresolved] == "pending_unacknowledged", (
        "send_attempted without a verdict keeps the attempt outstanding")


def test_f7_terminal_dominates_late_ack_replay(tmp_path: Path):
    msg = mint_msg_id()
    aid = _seed_assignment(
        tmp_path, dispatch_msg=msg,
        tx_events=(("pane_submitted", 1), ("recipient_acknowledged", 1)))
    wi = mint_work_item_id()
    emit_batch(tmp_path, [
        {"event_type": "task", "emitter": "fx", "fleet": "example-fleet",
         "payload": {"work_item_id": wi, "assignment_id": aid,
                     "event": "completed"}},
        # a late replayed ack after closure:
        {"event_type": "transmission", "emitter": "fx", "fleet": "example-fleet",
         "payload": {"msg_id": msg, "attempt_no": 2, "carrier": "tmux",
                     "destination": "w1", "state": "recipient_acknowledged"}},
    ])
    assert _statuses(tmp_path)[aid] == "completed"


# --- F8: malformed spool shapes --------------------------------------------

def test_f8_malformed_spool_shapes_quarantine_never_crash_or_vanish(env):
    root, conn, host = env
    sd = spool_dir(root)
    shapes = {
        f"ev_{'a' * 32}.json": "[]",
        f"ev_{'b' * 32}.json": '{"requests": null}',
        f"ev_{'c' * 32}.json": '{"requests": []}',
    }
    for name, text in shapes.items():
        (sd / name).write_text(text)
    report = drain(root, conn, host)
    assert report.quarantined == 3
    assert report.duplicates == 0, "empty batch must not read as duplicate"
    assert report.ingested == 0
    q = quarantine_dir(root)
    for name in shapes:
        assert (q / name).exists(), f"{name} silently vanished"
        assert (q / (name + ".reason")).exists()


# --- F9: CLI taxonomy -------------------------------------------------------

def _run(args, stdin=None):
    return subprocess.run(
        [sys.executable, "-m", "claudlobby", *args],
        input=stdin, capture_output=True, text=True,
    )


@pytest.mark.parametrize("payload", ["[]", "null", "42", '"x"'])
def test_f9_wrong_shape_single_request_exits_2(tmp_path: Path, payload):
    r = _run(["--root", str(tmp_path), "emit", "communication", "--json", "-"],
             stdin=payload)
    assert r.returncode == 2, r.stderr
    assert "Traceback" not in r.stderr


def test_f9_wrong_shape_batch_member_exits_2(tmp_path: Path):
    r = _run(["--root", str(tmp_path), "emit-batch", "--json", "-"],
             stdin='{"events": [42]}')
    assert r.returncode == 2, r.stderr
    assert "Traceback" not in r.stderr


def _make_newer_db(root: Path) -> None:
    conn = connect(db_path(root))
    migrate(conn)
    conn.execute("PRAGMA user_version = 99")
    conn.close()


def test_f9_status_and_retry_exit_4_on_newer_db(tmp_path: Path):
    _make_newer_db(tmp_path)
    for cmd in (["plane", "status"], ["plane", "spool", "retry"]):
        r = _run(["--root", str(tmp_path), *cmd])
        assert r.returncode == 4, (cmd, r.returncode, r.stderr)
        assert "Traceback" not in r.stderr


# --- F10: raced migration downgrade bypass ---------------------------------

def test_f10_raced_newer_migration_refuses_downgrade(tmp_path: Path, monkeypatch):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    conn.close()
    conn = connect(db_path(tmp_path))
    versions = iter([1, 99])             # entry read sane; post-failure read raced
    monkeypatch.setattr(
        migrations_mod, "_user_version",
        lambda c: next(versions, 99),
    )
    monkeypatch.setattr(
        migrations_mod, "_migration_files",
        lambda: [(2, "THIS IS NOT SQL;")],
    )
    with pytest.raises(DowngradeError):
        migrations_mod.migrate(conn)
    conn.close()


def test_f10_0002_applies_against_an_existing_v1_database(tmp_path: Path, monkeypatch):
    """The upgrade path, not just the fresh path: a db stamped v1 by 0001
    alone must gain 0002's index from a plain migrate()."""
    files = migrations_mod._migration_files()
    assert [n for n, _ in files] == [1, 2, 3, 4, 5]
    conn = connect(db_path(tmp_path))
    monkeypatch.setattr(migrations_mod, "SCHEMA_USER_VERSION", 1)
    monkeypatch.setattr(migrations_mod, "_migration_files", lambda: files[:1])
    assert migrations_mod.migrate(conn) == 1
    monkeypatch.undo()
    assert migrations_mod.migrate(conn) == 5
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    assert "idx_events_task_assignment" in names


# --- F11: operator surface --------------------------------------------------

def test_f11_oldest_is_min_spooled_at_not_filename_order(env):
    root, conn, host = env
    sd = spool_dir(root)
    young = {"event_ids": ["ev_" + "1" * 32], "spooled_at": "2026-08-24T10:00:00+00:00",
             "error": "x", "attempts": 0, "history": [], "requests": [_fin(_comm("4"), "ev_" + "1" * 32)]}
    old = {"event_ids": ["ev_" + "2" * 32], "spooled_at": "2026-08-20T10:00:00+00:00",
           "error": "x", "attempts": 0, "history": [], "requests": [_fin(_comm("5"), "ev_" + "2" * 32)]}
    # filename order (aaa < fff) puts the YOUNG entry first:
    (sd / ("ev_" + "a" * 32 + ".json")).write_text(json.dumps(young))
    (sd / ("ev_" + "f" * 32 + ".json")).write_text(json.dumps(old))
    from claudlobby.commands.plane import _oldest_spooled_at
    from claudlobby.plane.spool import spool_entries
    assert _oldest_spooled_at(spool_entries(root)) == "2026-08-20T10:00:00+00:00"


def test_f11_spool_inspect_prints_entry_with_history(env):
    root, conn, host = env
    name = "ev_" + "9" * 32 + ".json"
    entry = {"event_ids": ["ev_" + "9" * 32], "spooled_at": "2026-08-24T00:00:00+00:00",
             "error": "locked", "attempts": 2,
             "history": [{"at": "2026-08-24T00:01:00+00:00", "error": "locked"}],
             "requests": [_fin(_comm("6"), "ev_" + "9" * 32)]}
    (spool_dir(root) / name).write_text(json.dumps(entry))
    r = _run(["--root", str(root), "plane", "spool", "inspect", name])
    assert r.returncode == 0, r.stderr
    assert "history" in r.stdout and "locked" in r.stdout


def test_f11_doctor_healthy_0_quarantine_1(tmp_path: Path):
    emit(tmp_path, _comm())
    r = _run(["--root", str(tmp_path), "plane", "doctor"])
    assert r.returncode == 0, r.stdout + r.stderr
    qname = "ev_" + "d" * 32 + ".json"
    (quarantine_dir(tmp_path) / qname).write_text("{}")
    r = _run(["--root", str(tmp_path), "plane", "doctor"])
    assert r.returncode == 1
    assert "quarantine" in r.stdout


def test_f11_doctor_flags_broken_capture_config(tmp_path: Path):
    _capture_path(tmp_path).write_text('{"*": "ful"}')
    r = _run(["--root", str(tmp_path), "plane", "doctor"])
    assert r.returncode == 1
    assert "capture config" in r.stdout


# --- F12: tmp publication ---------------------------------------------------

def test_f12_preexisting_0644_tmp_cannot_poison_published_mode(tmp_path: Path):
    d = tmp_path / "d"
    d.mkdir()
    legacy_tmp = d / "target.json.tmp"    # the OLD predictable name
    legacy_tmp.write_text("poison")
    os.chmod(legacy_tmp, 0o644)
    old_umask = os.umask(0o022)
    try:
        p = _write_bytes_secure(d, "target.json", b'{"ok": true}\n')
    finally:
        os.umask(old_umask)
    import stat
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert p.read_text() == '{"ok": true}\n'
    assert legacy_tmp.read_text() == "poison", "foreign tmp must be untouched"


# --- F13: headline claims pinned -------------------------------------------

class _FullError(sqlite3.OperationalError):
    sqlite_errorcode = 13                # SQLITE_FULL, message NOT in fallback


def test_f13_emit_under_sqlite_full_spools_by_error_code(tmp_path: Path, monkeypatch):
    from claudlobby.plane import emit_api

    def full_ingest(conn, items, *, host_uid):
        raise _FullError("synthetic full condition")

    monkeypatch.setattr(emit_api, "ingest_many", full_ingest)
    out = emit(tmp_path, _comm())
    assert out.status == "spooled"
    monkeypatch.undo()
    # recovery: a later drain with a healthy db ingests it
    conn = connect(db_path(tmp_path))
    migrate(conn)
    host = ensure_host_uid(tmp_path / "state")
    assert drain(tmp_path, conn, host).ingested == 1
    conn.close()


def test_f13_spool_write_fsyncs_file_and_directory(env, monkeypatch):
    root, conn, host = env
    spool_dir(root)                      # pre-create so only the WRITE is spied
    kinds = []
    real_fsync = os.fsync

    def spy(fd):
        import stat
        kinds.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    spool_write(root, [_fin(_comm(), mint_event_id())], "db locked")
    monkeypatch.undo()
    assert "file" in kinds, "entry file itself must be fsynced"
    assert "dir" in kinds, "directory entry must be fsynced"


def test_f13_batch_second_item_failure_rolls_back_first(tmp_path: Path):
    taken = mint_event_id()
    emit(tmp_path, {**_comm("3"), "event_id": taken})
    fresh = mint_event_id()
    batch = [
        {**_comm("4"), "event_id": fresh},
        {"event_type": "task", "emitter": "fx", "fleet": "example-fleet",
         "event_id": taken,              # collides AND conflicts cross-family
         "payload": {"work_item_id": mint_work_item_id(), "event": "progress"}},
    ]
    with pytest.raises((ContractViolation, RuntimeError)):
        emit_batch(tmp_path, batch)
    conn = connect(db_path(tmp_path))
    n = conn.execute(
        "SELECT COUNT(*) FROM communications WHERE event_id = ?", (fresh,)
    ).fetchone()[0]
    conn.close()
    assert n == 0, "item 1 must roll back when item 2 fails (one transaction)"
