"""PR-B armed-path e2e: the REAL doors, the REAL shim (cold-CLI rung), a REAL
plane db — the dual-write contract end to end, with parity as the verdict.

Harness = test_task_id_dispatch's _fake_lib pattern extended: doors +
lib-common + the plane shim are symlinked so LIB_DIR resolves inside the fake
lib dir; transport (dispatch.sh / tmux / bot_tmux_send's tmux) is stubbed;
PLANE_EMIT_CLI points at the venv's real CLI so the shim's fallback rung does
real ingest into <tmp>/state/plane/plane.db."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.queries import TASK_STATUS_SQL, WORKSTREAM_STATUS_SQL

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
CLI = Path(sys.executable).parent / "claudlobby"

DOOR_FILES = (
    "dispatch-task.sh", "report-back.sh", "workstream-update.sh",
    "lib-common.sh", "plane-emit.sh", "plane-socket-client.py",
    "dispatch-overdue.py",
)


def _plane_lib(tmp_path: Path) -> tuple[Path, dict]:
    libdir = tmp_path / "lib"
    libdir.mkdir()
    for name in DOOR_FILES:
        (libdir / name).symlink_to(LIB_DIR / name)
    stub = libdir / "dispatch.sh"
    stub.write_text("#!/bin/bash\nexit 0\n")
    stub.chmod(0o755)
    tmux = tmp_path / "tmux"
    tmux.write_text("#!/bin/bash\nexit 0\n")
    tmux.chmod(0o755)
    env = {
        "CLAUDLOBBY_ROOT": str(tmp_path),
        "TMUX_BIN": str(tmux),
        "OBSERVABILITY_DISPATCH_DEADLINE": "600",
        "BOT_ID": "lead",
        "BOT_NAME": "lead",
        "FLEET_NAME": "e2e-fleet",
        "PLANE_EMIT_ENABLED": "1",
        "PLANE_EMIT_CLI": str(CLI),
        # No daemon in this harness: the shim's socket rung fails (exit 5,
        # disclosed) and the cold-CLI rung does the real ingest — rung 2 is
        # itself under test here.
        "PLANE_SOCKET": str(tmp_path / "no-daemon.sock"),
        "PATH": "/usr/bin:/bin",
    }
    return libdir, env


def _bash(cmd: str, env: dict, cwd=None):
    return subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True,
        env=env, cwd=cwd, timeout=120,
    )


def _rows(tmp_path: Path, sql: str, params: tuple = ()):
    conn = connect(db_path(tmp_path))
    out = conn.execute(sql, params).fetchall()
    conn.close()
    return out


def _ledger_row(tmp_path: Path) -> dict:
    lines = (tmp_path / "state" / "dispatch-log.jsonl").read_text().splitlines()
    return json.loads(lines[-1])


@pytest.fixture()
def armed(tmp_path: Path):
    return _plane_lib(tmp_path)


def test_dispatch_task_armed_lands_the_construct_triple(tmp_path, armed):
    libdir, env = armed
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "fix the widget"', env)
    assert r.returncode == 0, r.stderr
    row = _ledger_row(tmp_path)
    assert row["plane_msg_id"].startswith("msg_")
    assert row["plane_work_item_id"].startswith("wi_")
    assert row["plane_assignment_id"].startswith("asg_")
    conn = connect(db_path(tmp_path))
    comm = conn.execute(
        "SELECT sender_alias, recipient_raw, message_class, command_type,"
        " work_item_id, assignment_id, source_ref FROM communications"
    ).fetchone()
    asg = conn.execute(
        "SELECT assignment_id, work_item_id, dispatch_msg_id, expected_by,"
        " source_ref FROM assignments").fetchone()
    wi = conn.execute("SELECT work_item_id, title FROM work_items").fetchone()
    tx = conn.execute(
        "SELECT event, carrier, msg_id FROM events WHERE kind='transmission'"
    ).fetchone()
    conn.close()
    assert comm["sender_alias"] == "bot:e2e-fleet/lead"
    assert comm["recipient_raw"] == "w1"
    assert comm["message_class"] == "task_request"
    assert comm["command_type"] == "task"          # --botcommand == --type task
    assert comm["work_item_id"] == row["plane_work_item_id"]
    assert comm["assignment_id"] == row["plane_assignment_id"]
    assert comm["source_ref"] == f"dispatch-log:{row['task_id']}"
    assert asg["dispatch_msg_id"] == row["plane_msg_id"]
    assert asg["expected_by"] is not None
    assert wi["title"] == "fix the widget"
    assert tx["event"] == "pane_submitted" and tx["carrier"] == "tmux"
    # §6b activation: a submitted dispatch is OPEN
    statuses = dict(_rows(tmp_path, TASK_STATUS_SQL))
    assert statuses[row["plane_assignment_id"]] == "open"


def test_dispatch_query_armed_is_communication_only(tmp_path, armed):
    libdir, env = armed
    r = _bash(f'"{libdir}/dispatch-task.sh" --type query w1 "what is the retry logic"', env)
    assert r.returncode == 0, r.stderr
    conn = connect(db_path(tmp_path))
    comm = conn.execute(
        "SELECT message_class, command_type, work_item_id FROM communications"
    ).fetchone()
    n_wi = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
    n_asg = conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
    conn.close()
    assert comm["message_class"] == "question"
    assert comm["command_type"] == "query"
    assert comm["work_item_id"] is None
    assert n_wi == 0 and n_asg == 0
    row = _ledger_row(tmp_path)
    assert row["plane_msg_id"].startswith("msg_")
    assert row["plane_work_item_id"] == "" and row["plane_assignment_id"] == ""


def test_unarmed_door_writes_nothing_and_ledger_fields_empty(tmp_path, armed):
    libdir, env = armed
    env = {**env, "PLANE_EMIT_ENABLED": "0"}
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "quiet fleet"', env)
    assert r.returncode == 0, r.stderr
    row = _ledger_row(tmp_path)
    assert row["plane_msg_id"] == ""
    assert not db_path(tmp_path).exists() or _rows(
        tmp_path, "SELECT COUNT(*) FROM communications")[0][0] == 0


def test_report_back_links_task_facts_and_closes_the_status(tmp_path, armed):
    libdir, env = armed
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "build the door"', env)
    assert r.returncode == 0, r.stderr
    row = _ledger_row(tmp_path)
    task_id = row["task_id"]
    r2 = _bash(
        f'"{libdir}/report-back.sh" w1 completed "door built"'
        f' --pr https://github.com/org/repo/pull/9 --task {task_id}', env)
    assert r2.returncode == 0, r2.stderr
    conn = connect(db_path(tmp_path))
    report_comm = conn.execute(
        "SELECT message_class, work_item_id, assignment_id FROM communications"
        " WHERE message_class='report'").fetchone()
    task_ev = conn.execute(
        "SELECT event, work_item_id, assignment_id, detail FROM events"
        " WHERE kind='task'").fetchone()
    txs = [r[0] for r in conn.execute(
        "SELECT event FROM events WHERE kind='transmission' ORDER BY ingest_seq")]
    conn.close()
    assert report_comm["work_item_id"] == row["plane_work_item_id"]
    assert report_comm["assignment_id"] == row["plane_assignment_id"]
    assert task_ev["event"] == "completed"
    assert task_ev["work_item_id"] == row["plane_work_item_id"]
    assert "pull/9" in (task_ev["detail"] or "")
    # Dispatch rides the stubbed dispatch.sh (clean -> pane_submitted); the
    # report rides bot_tmux_send, which in this fake root resolves NO manager
    # socket and drops the send — and the door must RECORD that truth: failed,
    # never a fabricated submit. (The true submitted case for
    # bot_tmux_send-class sends is pinned by the pane_send_verified suite and
    # exercised live by the canary.)
    assert txs == ["pane_submitted", "failed"]
    statuses = dict(_rows(tmp_path, TASK_STATUS_SQL))
    assert statuses[row["plane_assignment_id"]] == "completed"
    # report ledger carries the parity join
    rb = json.loads((tmp_path / "runtime" / "fleet" / "report-back.jsonl"
                     ).read_text().splitlines()[-1])
    assert rb["plane_msg_id"].startswith("msg_")


def test_workstream_lifecycle_emits_construct_and_verb_events(tmp_path, armed):
    libdir, env = armed
    r = _bash(f'"{libdir}/workstream-update.sh" open "Ship the plane" --next "doors"', env)
    assert r.returncode == 0, r.stderr
    ws_id = r.stdout.strip().splitlines()[-1]
    for cmd in (
        f'"{libdir}/workstream-update.sh" progress {ws_id} --next "canary"',
        f'"{libdir}/workstream-update.sh" renew {ws_id} --note "waiting on review"',
        f'"{libdir}/workstream-update.sh" close {ws_id} --status done',
    ):
        rr = _bash(cmd, env)
        assert rr.returncode == 0, rr.stderr
    conn = connect(db_path(tmp_path))
    ws = conn.execute(
        "SELECT workstream_id, title, opened_by_uid FROM workstreams").fetchone()
    events = [(r[0], r[1]) for r in conn.execute(
        "SELECT event, detail FROM events WHERE kind='workstream'"
        " ORDER BY ingest_seq")]
    renewed = conn.execute(
        "SELECT renewed_until FROM events WHERE kind='workstream'"
        " AND event='renewed'").fetchone()
    conn.close()
    assert ws["workstream_id"] == ws_id and ws["title"] == "Ship the plane"
    assert [e for e, _ in events] == ["progressed", "renewed", "closed"]
    assert renewed["renewed_until"] is not None
    assert '"disposition": "done"' in events[2][1] or '"disposition":"done"' in events[2][1]
    statuses = dict(_rows(tmp_path, WORKSTREAM_STATUS_SQL,
                          ("2026-01-01", "2026-01-01")))
    assert statuses[ws_id] == "closed"


def test_parity_is_the_verdict_dispatch_lane_clean(tmp_path, armed):
    libdir, env = armed
    for i in range(3):
        r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "task {i}"', env)
        assert r.returncode == 0, r.stderr
    r = subprocess.run(
        [sys.executable, str(LIB_DIR / "plane-parity.py"),
         "--legacy", str(tmp_path / "state" / "dispatch-log.jsonl"),
         "--ledger-name", "dispatch-log", "--id-field", "task_id",
         "--db", str(db_path(tmp_path))],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "matched: 3" in r.stdout


def test_plane_failure_never_blocks_the_door(tmp_path, armed):
    libdir, env = armed
    env = {**env, "PLANE_EMIT_CLI": "/bin/false"}   # both rungs dead
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "still dispatches"', env)
    assert r.returncode == 0, "a dead plane must never block a dispatch"
    assert "plane record failed" in r.stderr
    row = _ledger_row(tmp_path)
    assert row["task_id"].startswith("t-"), "legacy ledger row still lands"