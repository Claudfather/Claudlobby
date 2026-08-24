"""Phase-2 T3: lib/plane-parity.py — fixtures both directions, the mismatch
battery, and unreachable-vs-empty per source_state's rule."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.plane.db import db_path
from claudlobby.plane.emit_api import emit

PARITY = Path(__file__).resolve().parent.parent / "lib" / "plane-parity.py"


def _emit_with_ref(root: Path, legacy_id: str, suffix: str) -> None:
    emit(root, {
        "event_type": "communication",
        "emitter": "parity-test",
        "fleet": "example-fleet",
        "source_ref": f"dispatch-log:{legacy_id}",
        "payload": {
            "msg_id": "msg_" + suffix * 32,
            "sender": "bot:example-fleet/mgr",
            "message_class": "task_request",
            "privacy": "full",
        },
    })


def _ledger(tmp_path: Path, rows: list) -> Path:
    p = tmp_path / "dispatch-log.jsonl"
    p.write_text("".join(
        (json.dumps(r) if isinstance(r, dict) else r) + "\n" for r in rows
    ))
    return p


def _run(ledger: Path, db: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(PARITY), "--legacy", str(ledger),
         "--ledger-name", "dispatch-log", "--id-field", "task_id",
         "--db", str(db), *extra],
        capture_output=True, text=True,
    )


def test_clean_parity_rc0(tmp_path):
    _emit_with_ref(tmp_path, "tsk_aaa", "1")
    _emit_with_ref(tmp_path, "tsk_bbb", "2")
    ledger = _ledger(tmp_path, [
        {"task_id": "tsk_aaa", "ts": "2026-08-24T01:00:00+00:00"},
        {"task_id": "tsk_bbb", "ts": "2026-08-24T02:00:00+00:00"},
    ])
    r = _run(ledger, db_path(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "matched: 2" in r.stdout


def test_missing_in_plane_rc1_named(tmp_path):
    _emit_with_ref(tmp_path, "tsk_aaa", "1")
    ledger = _ledger(tmp_path, [
        {"task_id": "tsk_aaa", "ts": "t"},
        {"task_id": "tsk_lost", "ts": "t"},
    ])
    r = _run(ledger, db_path(tmp_path))
    assert r.returncode == 1
    assert "missing in plane:  1" in r.stdout and "tsk_lost" in r.stdout


def test_missing_in_legacy_rc1_named(tmp_path):
    _emit_with_ref(tmp_path, "tsk_ghost", "3")
    ledger = _ledger(tmp_path, [])
    r = _run(ledger, db_path(tmp_path))
    assert r.returncode == 1
    assert "missing in legacy: 1" in r.stdout and "tsk_ghost" in r.stdout


def test_empty_ledger_with_empty_plane_is_clean_and_says_so(tmp_path):
    _emit_with_ref(tmp_path, "tsk_x", "4")  # creates the db; different prefix below
    ledger = _ledger(tmp_path, [])
    r = subprocess.run(
        [sys.executable, str(PARITY), "--legacy", str(ledger),
         "--ledger-name", "report-back", "--id-field", "task_id",
         "--db", str(db_path(tmp_path))],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "nothing to compare" in r.stdout


def test_absent_ledger_refuses_rc3(tmp_path):
    _emit_with_ref(tmp_path, "tsk_x", "5")
    r = _run(tmp_path / "nope.jsonl", db_path(tmp_path))
    assert r.returncode == 3
    assert "UNREACHABLE" in r.stderr
    assert "missing" not in r.stdout


def test_absent_db_refuses_rc3(tmp_path):
    ledger = _ledger(tmp_path, [{"task_id": "tsk_a", "ts": "t"}])
    r = _run(ledger, tmp_path / "no.db")
    assert r.returncode == 3 and "UNREACHABLE" in r.stderr


def test_directory_ledger_refuses_rc3(tmp_path):
    _emit_with_ref(tmp_path, "tsk_x", "6")
    d = tmp_path / "adir"
    d.mkdir()
    r = _run(d, db_path(tmp_path))
    assert r.returncode == 3 and "UNREACHABLE" in r.stderr


def test_unversioned_db_refuses_rc3(tmp_path):
    import sqlite3

    ledger = _ledger(tmp_path, [{"task_id": "tsk_a", "ts": "t"}])
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    r = _run(ledger, bare)
    assert r.returncode == 3 and "no plane schema" in r.stderr


def test_malformed_and_unjoinable_rows_disclosed_not_dropped(tmp_path):
    _emit_with_ref(tmp_path, "tsk_aaa", "7")
    ledger = _ledger(tmp_path, [
        {"task_id": "tsk_aaa", "ts": "t"},
        "not json at all",
        {"bot": "no-id-here", "ts": "t"},
    ])
    r = _run(ledger, db_path(tmp_path))
    assert r.returncode == 0, "unjoinable rows are disclosed, not mismatches"
    assert "malformed: 1" in r.stdout
    assert "unjoinable[no task_id]: 1" in r.stdout
    assert "could not be joined" in r.stdout


def test_since_window_filters_both_sides(tmp_path):
    _emit_with_ref(tmp_path, "tsk_old", "8")   # occurred_at = now (inside window)
    ledger = _ledger(tmp_path, [
        {"task_id": "tsk_old", "ts": "2020-01-01T00:00:00+00:00"},
    ])
    r = _run(ledger, db_path(tmp_path), "--since", "2025-01-01T00:00:00+00:00")
    # the ledger row is windowed OUT; the plane row (fresh occurred_at) stays
    assert r.returncode == 1
    assert "missing in legacy: 1" in r.stdout