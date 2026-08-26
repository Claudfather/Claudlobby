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
    assert "missing in plane: 1" in r.stdout and "tsk_lost" in r.stdout


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


def test_since_boundary_skew_is_disclosed_not_a_mismatch(tmp_path):
    """PR-#1345 review F10 (the reviewer's exact probe): one fact stamped
    11:59:59 in the ledger and 12:00:01 in the plane across a 12:00:00
    cutoff must not read as missing-in-legacy — the twin exists, just below
    the window."""
    _emit_with_ref(tmp_path, "tsk_old", "8")   # occurred_at = now (in window)
    ledger = _ledger(tmp_path, [
        {"task_id": "tsk_old", "ts": "2020-01-01T00:00:00+00:00"},
    ])
    r = _run(ledger, db_path(tmp_path), "--since", "2025-01-01T00:00:00+00:00")
    assert r.returncode == 0, r.stdout
    assert "missing in legacy: 0" in r.stdout
    assert "window-skew" in r.stdout and "tsk_old" in r.stdout


def test_plane_only_id_with_no_ledger_twin_still_fails_under_since(tmp_path):
    """The skew bucket must not swallow REAL losses: a plane row with no
    ledger twin anywhere (windowed or not) stays missing-in-legacy."""
    _emit_with_ref(tmp_path, "tsk_truly_lost", "9")
    ledger = _ledger(tmp_path, [])
    r = _run(ledger, db_path(tmp_path), "--since", "2025-01-01T00:00:00+00:00")
    assert r.returncode == 1
    assert "missing in legacy: 1" in r.stdout and "tsk_truly_lost" in r.stdout


def test_like_metacharacters_in_ledger_name_do_not_leak(tmp_path):
    """PR-#1345 review F8 (+ own pre-probe): '%'/'_' in a ledger name are
    text, not wildcards — 'dispatch_log' must not match 'dispatchXlog'."""
    emit(tmp_path, {
        "event_type": "communication", "emitter": "parity-test",
        "fleet": "example-fleet", "source_ref": "dispatchXlog:tsk_other",
        "payload": {"msg_id": "msg_" + "e" * 32,
                    "sender": "bot:example-fleet/mgr",
                    "message_class": "notice", "privacy": "full"},
    })
    ledger = _ledger(tmp_path, [{"task_id": "tsk_other", "ts": "t"}])
    r = subprocess.run(
        [sys.executable, str(PARITY), "--legacy", str(ledger),
         "--ledger-name", "dispatch_log", "--id-field", "task_id",
         "--db", str(db_path(tmp_path))],
        capture_output=True, text=True,
    )
    assert r.returncode == 1, "a wildcard match would have read parity-clean"
    assert "missing in plane: 1" in r.stdout


def test_per_table_duplicate_rows_are_flagged(tmp_path):
    """PR-#1345 review F4 (multiplicity): two rows for one fact in ONE table
    is a door double-writing — set-collapse hid it."""
    _emit_with_ref(tmp_path, "tsk_dup", "a")
    _emit_with_ref(tmp_path, "tsk_dup", "b")   # same source_ref, second comm row
    ledger = _ledger(tmp_path, [{"task_id": "tsk_dup", "ts": "t"}])
    r = _run(ledger, db_path(tmp_path))
    assert r.returncode == 1
    assert "2 rows in communications" in r.stdout


def test_field_mismatch_is_flagged_and_match_is_clean(tmp_path):
    """PR-#1345 review F4 (fields): same id, different content must fail."""
    emit(tmp_path, {
        "event_type": "work_item", "emitter": "parity-test",
        "fleet": "example-fleet", "source_ref": "dispatch-log:tsk_f1",
        "payload": {"work_item_id": "wi_" + "1" * 32, "title": "build the thing",
                    "created_by": "bot:example-fleet/mgr"},
    })
    ledger = _ledger(tmp_path, [
        {"task_id": "tsk_f1", "ts": "t", "task": "build the thing"},
    ])
    clean = _run(ledger, db_path(tmp_path), "--field", "task=work_items.title")
    assert clean.returncode == 0, clean.stdout
    assert "field mismatches: 0" in clean.stdout
    drifted = _ledger(tmp_path, [
        {"task_id": "tsk_f1", "ts": "t", "task": "build a DIFFERENT thing"},
    ])
    r = _run(drifted, db_path(tmp_path), "--field", "task=work_items.title")
    assert r.returncode == 1
    assert "ledger 'build a DIFFERENT thing' != plane title=" in r.stdout


def test_absent_field_flag_is_disclosed(tmp_path):
    _emit_with_ref(tmp_path, "tsk_nf", "c")
    ledger = _ledger(tmp_path, [{"task_id": "tsk_nf", "ts": "t"}])
    r = _run(ledger, db_path(tmp_path))
    assert r.returncode == 0
    assert "field comparison: none requested" in r.stdout


def test_bad_field_spec_refuses(tmp_path):
    _emit_with_ref(tmp_path, "tsk_bs", "d")
    ledger = _ledger(tmp_path, [{"task_id": "tsk_bs", "ts": "t"}])
    r = _run(ledger, db_path(tmp_path), "--field", "task=nosuchtable.title")
    assert r.returncode == 2, "a malformed call is rc 2, never clean or broken"
    assert "unknown table" in r.stderr