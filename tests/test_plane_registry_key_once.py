"""A registry read computes each snapshot's instant key at most once (#2887).

The registry reads order snapshots by ``claudlobby_instant_key``, a Python
function (#1843). Inlined into each reference, SQLite evaluated it three
times a row. These tests count the calls through a stand-in registered on
the read's own connection (``register_instant_key`` keeps a function that is
already there), so they measure what SQLite runs, whatever the SQL text says.
Each row has its own stamp, so a stamp's count is its row's count.
"""

from collections import Counter
import re
import sqlite3

import pytest

from claudlobby.plane import registry_read as rr
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.queries import (
    REG_CHANGES_SQL, REG_CURRENT_KEYS_SQL, REG_CURRENT_POINT_SQL, REG_CURRENT_SQL, REG_HISTORY_SQL,
)
from claudlobby.plane.time import instant_key, register_instant_key
from tests.test_plane_registry_read import BOT, _bot, _conn, _done, _root, _snap, _tomb

OTHER = "bot:f/gilfoyle"
# Before SQLite 3.35 queries.py leaves out MATERIALIZED, so a read computes
# the key at each reference again: the same rows, at the old cost.
hinted = pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35, 0),
                            reason="SQLite before 3.35.0 has no MATERIALIZED hint")


def _emit(root, rows):
    assert {r.status for r in emit_batch(root, rows)} == {"committed"}


def _registry(tmp_path):
    """Two bots, mixed offsets, a validated tombstone and a recreate, and one
    malformed historical stamp (its key is NULL, so no read may order by it)."""
    root = _root(tmp_path)
    _emit(root, [_snap(BOT, "s0", "2026-09-01T10:00:00Z", _bot(BOT, "m0")),
                 _snap(OTHER, "g0", "2026-09-01T10:15:00Z", _bot(OTHER, "g0")),
                 _snap(BOT, "s1", "2026-09-01T07:30:00-04:00", _bot(BOT, "m1")),
                 _snap(OTHER, "g1", "2026-09-01T11:20:00+01:00", _bot(OTHER, "g1")),
                 _snap(BOT, "s2", "2026-09-01T14:00:00+02:00", _bot(BOT, "m2")),
                 _snap(OTHER, "g2", "2026-09-01T10:25:00Z", _bot(OTHER, "g2"))])
    at = "2026-09-01T12:30:00.000001Z"
    _emit(root, [_tomb(BOT, "del", at), _done("del", at)])
    _emit(root, [_snap(BOT, "s3", "2026-09-01T09:45:00-03:00", _bot(BOT, "m3"))])
    with _conn(root) as conn:
        conn.execute("UPDATE registry_snapshots SET occurred_at = 'not-an-instant'"
                     " WHERE scan_id = 'g2'")
    return root


def _count_keys(conn):
    """Swap the connection's key function for one that records each stamp it
    is asked for; register_instant_key's probe passes NULL, which is no row."""
    calls = []

    def counted(value):
        if value is not None:
            calls.append(value)
        return instant_key(value)

    conn.create_function("claudlobby_instant_key", 1, counted, deterministic=True)
    return calls


def _stamps(conn, where="1", params=()):
    return Counter(row[0] for row in conn.execute(
        "SELECT occurred_at FROM registry_snapshots WHERE " + where, params))


def _assert_at_most_once(calls, scope, read):
    per_row = Counter(calls)
    assert per_row, f"{read}: the counting key function never ran"
    # Counter <= : every stamp asked for is in the read's scope, at most once.
    assert per_row <= scope, (read, dict(per_row))


@hinted
def test_full_reads_compute_each_rows_key_at_most_once(tmp_path):
    root = _registry(tmp_path)
    with _conn(root) as conn:
        every_row = _stamps(conn)
        assert len(every_row) == 8 and set(every_row.values()) == {1}
        host = conn.execute("SELECT DISTINCT host_uid FROM registry_snapshots").fetchone()[0]
        calls = _count_keys(conn)
        for read, run in (("current", lambda: rr.current_entities(conn)),
                          ("changes", lambda: rr.recent_changes(conn, limit=50)),
                          ("keys", lambda: conn.execute(REG_CURRENT_KEYS_SQL, (host,)).fetchall())):
            calls.clear()
            assert run(), read
            _assert_at_most_once(calls, every_row, read)


@hinted
def test_point_and_history_reads_compute_only_their_entitys_keys(tmp_path):
    root = _registry(tmp_path)
    with _conn(root) as conn:
        host, uid = conn.execute("SELECT host_uid, entity_uid FROM registry_snapshots"
                                 " WHERE entity_alias = ? LIMIT 1", (BOT,)).fetchone()
        its_rows = _stamps(conn, "host_uid = ? AND entity_type = 'bot' AND entity_uid = ?",
                           (host, uid))
        assert len(its_rows) == 5
        calls = _count_keys(conn)
        for read, run in (("point", lambda: rr.current_hash(conn, host, "bot", uid)),
                          ("history", lambda: rr.entity_history(conn, BOT))):
            calls.clear()
            assert run(), read
            _assert_at_most_once(calls, its_rows, read)


@pytest.mark.parametrize("sql, params, bound", [
    (REG_CURRENT_POINT_SQL, ("h", "bot", "u"), "host_uid=? AND entity_type=? AND entity_uid=?"),
    (REG_CURRENT_KEYS_SQL, ("h",), "host_uid=?"),
], ids=["point", "keys"])
def test_write_side_current_reads_search_the_partition_index(tmp_path, sql, params, bound):
    # The hash gate runs once per ingested snapshot and the tombstone diff
    # once per scan: each must search idx_registry_partition, never scan.
    root = _root(tmp_path)
    with _conn(root) as conn:
        register_instant_key(conn)
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    search = re.compile(r"SEARCH (TABLE registry_snapshots AS )?rs USING (COVERING )?INDEX"
                        r" idx_registry_partition \(" + re.escape(bound) + r"\)$")
    assert any(search.match(step) for step in plan), plan
    assert not any(re.match(r"SCAN (TABLE )?(registry_snapshots|rs)\b", step) for step in plan), plan


@hinted
def test_each_read_returns_the_same_rows_without_the_materialized_hint(tmp_path):
    # The text an older SQLite gets (queries.py drops the hint there) must be
    # valid SQL and return exactly the rows the hinted text returns.
    root = _registry(tmp_path)
    with _conn(root) as conn:
        register_instant_key(conn)
        host, uid = conn.execute("SELECT host_uid, entity_uid FROM registry_snapshots"
                                 " WHERE entity_alias = ? LIMIT 1", (BOT,)).fetchone()
        for sql, params in ((REG_CURRENT_SQL, ()), (REG_CURRENT_KEYS_SQL, (host,)),
                            (REG_CURRENT_POINT_SQL, (host, "bot", uid)),
                            (REG_HISTORY_SQL, {"ident": BOT}), (REG_CHANGES_SQL, (50,))):
            assert sql.count("effective AS MATERIALIZED (") == 1
            plain = sql.replace("effective AS MATERIALIZED (", "effective AS (")
            rows = conn.execute(sql, params).fetchall()
            assert rows and conn.execute(plain, params).fetchall() == rows
