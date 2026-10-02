"""Quiesced A0 references survive in old session handoffs without changing Plane."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

import pytest

from claudlobby import activation, activation_handoffs
from claudlobby.activation_handoffs import (_BEGIN, _END, _REFRESH_BEGIN, persist_canonical_handoffs,
                                            preflight_canonical_handoffs)
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
REFRESHED = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


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
    helper = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/lib-common.sh"
    result = subprocess.run(["/bin/bash", "-c", '. "$1"; should_resume_session "$2" 86400',
                             "bash", str(helper), str(path)], capture_output=True, text=True,
                            check=False)
    return result.returncode


def test_first_adoption_persists_current_fleet_owned_ids_and_missing_status(tmp_path):
    conn, roster, dirs, expected, worker_handoff = _fixture(tmp_path)
    try:
        assert _resume_gate(worker_handoff) == 1
        result = persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs,
                                             expected_audit=expected, refreshed_at=REFRESHED)
        assert (result["open_tasks"], result["active_assignments"]) == (2, 1)
        refreshed = worker_handoff.read_bytes()
        assert refreshed.startswith(b"---\ncwd: " + str(dirs["data", "worker"]).encode()
                                    + b"\nreferences_refreshed: 2026-10-02T12:00:00Z\n")
        assert b"reference refresh, not a new session capture" in refreshed
        assert b"capture freshness is unverified" in refreshed
        assert b"\n\n" + STALE_HANDOFF + b"\n\n" in refreshed
        # Inverted by #2094: the refresh used to make this 2020 capture resume,
        # so a booting bot read its references. The gate now reads the capture,
        # and the IDs reach a booting bot through the boot brief instead
        # (test_activation_handoffs_boot.py).
        assert _resume_gate(worker_handoff) == 1
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
                                    expected_audit=expected, refreshed_at=REFRESHED)
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


def _tree(root: Path) -> dict:
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file() and path.parent.name == ".claude"}


def test_live_preflight_writes_nothing_and_freezes_no_audit(tmp_path):
    conn, roster, dirs, _expected, worker_handoff = _fixture(tmp_path)
    try:
        before = _tree(tmp_path)
        preflight_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, candidate_bots=set(dirs))
        assert _tree(tmp_path) == before
        assert not (dirs["eng", "manager"] / ".claude").exists()
        # Live writers may add work before quiescence; the quiesced owner
        # rereads its own audit instead of requiring the preflight view.
        _row(conn, "work_items", fleet_uid=ENG, work_item_id="wi_" + "c" * 32,
             title="Arrived before quiescence", created_by_uid=ENG_MANAGER)
        result = persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs,
                                            expected_audit=asdict(audit_tasks(conn)))
        assert result["open_tasks"] == 3
    finally:
        conn.close()


def _malformed(worker_handoff: Path) -> None:
    # A previous activation's section ends the file, where the refresh writes
    # it, but its JSON lost the status: ours and damaged, so it still refuses.
    worker_handoff.write_bytes(STALE_HANDOFF + b"\n\n" + _BEGIN + b"\n```json\n{}\n```\n"
                               + _END + b"\n")


@pytest.mark.parametrize("case, message", [
    ("stopped-worker", "no reviewed old bot actor"),
    ("uninstalled-manager", "old fleet manager has no installed handoff owner"),
    ("retired-worker", "retired bot still owns open work"),
    ("damaged-section", "existing canonical handoff section is unreadable"),
])
def test_live_preflight_refuses_standing_blockers_without_writes(tmp_path, case, message):
    conn, roster, dirs, _expected, worker_handoff = _fixture(tmp_path)
    try:
        candidates = set(dirs)
        if case == "stopped-worker":
            # `bot stop` de-enrolled the worker; its assignment stays current.
            del dirs["data", "worker"]
            roster["data"] = ("manager", ("manager",))
        elif case == "uninstalled-manager":
            del dirs["data", "manager"]
            roster["data"] = ("manager", ("worker",))
        elif case == "retired-worker":
            candidates.discard(("data", "worker"))
        else:
            _malformed(worker_handoff)
        before = _tree(tmp_path)
        with pytest.raises(ActivationError, match=message):
            preflight_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, candidate_bots=candidates)
        assert _tree(tmp_path) == before
        assert not (dirs["eng", "manager"] / ".claude").exists()
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


# #2094: the refresh envelope reused `last_updated:`, the capture's freshness
# signal and the field start-bot's resume gate reads, so the gate measured the
# refresh, never the capture; and the 12-hour rule took the previous refresh
# time from a field any session can edit.
_MARKER_BLOCK = (_REFRESH_BEGIN + b"\n"
                 b"This timestamp marks a canonical task-reference refresh, not a new session capture.\n"
                 b"The original handoff bytes below are historical and their capture freshness is unverified.\n"
                 b"Read the canonical task and assignment IDs in the section at the end of this file.\n"
                 b"<!-- claudlobby first-adoption reference refresh end -->\n\n")


def _legacy_envelope(directory: Path, stamp: str) -> bytes:
    """The envelope as activations wrote it before #2094, carrying last_updated."""
    return f"---\ncwd: {directory}\nlast_updated: {stamp}\nschema_version: 2\n---\n".encode() + _MARKER_BLOCK


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _frozen(monkeypatch, moment: datetime) -> None:
    """Pin the clock the refresh reads."""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)
    monkeypatch.setattr(activation_handoffs, "datetime", Frozen)


def _refresh_stamp(path: Path) -> str:
    """The time the envelope at the top of a handoff carries, whichever field holds it."""
    head = path.read_bytes().split(b"\n---\n", 1)[0]
    return re.search(rb"^(?:references_refreshed|last_updated): (\S+)$", head, re.M).group(1).decode()


def test_a_handoff_written_after_a_refresh_resumes(tmp_path, monkeypatch):
    """A session keeps the envelope and writes its capture under it, a day after
    the refresh: the gate must read the capture, not the refresh."""
    conn, roster, dirs, expected, worker_handoff = _fixture(tmp_path)
    try:
        with monkeypatch.context() as clock:
            _frozen(clock, datetime.now(timezone.utc) - timedelta(hours=25))
            persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, expected_audit=expected)
        captured = _stamp(datetime.now(timezone.utc)).encode()
        worker_handoff.write_bytes(worker_handoff.read_bytes().replace(
            b"last_updated: 2020-01-01T00:00:00Z", b"last_updated: " + captured))
        assert _resume_gate(worker_handoff) == 0
    finally:
        conn.close()


def test_a_handoff_written_a_day_before_a_refresh_does_not_resume(tmp_path):
    conn, roster, dirs, expected, worker_handoff = _fixture(tmp_path)
    try:
        day_old = _stamp(datetime.now(timezone.utc) - timedelta(hours=25)).encode()
        worker_handoff.write_bytes(STALE_HANDOFF.replace(b"2020-01-01T00:00:00Z", day_old))
        assert _resume_gate(worker_handoff) == 1
        persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, expected_audit=expected)
        assert _resume_gate(worker_handoff) == 1  # a refresh is not a capture
    finally:
        conn.close()


def test_a_refresh_keeps_the_mtime_a_capture_without_a_timestamp_is_judged_by(tmp_path):
    """The gate falls back to the file's mtime for a handoff with no last_updated
    of its own, so a refresh that rewrites the file must not make it new."""
    conn, roster, dirs, expected, worker_handoff = _fixture(tmp_path)
    try:
        worker_handoff.write_bytes(b"## Next Steps\n- A capture with no timestamp of its own\n")
        day_old = time.time() - 25 * 3600
        os.utime(worker_handoff, (day_old, day_old))
        assert _resume_gate(worker_handoff) == 1
        persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, expected_audit=expected)
        assert _resume_gate(worker_handoff) == 1
    finally:
        conn.close()


QUOTING_HANDOFF = (b"---\nlast_updated: 2026-10-01T00:00:00Z\nschema_version: 2\n---\n\n## Notes\n"
                   b"- The activation adds a header that opens with " + _REFRESH_BEGIN + b".\n"
                   b"- Its IDs sit between " + _BEGIN + b" and " + _END + b".\n"
                   b"\n```\n" + _REFRESH_BEGIN + b"\n" + _BEGIN + b"\n" + _END + b"\n```\n")


def test_a_note_quoting_a_marker_line_does_not_refuse(tmp_path):
    """The markers are ours only where the refresh writes them: at the top and at
    the end. Quoted in a note, inline or on a line of their own, they are text,
    and one bot's quote must not refuse the whole host's activation."""
    conn, roster, dirs, expected, worker_handoff = _fixture(tmp_path)
    try:
        worker_handoff.write_bytes(QUOTING_HANDOFF)
        preflight_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, candidate_bots=set(dirs))
        persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, expected_audit=expected)
        assert b"\n\n" + QUOTING_HANDOFF + b"\n\n" in worker_handoff.read_bytes()
        assert _section(worker_handoff)["assigned_to_this_bot"][0]["assignment_id"] == ASSIGNMENT
        persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, expected_audit=expected)
        assert worker_handoff.read_bytes().count(QUOTING_HANDOFF) == 1
    finally:
        conn.close()


def test_an_edited_envelope_timestamp_does_not_become_the_refresh_time(tmp_path, monkeypatch):
    """A session rewrote the envelope's timestamp to its capture time, so the gate
    would read the capture. The next refresh must not take that edit as its own
    time: a time read from the file is one any session can change."""
    conn, roster, dirs, expected, worker_handoff = _fixture(tmp_path)
    try:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        edited = _stamp(now - timedelta(hours=1))
        worker_handoff.write_bytes(_legacy_envelope(dirs["data", "worker"], edited) + STALE_HANDOFF)
        with monkeypatch.context() as clock:
            _frozen(clock, now)
            persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, expected_audit=expected)
        assert _refresh_stamp(worker_handoff) == _stamp(now)
    finally:
        conn.close()


def test_a_section_with_later_notes_after_it_is_the_sessions_text(tmp_path):
    """A section is ours only at the end. One a session wrote more notes after is
    the session's text: the refresh keeps it and appends a fresh section, and
    no single bot's notes refuse the host's activation."""
    conn, roster, dirs, expected, worker_handoff = _fixture(tmp_path)
    try:
        noted = (STALE_HANDOFF + b"\n\n" + _BEGIN + b"\n```json\n{}\n```\n" + _END
                 + b"\n\n## Later notes\n")
        worker_handoff.write_bytes(noted)
        preflight_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, candidate_bots=set(dirs))
        persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, expected_audit=expected)
        content = worker_handoff.read_bytes()
        assert b"\n\n" + noted + b"\n\n" in content and content.rstrip().endswith(_END)
    finally:
        conn.close()


def test_both_transitional_envelopes_are_read_and_rewritten(tmp_path):
    """The envelope written before #2094 (with last_updated) and none at all are
    both read; the next write carries references_refreshed only, so the first
    last_updated in the file is the capture's."""
    conn, roster, dirs, expected, worker_handoff = _fixture(tmp_path)
    try:
        worker_handoff.write_bytes(_legacy_envelope(dirs["data", "worker"], "2026-10-01T08:00:00Z")
                                   + STALE_HANDOFF)
        preflight_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, candidate_bots=set(dirs))
        persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, expected_audit=expected,
                                   refreshed_at=REFRESHED)
        content = worker_handoff.read_bytes()
        assert content.count(_REFRESH_BEGIN) == 1
        assert re.search(rb"^last_updated: (\S+)$", content, re.M).group(1) == b"2020-01-01T00:00:00Z"
        assert _refresh_stamp(worker_handoff) == "2026-10-02T12:00:00Z"
        assert b"\n\n" + STALE_HANDOFF + b"\n\n" in content
    finally:
        conn.close()


def test_a_damaged_envelope_at_the_top_still_refuses(tmp_path):
    """At the top, where the refresh writes it, an envelope whose prose changed is
    ours and damaged: refused, as before."""
    conn, roster, dirs, expected, worker_handoff = _fixture(tmp_path)
    try:
        damaged = _legacy_envelope(dirs["data", "worker"], "2026-10-01T08:00:00Z").replace(
            b"historical", b"old")
        worker_handoff.write_bytes(damaged + STALE_HANDOFF)
        with pytest.raises(ActivationError, match="reference refresh is malformed"):
            preflight_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, candidate_bots=set(dirs))
        assert worker_handoff.read_bytes() == damaged + STALE_HANDOFF
    finally:
        conn.close()


def _records(monkeypatch, **bodies):
    """Activation records by id, as read_activation would return them."""
    def read(_root, activation_id):
        if activation_id not in bodies:
            raise ActivationError(f"cannot read activation {activation_id}")
        return activation.ActivationRecord(activation_id, Path("/"), bodies[activation_id])
    monkeypatch.setattr(activation, "read_activation", read)


@pytest.mark.parametrize("own, previous, expected", [
    ("2026-10-02T09:00:00Z", None, "2026-10-02T09:00:00Z"),  # a retried step keeps its own
    (None, "2026-10-02T03:00:00Z", "2026-10-02T03:00:00Z"),  # under 12 h: the previous one's
    (None, "2026-10-01T23:00:00Z", "2026-10-02T12:00:00Z"),  # 13 h old: now
    (None, "missing", "2026-10-02T12:00:00Z"),  # recorded before #2094: none to keep
    (None, "unreadable", "2026-10-02T12:00:00Z"),
    (None, "first", "2026-10-02T12:00:00Z"),  # a first adoption has no previous selection
])
def test_the_refresh_time_comes_from_the_activation_records(monkeypatch, own, previous, expected):
    body = {"previous_selection": None if previous == "first" else {"activation_id": "earlier"}}
    if own:
        body["handoff_refreshed"] = own
    bodies = {"current": body}
    if previous not in ("first", "unreadable"):
        bodies["earlier"] = {} if previous == "missing" else {"handoff_refreshed": previous}
    _records(monkeypatch, **bodies)
    now = REFRESHED.replace(microsecond=123456)
    assert _stamp(activation._handoff_refresh_time(Path("/"), "current", now)) == expected
