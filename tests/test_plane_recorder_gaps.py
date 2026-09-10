"""Chunk W — recorder-gaps in the header.

The recorder failing to RECORD — events refused (quarantine), a stalled or
unreadable spool — is the worst gap: it makes an empty board a lie. So
/api/overview surfaces it in `totals.recorder_gaps`, which the header renders as
a loud chip beside the presence counts. It reads the SAME doors the trust panel
reads (scan_spool / _spool_pending), so header and trust cannot disagree.

Fixtures seed the real spool/quarantine dirs (the trust tests' idiom); the db is
created through the real emit spine so /api/overview has something to open.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from claudlobby.plane.emit_api import emit_batch  # noqa: E402
from claudlobby.plane.view import create_app  # noqa: E402


def _seed_db(root: Path) -> None:
    # one real event so the read-only view has a db to open
    emit_batch(root, [{"event_type": "work_item", "emitter": "t", "fleet": "f",
                       "occurred_at": datetime.now(timezone.utc).isoformat(),
                       "payload": {"work_item_id": "wi_" + "a" * 32,
                                   "title": "t", "created_by": "bot:f/mgr"}}])


def _spool(root: Path) -> Path:
    s = root / "state" / "plane" / "spool"
    s.mkdir(parents=True, exist_ok=True)
    return s


def _gaps(root: Path) -> list:
    _seed_db(root)
    return TestClient(create_app(root)).get(
        "/api/overview").json()["data"]["totals"]["recorder_gaps"]


def test_no_recorder_gaps_when_healthy(tmp_path):
    # no spool, no quarantine — the header stays clean
    assert _gaps(tmp_path) == []


def test_quarantined_events_surface_as_a_gap(tmp_path):
    q = _spool(tmp_path) / "quarantine"
    q.mkdir()
    (q / "ev_b.json").write_text("{}")
    (q / "ev_b.json.reason").write_text("poison: schema violation")
    kinds = {g["kind"]: g for g in _gaps(tmp_path)}
    assert "quarantined" in kinds
    assert kinds["quarantined"]["count"] == 1
    assert kinds["quarantined"]["label"] == "1 event refused"


def test_a_stalled_spool_surfaces(tmp_path):
    # a pending spool entry older than the stall window = ingest is stuck
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    (_spool(tmp_path) / "ev_a.json").write_text('{"spooled_at": "%s"}' % old)
    assert any(g["kind"] == "spool_stalled" for g in _gaps(tmp_path))


def test_a_fresh_spool_is_not_a_gap(tmp_path):
    # a recent spool entry is draining, not stalled — no gap (no false alarm)
    fresh = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    (_spool(tmp_path) / "ev_a.json").write_text('{"spooled_at": "%s"}' % fresh)
    assert not any(g["kind"] == "spool_stalled" for g in _gaps(tmp_path))


def test_a_naive_spooled_at_does_not_crash(tmp_path):
    # a producer that dropped its tz offset must not 500 the overview
    (_spool(tmp_path) / "ev_a.json").write_text(
        '{"spooled_at": "2020-01-01T00:00:00"}')
    assert any(g["kind"] == "spool_stalled" for g in _gaps(tmp_path))


def test_header_gap_count_agrees_with_the_trust_panel(tmp_path):
    # DRY: the header's refused-count IS the trust tab's refused-count (one door)
    q = _spool(tmp_path) / "quarantine"
    q.mkdir()
    for i in range(3):
        (q / f"ev_{i}.json").write_text("{}")
        (q / f"ev_{i}.json.reason").write_text("poison")
    _seed_db(tmp_path)
    client = TestClient(create_app(tmp_path))
    ov = client.get("/api/overview").json()["data"]["totals"]["recorder_gaps"]
    trust = client.get("/api/trust").json()["data"]
    hdr = next(g["count"] for g in ov if g["kind"] == "quarantined")
    assert hdr == trust["quarantined"] == 3
