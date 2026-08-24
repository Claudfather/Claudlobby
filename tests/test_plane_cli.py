from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.migrations import migrate


def _run(args: list[str], stdin: str | None = None, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, "-m", "claudlobby", *args],
        input=stdin, capture_output=True, text=True, cwd=cwd,
    )


def _intent_json() -> str:
    return json.dumps({
        "event_type": "communication",
        "emitter": "cli-test",
        "fleet": "example-fleet",
        "payload": {
            "msg_id": "msg_" + "3" * 32,
            "sender": "bot:example-fleet/alpha",
            "message_class": "chat",
            "body": "via cli",
            "privacy": "full",
        },
    })


def test_emit_commits_and_prints_event_id(tmp_path: Path):
    r = _run(["--root", str(tmp_path), "emit", "communication", "--json", "-"],
             stdin=_intent_json())
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().startswith("ev_")
    conn = connect(db_path(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1
    conn.close()


def test_emit_contract_violation_exits_2(tmp_path: Path):
    bad = json.loads(_intent_json())
    bad["payload"]["message_class"] = "yell"
    r = _run(["--root", str(tmp_path), "emit", "communication", "--json", "-"],
             stdin=json.dumps(bad))
    assert r.returncode == 2
    assert "message_class" in r.stderr
    # Nothing written, nothing spooled:
    assert not (tmp_path / "state" / "plane" / "spool").exists() or not list(
        (tmp_path / "state" / "plane" / "spool").glob("*.json")
    )


def test_capture_modes_per_family(tmp_path: Path):
    """Round-3 F8: metadata/full behavior for EVERY content family."""
    import json as _json

    cap = tmp_path / "state" / "plane"
    cap.mkdir(parents=True)
    (cap / "capture.json").write_text('{"*": "metadata"}')
    # communication: body dropped, proof triple kept
    r = _run(["--root", str(tmp_path), "emit", "communication", "--json", "-"],
             stdin=_intent_json())
    assert r.returncode == 0, r.stderr
    from claudlobby.plane.db import connect, db_path
    conn = connect(db_path(tmp_path))
    row = conn.execute("SELECT body, body_sha256, privacy FROM communications").fetchone()
    assert row["body"] is None and row["body_sha256"] and row["privacy"] == "metadata"
    # work_item: body dropped silently
    wi = {"event_type": "work_item", "emitter": "t", "fleet": "example-fleet",
          "payload": {"work_item_id": "wi_" + "5" * 32, "title": "x",
                       "created_by": "bot:example-fleet/alpha", "body": "secret"}}
    r = _run(["--root", str(tmp_path), "emit", "work_item", "--json", "-"],
             stdin=_json.dumps(wi))
    assert r.returncode == 0, r.stderr
    assert conn.execute("SELECT body FROM work_items").fetchone()["body"] is None
    # task: summary dropped in metadata mode
    te = {"event_type": "task", "emitter": "t", "fleet": "example-fleet",
          "payload": {"work_item_id": "wi_" + "5" * 32, "event": "progress",
                       "summary": "secret detail"}}
    r = _run(["--root", str(tmp_path), "emit", "task", "--json", "-"],
             stdin=_json.dumps(te))
    assert r.returncode == 0, r.stderr
    detail = conn.execute("SELECT detail FROM events WHERE kind='task'").fetchone()["detail"]
    assert detail is None or "secret" not in detail
    # full mode: EVERY content family survives (round-5 F8 — task alone
    # was tested; communication body and work_item body now asserted too)
    (cap / "capture.json").write_text('{"*": "full"}')
    comm2 = _json.loads(_intent_json())
    comm2["payload"]["msg_id"] = "msg_" + "6" * 32
    comm2["payload"]["body"] = "full-mode communication body"
    _run(["--root", str(tmp_path), "emit", "communication", "--json", "-"],
         stdin=_json.dumps(comm2))
    row2 = conn.execute(
        "SELECT body, privacy FROM communications ORDER BY ingest_seq DESC"
    ).fetchone()
    assert row2["body"] == "full-mode communication body" and row2["privacy"] == "full"
    wi2 = {**wi, "payload": {**wi["payload"], "work_item_id": "wi_" + "6" * 32,
                              "body": "full-mode objective body"}}
    _run(["--root", str(tmp_path), "emit", "work_item", "--json", "-"],
         stdin=_json.dumps(wi2))
    assert conn.execute(
        "SELECT body FROM work_items ORDER BY ingest_seq DESC"
    ).fetchone()["body"] == "full-mode objective body"
    te2 = {**te, "payload": {**te["payload"], "summary": "kept"}}
    _run(["--root", str(tmp_path), "emit", "task", "--json", "-"], stdin=_json.dumps(te2))
    kept = conn.execute(
        "SELECT detail FROM events WHERE kind='task' ORDER BY ingest_seq DESC"
    ).fetchone()["detail"]
    conn.close()
    assert kept and "kept" in kept


def test_plane_status_reports(tmp_path: Path):
    _run(["--root", str(tmp_path), "emit", "communication", "--json", "-"],
         stdin=_intent_json())
    r = _run(["--root", str(tmp_path), "plane", "status"])
    assert r.returncode == 0
    assert "communication" in r.stdout and "spool" in r.stdout


def test_plane_schema_exports_json(tmp_path: Path):
    r = _run(["--root", str(tmp_path), "plane", "schema"])
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert "envelope" in data and "task" in data
