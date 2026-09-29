"""Canonical timer attention through the existing active Task CLI fixture."""

from __future__ import annotations

import sqlite3
from uuid import uuid4

from claudlobby import message_operations, operation_context
from claudlobby.__main__ import main
from claudlobby.message_transport import TransportOutcome
from claudlobby.plane.db import db_file
from tests.test_activation import cold, tmp_path  # noqa: F401 — active fixture dependencies
from tests.test_releases import installed  # noqa: F401 — dependency of cold
from tests.test_task_write_cli import _call, active  # noqa: F401 — shared private activated fixture


def _admit(capsys, root, title):
    return _call(capsys, root, "task", "admit", "--title", title,
                 "--request-id", str(uuid4()))["data"]["task_id"]


def _recheck(capsys, root, request_id=None, *, expected=0, **flags):
    argv = ["task", "recheck", "--max-age-h", str(flags.get("max_age_h", 0)),
            "--request-id", request_id or str(uuid4())]
    if flags.get("dry_run"):
        argv.append("--dry-run")
    if flags.get("by"):
        argv.extend(("--by", flags["by"]))
    return _call(capsys, root, *argv, expected=expected)


def test_recheck_caps_one_digest_and_debounces_per_canonical_task(active, monkeypatch, capsys):  # noqa: F811
    root, _release = active
    monkeypatch.setattr(operation_context, "_local_operator_alias", lambda: "human:operator")
    sent = []
    strict = message_operations.send_committed_native_attempt

    def send_once(*args, **kwargs):
        def native(*_, **options):
            receipt = args[3]
            assert receipt.stages[0].status == "committed"
            with sqlite3.connect(db_file(root)) as conn:
                expected = 8 if not sent else 1
                assert conn.execute("SELECT COUNT(*) FROM communications WHERE source_ref LIKE ?",
                                    (f"task-recheck:%:{receipt.request_id}",)).fetchone()[0] == expected
            sent.append(options["body"])
            return TransportOutcome("submitted", "sha256:" + "a" * 64, 99, 0)
        return strict(*args, transport=native, **kwargs)

    monkeypatch.setattr(message_operations, "send_committed_native_attempt", send_once)
    ids = [_admit(capsys, root, f"Queued {index}") for index in range(9)]
    dry = _recheck(capsys, root, dry_run=True)
    assert dry["data"]["task_ids"] == ids[:8] and dry["data"]["overflow"] == 1
    with sqlite3.connect(db_file(root)) as conn:
        before = conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0]
    first_id = str(uuid4())
    first = _recheck(capsys, root, first_id, expected=5, by="bot:example/worker")
    assert first["data"]["task_ids"] == ids[:8]
    assert first["data"]["bookkeeping_asks"] == 8
    assert first["data"]["bookkeeping_delivery"] == "no_individual_proof"
    assert first["data"]["transport"] == "submitted" and len(sent) == 1
    assert all(task_id in sent[0] for task_id in ids[:8]) and ids[8] not in sent[0]
    with sqlite3.connect(db_file(root)) as conn:
        asks = conn.execute("SELECT msg_id, work_item_id, sender_alias, recipient_alias "
                            "FROM communications WHERE source_ref LIKE ? ORDER BY ingest_seq",
                            (f"task-recheck:%:{first_id}",)).fetchall()
        transmissions = conn.execute("SELECT COUNT(*) FROM events WHERE kind='transmission' "
                                     "AND msg_id IN (SELECT msg_id FROM communications WHERE source_ref LIKE ?)",
                                     (f"task-recheck:%:{first_id}",)).fetchone()[0]
        provenance = conn.execute("SELECT subject_kind, subject_alias FROM events "
                                  "WHERE kind='system' AND event='task_recheck' "
                                  "AND source_ref=?", (f"request:{first_id}",)).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == before + 8
    assert [row[1] for row in asks] == ids[:8]
    assert all(row[2:] == ("human:operator", "bot:example/manager") for row in asks)
    assert transmissions == 1
    assert provenance == ("actor", "bot:example/worker")
    replay = _recheck(capsys, root, first_id, expected=5, by="bot:example/worker")
    assert replay["data"]["replayed"] is True
    assert replay["data"]["task_ids"] == ids[:8] and len(sent) == 1
    second = _recheck(capsys, root, expected=5)
    assert second["data"]["task_ids"] == ids[8:]
    assert second["data"]["held"] == 8 and len(sent) == 2


def test_known_failure_retries_next_tick_and_empty_uuid_stays_empty(active, monkeypatch, capsys):  # noqa: F811
    root, _release = active
    monkeypatch.setattr(operation_context, "_local_operator_alias", lambda: "human:operator")
    calls = []
    strict = message_operations.send_committed_native_attempt

    def send_once(*args, **kwargs):
        def native(*_, **options):
            calls.append(options["body"])
            return TransportOutcome("failed") if len(calls) == 1 else TransportOutcome(
                "submitted", "sha256:" + "b" * 64, 80, 0)
        return strict(*args, transport=native, **kwargs)

    monkeypatch.setattr(message_operations, "send_committed_native_attempt", send_once)
    with sqlite3.connect(db_file(root)) as conn:
        actors_before = conn.execute("SELECT COUNT(*) FROM identity_registry WHERE kind='actor'").fetchone()[0]
    preview = _recheck(capsys, root, dry_run=True)
    assert preview["data"]["task_ids"] == []
    with sqlite3.connect(db_file(root)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM identity_registry WHERE kind='actor'").fetchone()[0] == actors_before
    empty_id = str(uuid4())
    empty = _recheck(capsys, root, empty_id)
    assert empty["data"]["task_ids"] == [] and empty["data"]["recording"] == "committed"
    assert empty["data"]["request_persisted"] is True
    assert empty["data"]["notification"] == "not_requested"
    with sqlite3.connect(db_file(root)) as conn:
        noop = conn.execute("SELECT subject_kind, subject_alias FROM events "
                            "WHERE kind='system' AND event='task_recheck_noop' "
                            "AND source_ref=?", (f"request:{empty_id}",)).fetchone()
    assert noop == ("actor", "human:operator")
    task_id = _admit(capsys, root, "Queue for manager")
    empty_replay = _recheck(capsys, root, empty_id)
    assert empty_replay["data"]["replayed"] is True and empty_replay["data"]["task_ids"] == []
    first = _recheck(capsys, root, expected=5)
    assert first["data"]["task_ids"] == [task_id]
    assert first["data"]["transport"] == "failed" and len(calls) == 1
    again = _recheck(capsys, root, expected=5)
    assert again["data"]["task_ids"] == [task_id] and len(calls) == 2
    held = _recheck(capsys, root)
    assert held["data"]["task_ids"] == [] and held["data"]["held"] == 1
    assert len(calls) == 2


def test_private_timer_off_switch_skips_before_request_or_identity(active, monkeypatch, capsys):  # noqa: F811
    root, _release = active
    monkeypatch.setenv("TASK_RECHECK_ENABLED", "0")
    with sqlite3.connect(db_file(root)) as conn:
        before = conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0]
    assert main(["--root", str(root), "_task-recheck-tick", "example"]) == 0
    assert "TASK_RECHECK_ENABLED=0" in capsys.readouterr().out
    with sqlite3.connect(db_file(root)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0] == before


def test_uncertain_digest_holds_new_uuid_and_escalated_work_is_waiting(active, monkeypatch, capsys):  # noqa: F811
    root, _release = active
    monkeypatch.setattr(operation_context, "_local_operator_alias", lambda: "human:operator")
    calls = []
    strict = message_operations.send_committed_native_attempt

    def send_once(*args, **kwargs):
        def native(*_, **options):
            calls.append(options["body"])
            return TransportOutcome("unknown")
        return strict(*args, transport=native, **kwargs)

    monkeypatch.setattr(message_operations, "send_committed_native_attempt", send_once)
    due = _admit(capsys, root, "Needs review")
    waiting = _admit(capsys, root, "Waiting on human")
    _call(capsys, root, "task", "escalate", waiting, "--question", "Choose scope",
          "--request-id", str(uuid4()))
    first = _recheck(capsys, root, expected=5)
    assert first["data"]["task_ids"] == [due] and first["data"]["waiting"] == 1
    assert first["data"]["transport"] == "unknown" and len(calls) == 1
    held = _recheck(capsys, root)
    assert held["data"]["task_ids"] == [] and held["data"]["uncertain"] == 1
    assert held["data"]["uncertain_request_ids"] == [first["request_id"]]
    assert held["data"]["waiting"] == 1 and len(calls) == 1
