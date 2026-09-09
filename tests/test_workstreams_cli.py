"""Tests for the read-only `claudlobby workstreams` view (workstreams.py) — the
plane's rendering of the registry (F18 closure R2b: no file)."""

from __future__ import annotations

from pathlib import Path

from claudlobby.paths import Paths
from claudlobby.workstreams import format_list, format_show


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


# Deleted with the file (F18 R2b): test_load_missing_registry_returns_empty,
# test_load_reads_root_mode_registry, test_load_corrupt_registry_returns_empty,
# TestUnreachableRegistryIsNotAnEmptyOne.test_load_workstreams_itself_is_unchanged —
# `load_workstreams` and `registry_path` are gone; the registry is the plane's
# rendering (`plane_workstreams`).


def _seed(root: Path, fleet: str) -> None:
    """A plane that knows the fleet (one registry row) but holds no workstream."""
    from claudlobby.plane.emit_api import emit_batch
    out = emit_batch(root, [{
        "event_type": "system", "emitter": "test", "fleet": fleet,
        "payload": {"event": "keepalive_skip", "subject_kind": "actor", "subject": f"bot:{fleet}/alex",
                    "data": {"source": "test", "legacy_ts": "2026-05-27T10:00:00Z", "data": {}}}}])
    assert out[0].status == "committed", out


def _open_ws(root: Path, fleet: str, wid: str) -> None:
    from claudlobby.plane.emit_api import emit_batch
    out = emit_batch(root, [{
        "event_type": "workstream", "emitter": "workstream-update", "fleet": fleet,
        "source_ref": f"workstreams:{wid}", "occurred_at": "2026-07-01T00:00:00Z",
        "payload": {"workstream_id": wid, "title": "X", "opened_by": f"bot:{fleet}/alex",
                    "owner": f"bot:{fleet}/alex", "goal": "do thing"}}])
    assert out[0].status == "committed", out


REPO = Path(__file__).resolve().parent.parent


class TestUnreachableRegistryIsNotAnEmptyOne:
    """Same class as #1216, on the plane (F18 R2b): "No workstreams." from a
    plane that could not be read would tell a manager "this fleet has nothing
    open" when the truth is "the registry could not be rendered, or I resolved
    the wrong root". The command REFUSES (rc 3) with the note on stderr.
    """

    def _root(self, tmp_path):
        (tmp_path / "library").mkdir(exist_ok=True)
        if not (tmp_path / "lib").exists():
            (tmp_path / "lib").symlink_to(REPO / "lib")
        (tmp_path / "fleet.yaml").write_text("fleet:\n  name: test\n  bots: {}\n")
        return tmp_path

    def test_an_absent_plane_exits_three_and_says_so(self, tmp_path, capsys):
        from claudlobby.__main__ import main

        rc = main(["--root", str(self._root(tmp_path)), "workstreams"])
        cap = capsys.readouterr()
        assert rc == 3
        assert "UNREACHABLE" in cap.err and "plane" in cap.err
        assert "No workstreams." not in cap.out

    def test_a_plane_that_holds_the_fleet_but_no_workstream_still_answers_no_workstreams(
        self, tmp_path, capsys
    ):
        """The control. Presence, not emptiness — a fleet that has opened nothing
        yet gets the TRUE answer, and the refusal must not fire on it."""
        from claudlobby.__main__ import main

        root = self._root(tmp_path)
        _seed(root, "test")
        rc = main(["--root", str(root), "workstreams"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "No workstreams." in out

    def test_a_plane_that_never_saw_the_fleet_is_refused_not_empty(self, tmp_path, capsys):
        from claudlobby.__main__ import main

        root = self._root(tmp_path)
        _seed(root, "another-fleet")
        rc = main(["--root", str(root), "workstreams"])
        cap = capsys.readouterr()
        assert rc == 3 and "holds no bot of fleet" in cap.err
        assert "No workstreams." not in cap.out

    def test_a_populated_plane_is_unaffected(self, tmp_path, capsys):
        """Positive control: without it the assertions above would hold on a
        command that had stopped listing anything."""
        from claudlobby.__main__ import main

        root = self._root(tmp_path)
        _open_ws(root, "test", "ws-a")
        rc = main(["--root", str(root), "workstreams"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ws-a" in out
        rc = main(["--root", str(root), "workstreams", "show", "ws-a"])
        assert rc == 0 and "owner:    alex" in capsys.readouterr().out
