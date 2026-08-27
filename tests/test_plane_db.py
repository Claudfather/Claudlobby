from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.migrations import SCHEMA_USER_VERSION, migrate


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(db_path(tmp_path))
    migrate(c)
    yield c
    c.close()


def test_db_path_shape(tmp_path: Path):
    p = db_path(tmp_path)
    assert p == tmp_path / "state" / "plane" / "plane.db"
    assert p.parent.is_dir()


def test_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_file_modes_hardened(tmp_path):
    import os as _os, stat as _stat
    p = db_path(tmp_path)
    connect(p).close()
    assert _stat.S_IMODE(_os.stat(p).st_mode) == 0o600
    assert _stat.S_IMODE(_os.stat(p.parent).st_mode) == 0o700


def test_migration_failure_is_atomic(tmp_path, monkeypatch):
    """Round-2 F2: a failing script must leave NO tables and version 0 —
    the script owns BEGIN IMMEDIATE...COMMIT, so partial DDL cannot commit."""
    import claudlobby.plane.migrations as mig
    monkeypatch.setattr(mig, "_migration_files",
        lambda: [(1, "BEGIN IMMEDIATE; CREATE TABLE t1 (x); THIS IS NOT SQL; COMMIT;")])
    c = connect(db_path(tmp_path))
    import pytest as _pytest, sqlite3 as _sq
    with _pytest.raises(_sq.OperationalError):
        mig.migrate(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0
    names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert names == []
    c.close()


def test_migrate_sets_user_version_and_is_idempotent(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_USER_VERSION
    assert migrate(conn) == SCHEMA_USER_VERSION  # second run: no-op


def test_expected_tables(conn):
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "ingest_ledger", "identity_registry", "communications",
        "work_items", "assignments", "workstreams", "events",
    } <= names


def test_downgrade_refused(tmp_path: Path):
    c = connect(db_path(tmp_path))
    c.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION + 100}")
    with pytest.raises(RuntimeError, match="newer than this code"):
        migrate(c)


def test_ingest_ledger_seq_monotonic(conn):
    for i in range(3):
        conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES (?, 'task_event', '2026-01-01T00:00:00+00:00')",
            (f"ev_{i:032x}",),
        )
    seqs = [r[0] for r in conn.execute("SELECT ingest_seq FROM ingest_ledger ORDER BY ingest_seq")]
    assert seqs == sorted(seqs) and len(seqs) == 3


def test_kind_matrix_executed_against_installed_schema():
    """Round-3 F3: EXHAUSTIVE — every vocabulary member accepted; every
    required-field omission rejected; every DERIVED-forbidden column rejected.
    Round 2's hand-listed probes missed subject_kind NULL on declaration and
    off-kind actor/session — derivation closes the class."""
    import sqlite3 as sq

    import pytest as _pytest

    from claudlobby.plane import contracts as c
    from claudlobby.plane.db import connect
    from claudlobby.plane.migrations import migrate

    conn = connect(":memory:")
    migrate(conn)

    VALID = {
        "transmission": {"msg_id": "msg_" + "0" * 32, "carrier": "tmux",
                          "attempt_no": 1},
        "task": {"work_item_id": "wi_" + "0" * 32},
        "workstream": {"workstream_id": "ws-x"},
        "system": {},
        "declaration": {"subject_kind": "vault",
                         "subject_uid": "vault_" + "0" * 32},
    }
    FIRST_TOKEN = {"transmission": "send_attempted", "task": "progress",
                   "workstream": "progressed", "system": "restart",
                   "declaration": "revision_seen"}
    FVALS = {"event": "progress", "carrier": "tmux", "attempt_no": 2,
             "carrier_ref": "x", "msg_id": "msg_" + "1" * 32,
             "work_item_id": "wi_" + "1" * 32,
             "assignment_id": "asg_" + "1" * 32, "workstream_id": "ws-y",
             "subject_kind": "actor", "subject_uid": "actor_" + "1" * 32,
             "subject_alias": "bot:f/x", "actor_uid": "actor_" + "2" * 32,
             "session_uid": "sess_" + "1" * 32, "severity": "notice",
             "deadline": "t", "successor_id": "x", "renewed_until": "t"}
    eid = [100]

    def attempt(row: dict):
        # Round-4 F3: the ledger's AUTOINCREMENT assigns ingest_seq — use the
        # cursor's lastrowid (a synthetic counter breaks the FK, which the
        # real connection enforces: foreign_keys=ON). Savepoint per attempt so
        # expected failures leave no ledger residue.
        eid[0] += 1
        conn.execute("SAVEPOINT probe")
        try:
            cur = conn.execute(
                "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                " VALUES (?, 'probe', 't')", (f"ev_{eid[0]:032x}",))
            seq = cur.lastrowid
            cols = {"ingest_seq": seq, "event_id": f"ev_{eid[0]:032x}",
                    "schema_version": "1", "occurred_at": "t",
                    "ingested_at": "t", "host_uid": "h", "emitter": "e", **row}
            names = tuple(cols)
            conn.execute(
                f"INSERT INTO events ({','.join(names)})"
                f" VALUES ({','.join('?' * len(names))})",
                tuple(cols.values()))
        except BaseException:
            conn.execute("ROLLBACK TO probe")
            conn.execute("RELEASE probe")
            raise
        conn.execute("RELEASE probe")

    for kind, manifest in c.KIND_MANIFEST.items():
        base = {"kind": kind, "event": FIRST_TOKEN[kind], **VALID[kind]}
        # 1) EVERY vocabulary member is accepted — under a carrier the
        #    carrier/state matrix permits (#1372 F12: pane facts are tmux-only,
        #    carrier_accepted telegram-only; derived from the contracts SSOT,
        #    never a hand list). Each carrier-restricted token is ALSO probed
        #    with a wrong carrier and must be refused.
        for token in (manifest["vocab"] or (FIRST_TOKEN[kind],
                                            "brand-new-machinery-type")):
            row = {**base, "event": token}
            allowed = c._CARRIER_ONLY_STATES.get(token) if kind == "transmission" else None
            if allowed is not None:
                # EVERY allowed carrier accepted, EVERY disallowed rejected —
                # allowed[0]-only left a can't-drift claim false (#1372
                # re-verify: dropping telegram-bridge from carrier_accepted
                # stayed green under the old single-carrier probe).
                for ok_carrier in allowed:
                    attempt({**row, "carrier": ok_carrier})
                for bad_carrier in c.CARRIERS:
                    if bad_carrier not in allowed:
                        with _pytest.raises(sq.IntegrityError):
                            attempt({**row, "carrier": bad_carrier})
            else:
                attempt(row)
        if manifest["vocab"] is not None:
            with _pytest.raises(sq.IntegrityError):
                attempt({**base, "event": "no-such-token"})
        # 2) EVERY required-field omission is rejected (incl. event=None):
        for req in manifest["require"]:
            with _pytest.raises(sq.IntegrityError):
                attempt({**base, req: None})
        # 3) EVERY derived-forbidden column is rejected:
        for col in c.kind_forbidden(kind):
            with _pytest.raises(sq.IntegrityError):
                attempt({**base, col: FVALS[col]})
        # 4) EVERY allowed (optional) column is individually ACCEPTED
        #    (round-4 note: parity claimed exhaustive without proving this half):
        for col in manifest["allowed"]:
            attempt({**base, col: FVALS[col]})
        # 5) allowed GROUPS — EVERY nonempty subset enumerated (round-6):
        #    valid iff the subset contains the full anchor; dependents are
        #    legal only alongside it. For system's 2-anchor+1-dependent
        #    group: 7 subsets → 2 accepted ({kind,uid}, {kind,uid,alias}),
        #    5 rejected. Totals (PR-B recount): 52 accepted / 85 rejected / 0 —
        #    carrier_queued + supplied_id_not_open joined the vocabularies and
        #    the three carrier-restricted tokens each gained a wrong-carrier
        #    rejection probe (#1372 F12).
        from itertools import chain, combinations

        GROUP_VALS = {"subject_kind": "actor",
                      "subject_uid": "actor_" + "3" * 32,
                      "subject_alias": "bot:f/g"}
        for group in manifest.get("allowed_groups", ()):
            members = tuple(group["anchor"]) + tuple(group["dependent"])
            anchor = set(group["anchor"])
            for subset in chain.from_iterable(
                combinations(members, n) for n in range(1, len(members) + 1)
            ):
                row = {**base, **{g: GROUP_VALS[g] for g in subset}}
                if anchor <= set(subset):
                    attempt(row)
                else:
                    with _pytest.raises(sq.IntegrityError):
                        attempt(row)

    with _pytest.raises(sq.IntegrityError):     # dead vocabulary stays dead
        attempt({"kind": "task", "work_item_id": "wi_" + "2" * 32,
                 "event": "receiver_acknowledged"})
    conn.close()


def test_envelope_is_17_columns_on_every_lane_b_table():
    from claudlobby.plane.db import connect
    from claudlobby.plane.migrations import migrate

    conn = connect(":memory:")
    migrate(conn)
    ENVELOPE = ["ingest_seq", "event_id", "schema_version", "occurred_at",
                "observed_at", "ingested_at", "host_uid", "fleet_uid",
                "emitter", "source_ref", "correlation_id", "causation_id",
                "trace_id", "span_id", "origin", "import_batch", "confidence"]
    for table in ("communications", "work_items", "assignments",
                  "workstreams", "events"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert cols[:17] == ENVELOPE, f"{table} envelope drift: {cols[:17]}"
    conn.close()


def test_duplicate_event_id_rejected_by_ledger(conn):
    conn.execute(
        "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
        " VALUES ('ev_' || printf('%032x', 7), 'task_event', 't')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES ('ev_' || printf('%032x', 7), 'task_event', 't')"
        )
