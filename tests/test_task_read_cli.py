"""Public canonical reads from a selected private activation and real SQLite."""

import builtins
import json
from pathlib import Path
import sqlite3

import pytest

from claudlobby import activation, context
from claudlobby.__main__ import main
from claudlobby.plane.db import db_file
from tests.test_activation import cold, tmp_path  # noqa: F401 — activation and short socket root
from tests.test_releases import installed  # noqa: F401 — dependency of cold
from tests.test_task_state import _assignment, _task


@pytest.fixture
def active(cold, monkeypatch):  # noqa: F811 — pytest fixture parameter
    root, _, plan, host = cold
    for key in ("FLEET_NAME", "CLAUDLOBBY_FLEET", "BOT_ID", "BOT_NAME", "BOT_DIR", "FLEET_ROOT"):
        monkeypatch.delenv(key, raising=False)
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    monkeypatch.setattr(context, "get_resources", lambda: host.package)
    return root, host


def _call(capsys, root, *argv, expected=0):
    assert main(["--root", str(root), "--json", *argv]) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1 and result["ok"] is (expected == 0)
    assert result["request_id"] is None
    return result


def test_list_show_assignment_and_scoped_reference_hints_are_read_only(active, capsys):
    root, host = active
    from claudlobby.activation_identity import read_selected_identity_bindings
    bindings = read_selected_identity_bindings(root, "example", package=host.package)
    with sqlite3.connect(db_file(root)) as conn:
        _task(conn, "wi_first", fleet_uid=bindings["fleet_uid"], source_ref="dispatch-log:old-17")
        _task(conn, "wi_second", fleet_uid=bindings["fleet_uid"])
        conn.execute("UPDATE work_items SET project_key=NULL, workstream_id=NULL "
                     "WHERE fleet_uid=?", (bindings["fleet_uid"],))
        _assignment(conn, "asg_first", "wi_first", fleet_uid=bindings["fleet_uid"])
        conn.execute("UPDATE assignments SET assignee_uid=? WHERE assignment_id='asg_first'",
                     (bindings["bots"]["worker"],))
    with sqlite3.connect(db_file(root)) as conn:
        before = (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])

    first_result = _call(capsys, root, "task", "list", "--limit", "1")
    assert first_result["release_id"] == host.release.release_id
    first = first_result["data"]
    assert [item["task_id"] for item in first["items"]] == ["wi_first"]
    assert first["next_cursor"]
    second = _call(capsys, root, "task", "list", "--limit", "1", "--cursor",
                   first["next_cursor"])["data"]
    assert [item["task_id"] for item in second["items"]] == ["wi_second"]
    assert second["next_cursor"] is None
    filtered = _call(capsys, root, "task", "list", "--bot", "worker")["data"]
    assert [item["task_id"] for item in filtered["items"]] == ["wi_first"]
    shown = _call(capsys, root, "task", "show", "wi_first")["data"]["task"]
    assert shown["display_ids"] == ["old-17"]
    assignment = _call(capsys, root, "assignment", "show", "asg_first")["data"]
    assert assignment["assignment"]["task_id"] == shown["task_id"]
    wrong = _call(capsys, root, "task", "show", "asg_first", expected=4)["error"]
    assert wrong["code"] == "wrong_reference"
    assert wrong["hint"]["next_command"] == ["task", "show", "wi_first"]
    assert main(["--root", str(root), "task", "show", "asg_first"]) == 4
    plain = capsys.readouterr()
    assert "inspect claudlobby task show wi_first" in plain.err
    assert "ReferenceHint(" not in plain.err and "{'candidates'" not in plain.err
    legacy = _call(capsys, root, "task", "show", "old-17", expected=4)["error"]
    assert legacy["hint"]["total_matches"] == 1
    missing = _call(capsys, root, "task", "show", "wi_foreign", expected=3)["error"]
    assert missing["code"] == "not_found"
    if missing["hint"] is not None:
        assert missing["hint"]["candidates"] == []
    _call(capsys, root, "task", "list", "--limit", "2", "--cursor",
          first["next_cursor"], expected=2)
    _call(capsys, root, "task", "list", "--bot", "outside", expected=3)
    with sqlite3.connect(db_file(root)) as conn:
        after = (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                 conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])
    assert after == before


def test_empty_or_absent_plane_and_invalid_generated_origin_never_initialize(active, monkeypatch, capsys):
    root, _ = active
    assert _call(capsys, root, "task", "list")["data"]["items"] == []
    assert _call(capsys, root, "--seed", "task", "list", expected=4)["error"]["code"] == "conflict"
    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "")
    error = _call(capsys, root, "task", "list", expected=2)["error"]
    assert error["code"] == "invalid_argument"
    monkeypatch.delenv("BOT_ID")
    monkeypatch.delenv("FLEET_NAME")
    db_file(root).rename(root / "plane-db-offline")
    assert _call(capsys, root, "task", "list", expected=6)["error"]["code"] == "unavailable"
    assert not db_file(root).exists()


def test_task_reviews_reads_host_evidence_without_writing_and_refuses_plane_outage(active, monkeypatch, capsys):
    root, host = active
    from claudlobby import review_queries, review_rules
    from claudlobby.plane.emit_api import emit_batch

    url = "https://github.com/org/repo/pull/1046"
    ts = "2026-09-02T14:00:00Z"
    doc = Path(__file__).resolve().parents[1] / "library/expertise/code-review.md"
    examples = [line for line in doc.read_text().splitlines()
                if "<" not in line and review_rules.parse_verdict(line) == review_rules.APPROVE]
    assert examples, "the documented approve header must reach the public review reader"
    doc_header = examples[0]
    anchor = review_rules.parse_anchor(doc_header)
    assert anchor and review_rules.parse_header_identity(doc_header) == "alex"
    doc_head = anchor + "0" * (40 - len(anchor))
    emit_batch(root, [
        {"event_type": "system", "emitter": "report-back", "fleet": "other",
         "source_ref": f"report-back:msg_{'8':0>32}", "occurred_at": ts,
         "payload": {"event": "report_status", "subject_kind": "actor",
                     "subject": "bot:other/worker",
                     "data": {"status": "completed", "pr_url": url,
                              "pr_role": "reviewed"}}},
        {"event_type": "system", "emitter": "report-back", "fleet": "example",
         "source_ref": f"report-back:msg_{'9':0>32}", "occurred_at": ts,
         "payload": {"event": "report_status", "subject_kind": "actor",
                     "subject": "bot:example/worker",
                     "data": {"status": "completed", "pr_url": url,
                              "pr_role": "authored"}}},
        {"event_type": "system", "emitter": "report-back", "fleet": "other",
         "source_ref": f"report-back:msg_{'a':0>32}", "occurred_at": "2026-09-27T00:00:08Z",
         "payload": {"event": "report_status", "subject_kind": "actor",
                     "subject": "bot:other/alex",
                     "data": {"status": "completed",
                              "pr_url": "https://github.com/org/repo/pull/1913",
                              "pr_role": "reviewed"}}},
    ])
    head = "b27ffc2c16e9dc3972332a550925b33f1b6143b1"
    payload = {"number": 1046, "title": "Review", "headRefOid": head,
               "reviews": [{"submittedAt": "2026-09-02T13:59:52Z",
                            "body": "**[worker] [VERDICT] request-changes** reviewed against b27ffc2"}],
               "comments": []}
    doc_payload = {"number": 1913, "title": "Doc-sourced fixture", "headRefOid": doc_head,
                   "reviews": [{"submittedAt": "2026-09-27T00:00:00Z", "body": doc_header}],
                   "comments": []}

    def github(args):
        assert args[0:2] == ["pr", "view"]
        assert args[3:] == ["--repo", "org/repo", "--json",
                            review_queries.review_rules.PR_FIELDS]
        return {"1046": payload, "1913": doc_payload}[args[2]]

    monkeypatch.setattr(review_queries, "_gh", github)
    with sqlite3.connect(db_file(root)) as conn:
        before = (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])
    result = _call(capsys, root, "task", "reviews", "org/repo", "--pr", "1046")
    data = result["data"]
    assert result["release_id"] == host.release.release_id
    assert data["caller_fleet"] == "example" and data["host_scope"] == "all fleets in the selected root"
    assert data["rows"] == 2 and data["fleets"] == ["other"]
    assert data["attribution_events"][0]["events"][0]["actor"] == "bot:other/worker"
    assert data["prs"][0]["blocking"] and data["merge_authorization"] is False
    doc_result = _call(capsys, root, "task", "reviews", "org/repo", "--pr", "1913")["data"]
    assessed = doc_result["prs"][0]
    assert assessed["resolved"]["bot:other/alex"]["verdict"] == review_rules.APPROVE
    assert assessed["resolved"]["bot:other/alex"]["anchor"] == anchor
    assert assessed["blocking"] == assessed["stale"] == assessed["unanchored"] == []
    assert doc_result["attribution_events"][0]["events"][0]["actor"] == "bot:other/alex"
    with sqlite3.connect(db_file(root)) as conn:
        after = (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                 conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])
    assert after == before
    with sqlite3.connect(db_file(root)) as conn:
        changed = conn.execute(
            "UPDATE registry_snapshots SET host_uid=?"
            " WHERE entity_type='fleet' AND entity_alias='example'",
            ("host_" + "f" * 32,))
        assert changed.rowcount == 1
    mismatch = _call(capsys, root, "task", "reviews", "org/repo", "--pr", "1046",
                     expected=4)["error"]
    assert mismatch["code"] == "conflict" and "host" in mismatch["message"]
    with sqlite3.connect(db_file(root)) as conn:
        conn.execute("UPDATE registry_snapshots SET host_uid=?"
                     " WHERE entity_type='fleet' AND entity_alias='example'",
                     (data["host_uid"],))
        conn.execute("UPDATE registry_snapshots SET entity_alias='other'"
                     " WHERE entity_type='fleet' AND entity_alias='example'")
    mismatch = _call(capsys, root, "task", "reviews", "org/repo", "--pr", "1046",
                     expected=4)["error"]
    assert mismatch["code"] == "conflict"
    with sqlite3.connect(db_file(root)) as conn:
        conn.execute("UPDATE registry_snapshots SET entity_alias='example'"
                     " WHERE entity_type='fleet' AND entity_alias='other'")
    db_file(root).rename(root / "plane-db-offline")
    assert _call(capsys, root, "task", "reviews", "org/repo", "--pr", "1046",
                 expected=6)["error"]["code"] == "unavailable"
    assert not db_file(root).exists()


def test_help_and_syntax_remain_dependency_light(monkeypatch, capsys):
    original = builtins.__import__

    def reject(name, *args, **kwargs):
        if name.split(".")[0] in {"yaml", "jinja2", "pydantic"} or name.endswith(".task_read"):
            raise ImportError("blocked task read dependency")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", reject)
        with pytest.raises(SystemExit) as exited:
            main(["task", "list", "--help"])
        assert exited.value.code == 0
        assert "--cursor" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exited:
        main(["--json", "assignment", "show", "--bad=private-value"])
    output = capsys.readouterr().out
    assert exited.value.code == 2 and "private-value" not in output
    assert json.loads(output)["command"] == "assignment.show"
