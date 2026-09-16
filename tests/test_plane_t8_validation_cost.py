"""PR-B T8: the capture-validation double pass is paid only where both passes
do something — with the finding-3 semantics (the reason the double pass
exists) pinned unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest

from claudlobby.plane import emit_api
from claudlobby.plane.contracts import ContractViolation, FIELD_POLICY
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import CaptureConfigError, emit
from claudlobby.plane.ids import mint_work_item_id


def _counting_validate(monkeypatch):
    calls = {"n": 0}
    real = emit_api.validate_request

    def spy(raw):
        calls["n"] += 1
        return real(raw)

    monkeypatch.setattr(emit_api, "validate_request", spy)
    return calls


def _comm(body="hello") -> dict:
    return {
        "event_type": "communication", "emitter": "t8", "fleet": "f",
        "payload": {"msg_id": "msg_" + "1" * 32, "sender": "bot:f/a",
                    "message_class": "notice", "body": body, "privacy": "full"},
    }


def _task(summary=None) -> dict:
    p = {"work_item_id": mint_work_item_id(), "event": "progress"}
    if summary is not None:
        p["summary"] = summary
    return {"event_type": "task", "emitter": "t8", "fleet": "f", "payload": p}


def _arm_full(root: Path):
    d = root / "state" / "plane"
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture.json").write_text('{"*": "full"}')


def test_communication_validates_twice_by_ruling(tmp_path, monkeypatch):
    """REVERSED from T8's single-pass (#1372 review F1): raw validation for
    communications is load-bearing — capture LAUNDERED a malformed payload
    (list-of-pairs, privacy='bogus') into a committed row when skipped. The
    double pass is the price of the capture rewrite."""
    calls = _counting_validate(monkeypatch)
    emit(tmp_path, _comm())
    assert calls["n"] == 2


def test_untransformed_task_validates_once(tmp_path, monkeypatch):
    _arm_full(tmp_path)                      # full mode: capture is identity
    calls = _counting_validate(monkeypatch)
    emit(tmp_path, _task(summary="kept whole in full mode"))
    assert calls["n"] == 1


def test_metadata_task_without_content_validates_once(tmp_path, monkeypatch):
    calls = _counting_validate(monkeypatch)
    emit(tmp_path, _task(summary=None))      # nothing for capture to drop
    assert calls["n"] == 1


def test_transformed_task_still_validates_twice(tmp_path, monkeypatch):
    calls = _counting_validate(monkeypatch)
    emit(tmp_path, _task(summary="dropped by metadata mode"))
    assert calls["n"] == 2, "a capture-changed request validates its stored form"
    conn = connect(db_path(tmp_path))
    detail = conn.execute(
        "SELECT detail FROM events WHERE kind='task'").fetchone()[0]
    conn.close()
    assert detail is None or "dropped by" not in detail


# --- the finding-3 semantics the optimization must NOT move -----------------

def test_overcap_authored_summary_still_rejects_pre_capture(tmp_path):
    cap = FIELD_POLICY[("task", "summary")]["cap"]
    with pytest.raises(ContractViolation):
        emit(tmp_path, _task(summary="x" * (cap + 1)))
    assert not db_path(tmp_path).exists()


def test_broken_capture_config_still_raises_before_any_db(tmp_path):
    """Ordering moved by #1372 F1 (raw validation now precedes capture for
    every family — the laundering fix), so the pin is what actually matters:
    a broken policy still fails LOUDLY and before any db access."""
    d = tmp_path / "state" / "plane"
    d.mkdir(parents=True)
    (d / "capture.json").write_text('{"*": "ful"}')
    with pytest.raises(CaptureConfigError):
        emit(tmp_path, _comm())
    assert not db_path(tmp_path).exists()


def test_metadata_comm_still_drops_body_with_proof(tmp_path):
    emit(tmp_path, {**_comm(body="secret"), "fleet": "unarmed"})
    conn = connect(db_path(tmp_path))
    row = conn.execute(
        "SELECT body, body_sha256, privacy FROM communications").fetchone()
    conn.close()
    assert row["privacy"] == "metadata" and row["body"] is None
    assert row["body_sha256"]