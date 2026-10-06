"""Remaining task-loop reader, deadline and migration pins over real Plane storage."""

from __future__ import annotations


import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import load_lib_module

F = "e2e-fleet"


def _matcher(root, libdir, env, *args):
    return subprocess.run([sys.executable, str(libdir / "dispatch-overdue.py"), *args,
                           "--fleet", F, "--root", str(root)],
                          capture_output=True, text=True, env=env, timeout=120)


def _lookup(root, libdir, env, *args):
    return subprocess.run([sys.executable, str(libdir / "plane-lookup.py"),
                           "--root", str(root), *args],
                          capture_output=True, text=True, env=env, timeout=120)


def test_the_escalated_read_refuses_an_unreachable_plane(tmp_path, *, scratch_plane_env):
    """source_state's rule: unreachable is not empty. A watchdog that read
    'nothing escalated' off a plane it could not open would go dark in
    silence, which is the exact class #1014 named."""
    libdir, env = Path(__file__).resolve().parent.parent / "claudlobby/_runtime_scripts", scratch_plane_env(tmp_path)
    out = _lookup(tmp_path / "nowhere", libdir, env, "--escalated", "--fleet", F)
    assert out.returncode == 3 and out.stdout == ""
    assert "unreachable" in out.stderr


# --- the terminal vocabulary retained by the matcher -------------------------

def test_cancelled_is_terminal_in_every_matcher_vocabulary():
    """`cancelled` has been terminal for the plane's OPEN set since Phase 1,
    but the two LEGACY-status sets the matcher and brief read did not carry
    it — so a withdrawn row left `--open` while every status render called it
    live. One vocabulary, or the doors disagree about the same fact."""
    import importlib.util

    lib = Path(__file__).resolve().parent.parent / "claudlobby/_runtime_scripts"
    spec = importlib.util.spec_from_file_location("dov", lib / "dispatch-overdue.py")
    dov = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dov)
    spec = importlib.util.spec_from_file_location("pr", lib / "plane-readers.py")
    pr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr)

    assert "cancelled" in dov._TERMINAL
    assert "cancelled" in pr.TERMINAL_STATUSES
    assert dov._TERMINAL == set(pr.TERMINAL_STATUSES)
    assert pr.LEGACY_STATUS["cancelled"] == "cancelled"
    # ...and the legacy names still map to what they always did
    assert pr.LEGACY_STATUS["returned_blocked"] == "blocked"


# --- M1: a deadline by default ----------------------------------------------

# --- migration 0010: the widened CHECK ---------------------------------------

def test_migration_0010_widens_the_task_check_without_losing_a_row(tmp_path):
    """SQLite cannot ALTER a CHECK, so widening the task vocabulary is a table
    REBUILD. What that has to preserve is everything: the rows, the schema's
    own refusals, and every index (a silently-dropped partial index turns the
    per-assignment seek the §14 read gate depends on into a table scan)."""
    import sqlite3

    from claudlobby.plane.db import connect
    from claudlobby.plane.migrations import (
        SCHEMA_USER_VERSION, _migration_files, migrate)

    conn = connect(tmp_path / "old.db")
    conn.isolation_level = None
    for number, sql in _migration_files():          # stop one short of 0010
        if number >= 10:
            break
        conn.executescript(sql)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    before_idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'")}

    conn.execute("INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                 " VALUES ('ev_' || ?, 'task', '2026-01-01T00:00:00+00:00')", ("a" * 32,))
    seq = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO events (ingest_seq, event_id, schema_version, occurred_at,"
        " ingested_at, host_uid, emitter, kind, event, work_item_id)"
        " VALUES (?, 'ev_' || ?, '2', '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00', 'host_' || ?, 't', 'task', 'progress', 'wi_' || ?)",
        (seq, "a" * 32, "0" * 32, "b" * 32))
    # the pre-0010 schema REFUSES the new tokens — the defect the migration fixes
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (ingest_seq, event_id, schema_version, occurred_at,"
            " ingested_at, host_uid, emitter, kind, event, work_item_id)"
            " VALUES (?, 'ev_' || ?, '2', 'x', 'x', 'h', 't', 'task', 'escalated', 'wi')",
            (seq + 1, "c" * 32))

    # migrate() applies the rest of the ladder — 0010's widen AND every later
    # rebuild (0011 added `received`, chunk P) — reaching SCHEMA_USER_VERSION.
    # The row/index preservation asserted below therefore holds ACROSS all of
    # them: 0011 is the same 12-step rebuild and preserves both, so this pin
    # only had to learn a later migration exists, not weaken what it checks.
    assert migrate(conn) == SCHEMA_USER_VERSION
    assert [r[0] for r in conn.execute("SELECT event FROM events")] == ["progress"]
    after_idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'")}
    assert after_idx == before_idx

    # ...and the LIVE migrated schema accepts EVERY token the contract names
    # (fold F13): the two new ones are the point, but a vocabulary pinned only
    # at its newest members is a gate that stops watching the rest.
    from claudlobby.plane.contracts import TASK_EVENTS

    assert {"escalated", "nudged"} <= set(TASK_EVENTS)
    for i, token in enumerate(TASK_EVENTS):
        conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES ('ev_' || ?, 'task', 'x')", (f"{i:032x}",))
        s = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO events (ingest_seq, event_id, schema_version, occurred_at,"
            " ingested_at, host_uid, emitter, kind, event, work_item_id)"
            " VALUES (?, 'ev_' || ?, '2', 'x', 'x', 'h', 't', 'task', ?, 'wi')",
            (s, f"{i:032x}", token))
    # ...and a dead token stays dead: the rebuild widened the list, not the gate
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (ingest_seq, event_id, schema_version, occurred_at,"
            " ingested_at, host_uid, emitter, kind, event, work_item_id)"
            " VALUES (99, 'ev_' || ?, '2', 'x', 'x', 'h', 't', 'task',"
            " 'receiver_acknowledged', 'wi')", ("d" * 32,))
    conn.close()


def test_the_stdlib_open_by_task_sql_is_byte_identical_to_the_package():
    """FOLD F6: "the open assignments carrying this task id" was written three
    times. One definition, one stdlib twin, pinned like `OPEN_SQL`."""
    from claudlobby.plane.queries import OPEN_BY_TASK_REF_SQL

    assert load_lib_module("plane-readers").TASK_OPEN_SQL == OPEN_BY_TASK_REF_SQL


def test_the_escalation_window_is_the_same_tuple_on_both_sides():
    """The remaining legacy menu and canonical query must both ignore nudges."""
    from claudlobby.plane.queries import ESCALATION_IGNORED

    assert load_lib_module("plane-readers").ESCALATION_IGNORED == ESCALATION_IGNORED
    assert "nudged" in ESCALATION_IGNORED


def test_a_migrator_that_lost_the_race_waits_instead_of_raising(tmp_path):
    """FOLD F13. The raced-migrator branch reads `user_version` the instant
    the script failed — right when the winner has COMMITTED, wrong when it is
    still running. Every earlier migration is milliseconds, so the loser's
    `BEGIN IMMEDIATE` always found the lock free or waited out a blink; 0010
    rebuilds `events` and holds it for SECONDS on a real plane, long enough to
    exceed `busy_timeout` and raise on a benign race. Taking the lock ourselves
    blocks until the winner commits, which is the proof the re-read needs."""
    import sqlite3

    from claudlobby.plane import migrations as m
    from claudlobby.plane.db import connect

    # the wait itself, against a real idle db: it answers the live version and
    # leaves NO transaction open (a held write lock here would wedge the door)
    conn = connect(tmp_path / "p.db")
    conn.isolation_level = None
    assert m.migrate(conn) == m.SCHEMA_USER_VERSION
    assert m._version_after_writer(conn) == m.SCHEMA_USER_VERSION
    assert not conn.in_transaction
    conn.close()

    # ...and the branch that uses it, driven deterministically: a script that
    # fails locked while `user_version` still reads BEHIND, and a winner that
    # commits while we wait for the lock.
    class _Losing:
        """The narrow slice of a connection `migrate` touches."""

        isolation_level = None
        in_transaction = False

        def __init__(self):
            self.versions = [0]          # nothing applied yet

        def execute(self, sql, *a):
            if "user_version" in sql:
                cur = sqlite3.connect(":memory:").execute(
                    "SELECT ?", (self.versions[-1],))
                return cur
            return None

        def executescript(self, sql):
            # the winner is mid-rebuild and still holds the write lock
            raise sqlite3.OperationalError("database is locked")

    losing = _Losing()
    waited = []

    def _winner_committed(conn):
        waited.append(True)
        return m.SCHEMA_USER_VERSION      # it finished while we blocked

    original = m._version_after_writer
    m._version_after_writer = _winner_committed
    try:
        assert m.migrate(losing) == m.SCHEMA_USER_VERSION
    finally:
        m._version_after_writer = original
    assert waited, "the loser never waited for the winner — it raised instead"


# --- #1747 Phase E: source adoption boundary ---------------------------------

_RETIRED_TASK_DOOR = re.compile(
    r"\b(?:dispatch-task|report-back|task-act|checkin-record)\.sh\b")
_TASK_FAMILY_LITERAL = re.compile(
    r"(?:\\?[\"'])event_type(?:\\?[\"'])\s*:\s*(?:\\?[\"'])"
    r"(?:work_item|assignment|task)(?:\\?[\"'])"
    r"|(?:\\?[\"'])kind(?:\\?[\"'])\s*:\s*(?:\\?[\"'])task(?:\\?[\"'])"
    r"|\b_raw\s*\([^\n]*?,[^\n]*?,\s*[\"'](?:work_item|assignment|task)[\"']"
    r"|\bplane\s+emit\s+(?:work_item|assignment|task)\b")
_TASK_FAMILY_OWNERS = {
    "claudlobby/task_operations.py",
    "claudlobby/report_payload.py",
    "claudlobby/plane/expiry.py",
    # Ingest maps accepted wire facts to stored rows; it does not originate a batch.
    "claudlobby/plane/ingest.py",
}


def _is_code_comment(text: str, offset: int, path: Path) -> bool:
    if path.suffix not in {".py", ".sh"}:
        return False
    line_start = text.rfind("\n", 0, offset) + 1
    return text[line_start:offset].lstrip().startswith("#")


def _task_source_violations(root: Path) -> list[str]:
    """Catch obvious authored task writes and retired recipes, not dynamic construction."""
    violations = []
    for directory in ("library", "templates"):
        for path in sorted((root / directory).rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for match in _RETIRED_TASK_DOOR.finditer(text):
                if _is_code_comment(text, match.start(), path):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{path.relative_to(root)}:{line}: retired task door")
            for match in _TASK_FAMILY_LITERAL.finditer(text):
                if _is_code_comment(text, match.start(), path):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{path.relative_to(root)}:{line}: direct task-family fact")
    for path in sorted((root / "claudlobby").rglob("*")):
        if path.suffix not in {".py", ".sh"} or "_resources" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        if relative in _TASK_FAMILY_OWNERS:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _TASK_FAMILY_LITERAL.finditer(text):
            if _is_code_comment(text, match.start(), path):
                continue
            line = text.count("\n", 0, match.start()) + 1
            violations.append(f"{relative}:{line}: direct task-family fact")
    return sorted(violations)


def test_composable_sources_use_the_owned_task_doors():
    root = Path(__file__).resolve().parent.parent
    assert _task_source_violations(root) == []


def test_task_source_guard_catches_competing_writer_and_retired_recipe(tmp_path):
    recipe = tmp_path / "library" / "skills" / "dispatch" / "SKILL.md"
    recipe.parent.mkdir(parents=True)
    recipe.write_text(
        "Run dispatch-task.sh --worker example\n"
        "Run claudlobby plane emit task here.\n", encoding="utf-8")
    tool = tmp_path / "library" / "tools" / "shortcut" / "tool.sh"
    tool.parent.mkdir(parents=True)
    tool.write_text(
        r'''printf "{\"event_type\":\"task\"}" | plane-emit.sh''' + "\n",
        encoding="utf-8")
    writer = tmp_path / "claudlobby" / "competing_writer.py"
    writer.parent.mkdir()
    writer.write_text(
        'batch = {"events": [{"event_type": "assignment", "payload": {}}]}\n'
        'row = {"kind": "task", "event": "progress"}\n'
        '_raw(ctx, request_id, "work_item", payload, receipt)\n'
        'other = {"event_type": "communication"}\n', encoding="utf-8")
    owner = tmp_path / "claudlobby" / "task_operations.py"
    owner.write_text('owned = {"event_type": "task"}\n', encoding="utf-8")
    observer = tmp_path / "claudlobby" / "_runtime_scripts" / "observer.sh"
    observer.parent.mkdir()
    observer.write_text(
        '# {"event_type": "task"} describes historical input\n'
        'printf \'{"event_type":"system"}\' | plane-emit.sh\n'
        'printf \'{"event_type":"communication"}\' | plane-emit.sh\n',
        encoding="utf-8")
    recipe_ok = tmp_path / "templates" / "claude.md.j2"
    recipe_ok.parent.mkdir()
    recipe_ok.write_text("Use claudlobby task assign.\n", encoding="utf-8")
    assert _task_source_violations(tmp_path) == [
        "claudlobby/competing_writer.py:1: direct task-family fact",
        "claudlobby/competing_writer.py:2: direct task-family fact",
        "claudlobby/competing_writer.py:3: direct task-family fact",
        "library/skills/dispatch/SKILL.md:1: retired task door",
        "library/skills/dispatch/SKILL.md:2: direct task-family fact",
        "library/tools/shortcut/tool.sh:1: direct task-family fact",
    ]
