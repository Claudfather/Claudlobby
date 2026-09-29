"""Quiesced A0 references survive in old session handoffs without changing Plane."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from claudlobby.activation_handoffs import persist_canonical_handoffs
from claudlobby.activation_state import ActivationError
from claudlobby.plane.db import db_file
from claudlobby.plane.migrations import migrate
from claudlobby.task_audit import audit_tasks


HOST = "host_" + "1" * 32
ENG = "fleet_" + "2" * 32
DATA = "fleet_" + "3" * 32
ENG_MANAGER = "actor_" + "4" * 32
DATA_MANAGER = "actor_" + "5" * 32
DATA_WORKER = "actor_" + "6" * 32
TASK = "wi_" + "7" * 32
QUEUED = "wi_" + "8" * 32
ASSIGNMENT = "asg_" + "9" * 32
STALE_HANDOFF = (b"---\nlast_updated: 2020-01-01T00:00:00Z\nschema_version: 2\n---\n"
                 b"\n## Next Steps\n- Historical session note\n")


def _row(conn: sqlite3.Connection, table: str, **fields) -> None:
    event_id = f"ev_{conn.execute('SELECT COUNT(*) FROM ingest_ledger').fetchone()[0]:032x}"
    seq = conn.execute("INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                       " VALUES (?, ?, 't')", (event_id, table)).lastrowid
    row = dict(ingest_seq=seq, event_id=event_id, schema_version="1.0.0",
               occurred_at="2026-09-01T00:00:00Z", ingested_at="2026-09-01T00:00:00Z",
               host_uid=HOST, emitter="dispatch-task", origin="legacy", **fields)
    conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                 tuple(row.values()))


def _fixture(root: Path):
    (root / "state/plane").mkdir(parents=True)
    host = root / "state/host-uid"
    host.write_text(HOST + "\n")
    host.chmod(0o600)
    conn = sqlite3.connect(db_file(root), isolation_level=None)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    for uid, kind, alias, parent in (
        (HOST, "host", "host", None), (ENG, "fleet", "eng", HOST),
        (DATA, "fleet", "data", HOST),
        (ENG_MANAGER, "actor", "bot:eng/manager", ENG),
        (DATA_MANAGER, "actor", "bot:data/manager", DATA),
        (DATA_WORKER, "actor", "bot:data/worker", DATA),
    ):
        conn.execute("INSERT INTO identity_registry"
                     " (uid, kind, alias, parent_uid, provisional, first_seen, last_seen)"
                     " VALUES (?, ?, ?, ?, 0, 't', 't')", (uid, kind, alias, parent))
    _row(conn, "work_items", fleet_uid=DATA, work_item_id=TASK,
         title="Fleet-owned historical assignment", created_by_uid=DATA_MANAGER)
    _row(conn, "assignments", fleet_uid=DATA, assignment_id=ASSIGNMENT,
         work_item_id=TASK, assignee_uid=DATA_WORKER, assigned_by_uid=DATA_MANAGER)
    _row(conn, "work_items", fleet_uid=ENG, work_item_id=QUEUED,
         title="Queued intake", created_by_uid=ENG_MANAGER)
    expected = asdict(audit_tasks(conn))
    bot_dirs = {}
    for fleet, bot in (("eng", "manager"), ("data", "manager"), ("data", "worker")):
        directory = root / "local" / fleet / "runtime" / "bots" / bot
        directory.mkdir(parents=True)
        bot_dirs[fleet, bot] = directory
    worker_handoff = bot_dirs["data", "worker"] / ".claude/session.md"
    worker_handoff.parent.mkdir()
    worker_handoff.write_bytes(STALE_HANDOFF)
    roster = {"eng": ("manager", ("manager",)),
              "data": ("manager", ("manager", "worker"))}
    return conn, roster, bot_dirs, expected, worker_handoff


def _section(path: Path) -> dict:
    content = path.read_text()
    return json.loads(content.split("```json\n", 1)[1].split("\n```", 1)[0])


def _resume_gate(path: Path) -> int:
    helper = Path(__file__).resolve().parents[1] / "lib/lib-common.sh"
    result = subprocess.run(["/bin/bash", "-c", '. "$1"; should_resume_session "$2" 86400',
                             "bash", str(helper), str(path)], capture_output=True, text=True,
                            check=False)
    return result.returncode


def test_first_adoption_persists_current_fleet_owned_ids_and_missing_status(tmp_path):
    conn, roster, dirs, expected, worker_handoff = _fixture(tmp_path)
    try:
        assert _resume_gate(worker_handoff) == 1
        result = persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs,
                                             expected_audit=expected)
        assert (result["open_tasks"], result["active_assignments"]) == (2, 1)
        refreshed = worker_handoff.read_bytes()
        assert refreshed.startswith(b"---\ncwd: " + str(dirs["data", "worker"]).encode())
        assert b"reference refresh, not a new session capture" in refreshed
        assert b"capture freshness is unverified" in refreshed
        assert b"\n\n" + STALE_HANDOFF + b"\n\n" in refreshed
        assert _resume_gate(worker_handoff) == 0
        worker = _section(worker_handoff)
        assert worker["previous_handoff"] == "existing file present (fresh capture unverified)"
        assert worker["assigned_to_this_bot"] == [{
            "task_id": TASK, "assignment_id": ASSIGNMENT, "owning_fleet": "data",
            "state": "assigned", "current_assignee": "bot:data/worker"}]
        manager_path = dirs["eng", "manager"] / ".claude/session.md"
        manager = _section(manager_path)
        assert manager["previous_handoff"].startswith("unavailable")
        assert {row["task_id"] for row in manager["manager_owned_work"]} == {QUEUED}
        data_manager = _section(dirs["data", "manager"] / ".claude/session.md")
        assert {row["task_id"] for row in data_manager["manager_owned_work"]} == {TASK}
        first = worker_handoff.read_bytes()
        manager_first = manager_path.read_bytes()
        persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs,
                                    expected_audit=expected)
        assert worker_handoff.read_bytes() == first
        assert manager_path.read_bytes() == manager_first
        # A new queued task changes the A0 audit; a changed assignee alone does not.
        _row(conn, "work_items", fleet_uid=ENG, work_item_id="wi_" + "b" * 32,
             title="Arrived after quiescence", created_by_uid=ENG_MANAGER)
        with pytest.raises(ActivationError, match="audit changed"):
            persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs,
                                        expected_audit=expected)
        assert worker_handoff.read_bytes() == first
        # Even a fresh audit cannot make an unknown current assignee a bot.
        conn.execute("UPDATE assignments SET assignee_uid=? WHERE assignment_id=?",
                     ("actor_" + "a" * 32, ASSIGNMENT))
        fresh = asdict(audit_tasks(conn))
        with pytest.raises(ActivationError, match="no reviewed old bot actor"):
            persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs,
                                        expected_audit=fresh)
        assert worker_handoff.read_bytes() == first
    finally:
        conn.close()


def test_retiring_bot_with_current_assignment_refuses_before_handoff_write(tmp_path):
    conn, roster, dirs, expected, worker_handoff = _fixture(tmp_path)
    try:
        retained = set(dirs) - {("data", "worker")}
        with pytest.raises(ActivationError, match="retired bot still owns open work"):
            persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs,
                                       expected_audit=expected, candidate_bots=retained)
        assert worker_handoff.read_bytes() == STALE_HANDOFF
        assert not (dirs["eng", "manager"] / ".claude/session.md").exists()
    finally:
        conn.close()


def test_cross_fleet_legacy_assignment_refuses_before_handoff_write(tmp_path):
    conn, roster, dirs, _expected, worker_handoff = _fixture(tmp_path)
    try:
        conn.execute("UPDATE work_items SET fleet_uid=? WHERE work_item_id=?", (ENG, TASK))
        conn.execute("UPDATE assignments SET fleet_uid=? WHERE assignment_id=?", (ENG, ASSIGNMENT))
        audit = audit_tasks(conn)
        assert audit.blockers
        with pytest.raises(ActivationError, match="unresolved active links"):
            persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs,
                                       expected_audit=asdict(audit))
        assert worker_handoff.read_bytes() == STALE_HANDOFF
        assert not (dirs["eng", "manager"] / ".claude/session.md").exists()
    finally:
        conn.close()
