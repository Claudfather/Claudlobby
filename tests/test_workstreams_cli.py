"""Pure formatting checks for the Plane workstream projection."""

from __future__ import annotations

from claudlobby.workstreams import format_list, format_show


def _entry(**kw) -> dict:
    base = {
        "id": "ws-x", "fleet": "f", "title": "X", "project": None,
        "status": "active", "owner_bot": "alex", "next": "do thing",
        "opened_ts": "2026-07-01T00:00:00Z", "last_progress_ts": "2026-07-01T00:00:00Z",
        "lease_expires_ts": "2026-08-01T00:00:00Z",
        "task_ids": [], "refs": {"issues": [], "prs": []}, "renewals": [],
    }
    base.update(kw)
    return base


def test_format_list_empty():
    assert format_list({}) == "No workstreams."


def test_format_list_orders_active_before_terminal_and_has_columns():
    ws = {
        "ws-a": _entry(id="ws-a", status="done"),
        "ws-b": _entry(id="ws-b", status="active"),
    }
    lines = format_list(ws).splitlines()
    assert "STATUS" in lines[0]
    active_idx = next(i for i, ln in enumerate(lines) if "ws-b" in ln)
    done_idx = next(i for i, ln in enumerate(lines) if "ws-a" in ln)
    assert active_idx < done_idx


def test_format_show_includes_key_fields():
    out = format_show(
        _entry(task_ids=["t-1-abcd"], renewals=[{"ts": "x", "note": "waiting on review"}])
    )
    assert "ws-x — X" in out
    assert "owner:    alex" in out
    assert "t-1-abcd" in out
    assert "renewals: 1" in out
    assert "waiting on review" in out
