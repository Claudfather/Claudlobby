"""Tests for claudlobby report-back subcommand (issue #242)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from claudlobby.__main__ import main


@pytest.fixture
def ledger_root(tmp_path):
    """Create a tmp_path with runtime/fleet/report-back.jsonl for root mode."""
    fleet_dir = tmp_path / "runtime" / "fleet"
    fleet_dir.mkdir(parents=True)

    (tmp_path / "fleet.yaml").write_text("fleet:\n  name: test\n  bots: {}\n")
    (tmp_path / "library").mkdir()
    (tmp_path / "lib").mkdir()

    now = datetime.now(timezone.utc)
    entries = [
        {
            "ts": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bot": "eng-1",
            "status": "completed",
            "summary": "Fixed auth",
            "pr_url": "https://github.com/org/repo/pull/1",
            "issues": "",
            "skill": "",
        },
        {
            "ts": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bot": "eng-2",
            "status": "blocked",
            "summary": "Missing token",
            "pr_url": "",
            "issues": "",
            "skill": "",
        },
        {
            "ts": (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bot": "eng-1",
            "status": "progress",
            "summary": "Working on tests",
            "pr_url": "",
            "issues": "",
            "skill": "prs",
        },
    ]
    ledger = fleet_dir / "report-back.jsonl"
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    return tmp_path


def test_report_back_all(ledger_root, capsys):
    rc = main(["--root", str(ledger_root), "report-back"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "3 event(s)" in out
    assert "eng-1" in out
    assert "eng-2" in out


def test_report_back_filter_bot(ledger_root, capsys):
    rc = main(["--root", str(ledger_root), "report-back", "--bot", "eng-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 event(s)" in out
    assert "eng-2" not in out


def test_report_back_filter_status(ledger_root, capsys):
    rc = main(["--root", str(ledger_root), "report-back", "--status", "blocked"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 event(s)" in out
    assert "Missing token" in out


def test_report_back_since(ledger_root, capsys):
    rc = main(["--root", str(ledger_root), "report-back", "--since", "1h"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 event(s)" in out
    assert "Working on tests" in out


def test_report_back_json_output(ledger_root, capsys):
    rc = main(["--root", str(ledger_root), "report-back", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [l for l in out.strip().split("\n") if l]
    assert len(lines) == 3
    for line in lines:
        parsed = json.loads(line)
        assert "bot" in parsed
        assert "status" in parsed


def test_report_back_no_ledger(tmp_path, capsys):
    (tmp_path / "library").mkdir()
    (tmp_path / "lib").mkdir()
    rc = main(["--root", str(tmp_path), "report-back"])
    assert rc == 0


def test_report_back_overlay(tmp_path, capsys):
    fleet_dir = tmp_path / "local" / "myfleet"
    runtime = fleet_dir / "runtime"
    runtime.mkdir(parents=True)
    (fleet_dir / "fleet.yaml").write_text("fleet:\n  name: myfleet\n  bots: {}\n")
    (tmp_path / "library").mkdir()
    (tmp_path / "lib").mkdir()

    now = datetime.now(timezone.utc)
    entry = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bot": "worker",
        "status": "completed",
        "summary": "Done",
        "pr_url": "",
        "issues": "",
        "skill": "",
    }
    (runtime / "report-back.jsonl").write_text(json.dumps(entry) + "\n")

    rc = main(["--root", str(tmp_path), "--fleet", "myfleet", "report-back"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 event(s)" in out
