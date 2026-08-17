"""Tests for the read-only `claudlobby workstreams` view (workstreams.py)."""

from __future__ import annotations

import json
from pathlib import Path

from claudlobby.paths import Paths
from claudlobby.workstreams import format_list, format_show, load_workstreams


def _entry(**kw) -> dict:
    base = {
        "id": "ws-x", "fleet": "f", "title": "X", "project": None,
        "status": "active", "owner_bot": "alex", "next": "do thing",
        "opened_ts": "2026-07-01T00:00:00Z", "last_progress_ts": "2026-07-01T00:00:00Z",
        "lease_expires_ts": "2026-08-01T00:00:00Z",
        "task_ids": [], "refs": {"issues": [], "prs": []}, "renewals": [],
    }
    base.update(kw)
    return base


def test_format_list_empty():
    assert format_list({}) == "No workstreams."


def test_format_list_orders_active_before_terminal_and_has_columns():
    ws = {
        "ws-a": _entry(id="ws-a", status="done"),
        "ws-b": _entry(id="ws-b", status="active"),
    }
    lines = format_list(ws).splitlines()
    assert "STATUS" in lines[0]
    active_idx = next(i for i, ln in enumerate(lines) if "ws-b" in ln)
    done_idx = next(i for i, ln in enumerate(lines) if "ws-a" in ln)
    assert active_idx < done_idx


def test_format_show_includes_key_fields():
    out = format_show(
        _entry(task_ids=["t-1-abcd"], renewals=[{"ts": "x", "note": "waiting on review"}])
    )
    assert "ws-x — X" in out
    assert "owner:    alex" in out
    assert "t-1-abcd" in out
    assert "renewals: 1" in out
    assert "waiting on review" in out


def test_load_missing_registry_returns_empty(tmp_path: Path):
    assert load_workstreams(Paths(root=tmp_path, fleet_dir=None)) == {}


def test_load_reads_root_mode_registry(tmp_path: Path):
    reg = tmp_path / "runtime" / "fleet" / "workstreams.json"
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps({"updated": "x", "workstreams": {"ws-x": _entry()}}))
    ws = load_workstreams(Paths(root=tmp_path, fleet_dir=None))
    assert "ws-x" in ws


def test_load_corrupt_registry_returns_empty(tmp_path: Path):
    reg = tmp_path / "runtime" / "fleet" / "workstreams.json"
    reg.parent.mkdir(parents=True)
    reg.write_text("{not json")
    assert load_workstreams(Paths(root=tmp_path, fleet_dir=None)) == {}


class TestUnreachableRegistryIsNotAnEmptyOne:
    """Same class as #1216. ``load_workstreams`` returns {} for an absent registry
    AND for an empty one, so ``format_list`` printed "No workstreams." either way.
    A manager reading that cannot tell "this fleet has nothing open" from "the
    registry was never created, or I resolved the wrong tier".
    """

    def _root(self, tmp_path):
        (tmp_path / "library").mkdir(exist_ok=True)
        (tmp_path / "lib").mkdir(exist_ok=True)
        (tmp_path / "fleet.yaml").write_text("fleet:\n  name: test\n  bots: {}\n")
        return tmp_path

    def test_an_absent_registry_exits_nonzero_and_says_so(self, tmp_path, capsys):
        from claudlobby.__main__ import main

        rc = main(["--root", str(self._root(tmp_path)), "workstreams"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "cannot read the workstream registry" in out
        assert "No workstreams." not in out

    def test_a_present_but_empty_registry_still_answers_no_workstreams(
        self, tmp_path, capsys
    ):
        """The control. Presence, not emptiness — a fleet that has opened nothing
        yet gets the TRUE answer, and the refusal must not fire on it."""
        from claudlobby.__main__ import main

        root = self._root(tmp_path)
        reg = root / "runtime" / "fleet" / "workstreams.json"
        reg.parent.mkdir(parents=True)
        reg.write_text(json.dumps({"workstreams": {}}))

        rc = main(["--root", str(root), "workstreams"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "No workstreams." in out

    def test_a_populated_registry_is_unaffected(self, tmp_path, capsys):
        """Positive control: without it the assertions above would hold on a
        command that had stopped listing anything."""
        from claudlobby.__main__ import main

        root = self._root(tmp_path)
        reg = root / "runtime" / "fleet" / "workstreams.json"
        reg.parent.mkdir(parents=True)
        reg.write_text(json.dumps({"workstreams": {"ws-a": _entry(id="ws-a")}}))

        rc = main(["--root", str(root), "workstreams"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ws-a" in out

    def test_load_workstreams_itself_is_unchanged(self, tmp_path):
        """The probe is in the COMMAND, not the loader: brief.py imports
        load_workstreams and has its own remedy for a missing registry, so the
        loader keeps returning {} rather than raising."""
        p = Paths(root=tmp_path)
        assert load_workstreams(p) == {}
