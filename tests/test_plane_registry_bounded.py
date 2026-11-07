"""Bound returned registry payloads without truncating their SCD partitions."""

import pytest

from claudlobby.plane import registry_read as rr
from claudlobby.plane.emit_api import emit_batch
from tests.test_plane_registry_read import (
    BOT, T1, T2, T3, _bot, _conn, _done, _root, _snap, _tomb,
)


def _emit(root, rows):
    assert {r.status for r in emit_batch(root, rows)} == {"committed"}


def _read_counts(monkeypatch):
    """Count real SQL output rows and JSON decodes, without replacing either."""
    counts = {"rows": 0, "decodes": 0}
    query, decode = rr._q, rr.json.loads

    def counted_query(*args, **kwargs):
        rows = query(*args, **kwargs)
        counts["rows"] += len(rows)
        return rows

    def counted_decode(*args, **kwargs):
        counts["decodes"] += 1
        return decode(*args, **kwargs)

    monkeypatch.setattr(rr, "_q", counted_query)
    monkeypatch.setattr(rr.json, "loads", counted_decode)
    return counts


def test_entity_history_does_not_decode_unrelated_payloads(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _emit(root, [_snap(BOT, str(i), ts, _bot(BOT, model=str(i)))
                 for i, ts in enumerate((T1, T2, T3))]
          + [_snap(f"bot:f/other-{i}", "noise", T1, _bot(f"bot:f/other-{i}"))
             for i in range(8)])
    with _conn(root) as conn:
        counts = _read_counts(monkeypatch)
        rows = rr.entity_history(conn, BOT)
        assert [r["payload"]["model"] for r in rows] == ["0", "1", "2"]
        assert counts == {"rows": 3, "decodes": 3}
        assert rr.entity_history(conn, rows[0]["entity_uid"]) == rows
        counts.update(rows=0, decodes=0)
        assert rr.entity_history(conn, "' OR 1=1 --") == []
        assert counts == {"rows": 0, "decodes": 0}


def test_history_alias_selection_keeps_rename_and_host_windows(tmp_path):
    root = _root(tmp_path)
    renamed = "bot:f/renamed"
    _emit(root, [_snap(BOT, "old", T1, _bot(BOT))])
    host1 = (root / "state/host-uid").read_text().strip()
    # A rename preserves the identity; only the registry's alias changes.
    with _conn(root) as conn:
        conn.execute("UPDATE identity_registry SET alias=?"
                     " WHERE kind='bot_instance' AND alias=?", (renamed, BOT))
    _emit(root, [_snap(renamed, "new", T2, _bot(renamed, "new")),
                 _snap(renamed, "tie", T2, _bot(renamed, "tie"))])
    # A second host sees the same alias/UID, but has an independent window.
    host2 = "host_" + "2" * 32
    (root / "state/host-uid").write_text(host2 + "\n")
    _emit(root, [_snap(renamed, "other-host-1", T1, _bot(renamed, "h2a")),
                 _snap(renamed, "other-host-2", T3, _bot(renamed, "h2b"))])
    with _conn(root) as conn:
        old = rr.entity_history(conn, BOT)
        assert len(old) == 1 and old[0]["valid_to"] == T2
        renamed_rows = rr.entity_history(conn, renamed)
        assert [(r["payload"]["model"], r["valid_to"]) for r in renamed_rows
                if r["host_uid"] == host1] == [("new", T2), ("tie", None)]
        assert [(r["payload"]["model"], r["valid_to"]) for r in renamed_rows
                if r["host_uid"] == host2] == [("h2a", T3), ("h2b", None)]
        by_uid = rr.entity_history(conn, old[0]["entity_uid"])
        assert len(by_uid) == 5
        assert {r["host_uid"] for r in by_uid} == {host1, host2}


def test_history_and_changes_keep_f11_tombstones(tmp_path):
    root = _root(tmp_path)
    _emit(root, [_snap(BOT, "first", T1, _bot(BOT)),
                 _tomb(BOT, "missing-completion", T2),
                 _tomb(BOT, "incomplete", T2), _done("incomplete", T2, False),
                 _tomb(BOT, "deleted", T3), _done("deleted", T3)])
    with _conn(root) as conn:
        history = rr.entity_history(conn, BOT)
        assert [r["scan_id"] for r in history] == ["first", "deleted"]
        assert [r["valid_to"] for r in history] == [T3, None]
        assert rr.recent_changes(conn, limit=1)[0]["change"] == "deleted"
    _emit(root, [_snap(BOT, "recreated", "2026-09-01T13:00:00+00:00", _bot(BOT))])
    with _conn(root) as conn:
        assert rr.recent_changes(conn, limit=1)[0]["change"] == "recreated"


@pytest.mark.parametrize("limit", [1, 2, 20, 2**63])
def test_recent_changes_sql_limit_preserves_predecessor(tmp_path, monkeypatch, limit):
    root = _root(tmp_path)
    _emit(root, [_snap(BOT, str(i), ts, _bot(BOT, model=str(i)))
                 for i, ts in enumerate((T1, T2, T3))])
    with _conn(root) as conn:
        counts = _read_counts(monkeypatch)
        rows = rr.recent_changes(conn, limit=limit)
        assert len(rows) == min(limit, 3)
        assert rows[0]["fields"]["model"] == ("1", "2")
        assert rows[0]["change"] == "updated"
        assert counts["rows"] == len(rows)
        # Every output row has a current payload; only the first lacks a prior one.
        assert counts["decodes"] == 2 * len(rows) - int(limit >= 3)


def test_changes_zero_limit_skips_query():
    assert rr.recent_changes(object(), limit=0) == []


@pytest.mark.parametrize("limit", [-1, True, False, 1.5, "1", None])
def test_changes_rejects_invalid_limit(limit):
    with pytest.raises(ValueError, match="nonnegative integer"):
        rr.recent_changes(object(), limit=limit)
