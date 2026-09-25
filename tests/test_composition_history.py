"""Composition observations survive replacement of the latest snapshot."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby import composer
from claudlobby.commands import core
from claudlobby.paths import Paths
from claudlobby.plane import emit_api, registry_emit
from tests.conftest import load_test_fleet


@pytest.fixture
def generate_scene(fleet_dir, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PLANE_SOCKET", str(tmp_path / "absent.sock"))
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)
    monkeypatch.delenv("PLANE_EMIT_ENABLED", raising=False)
    (fleet_dir / "lib").symlink_to(Path(__file__).resolve().parents[1] / "lib")
    fleet = load_test_fleet(fleet_dir)
    for bot in fleet.bots.values():
        bot.telegram.handle = ""
    paths = Paths(root=fleet_dir, fleet_dir=fleet_dir)
    monkeypatch.setattr(core, "_resolve_paths", lambda _: paths)
    monkeypatch.setattr(core, "_load_env", lambda _: None)
    monkeypatch.setattr(core, "_load_fleet_or_exit", lambda _: (fleet, {}))
    monkeypatch.setattr(core, "validate", lambda *a: SimpleNamespace(has_errors=False, warnings=[]))
    monkeypatch.setattr(core, "_warn_unresolvable_skill_refs", lambda _: None)
    for name in ("compose_fleet_timers", "compose_host_timers", "compose_host_bot_handles", "compose_host_mention_allowlist"):
        monkeypatch.setattr(composer, name, lambda *a: tmp_path / "not-created")
    batches = []
    def record(root, events):
        assert Path(root) == fleet_dir
        batches.extend(events)
        return [SimpleNamespace(status="committed") for _ in events]
    monkeypatch.setattr(emit_api, "emit_batch", record)
    return paths, fleet, batches


@pytest.mark.parametrize("bot_id", [None, "lead"])
def test_generate_threads_its_observation_without_rereading_sidecar(generate_scene, monkeypatch, bot_id):
    paths, fleet, batches = generate_scene
    observations = []
    original = composer.manifest_provenance
    def snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        observations.append(result)
        return result
    def other_generate(_):
        # Another generate replaces the sidecar before the registry scan starts.
        (paths.runtime / "composed.json").write_text('{"fleet":"other-run"}')
    monkeypatch.setattr(composer, "manifest_provenance", snapshot)
    monkeypatch.setattr(core, "_warn_unresolvable_skill_refs", other_generate)
    assert core.cmd_generate(SimpleNamespace(bot=bot_id, strict=False)) == 0
    completion = [e for e in batches if e["event_type"] == "declaration"
                  and e["payload"]["event"] == "scan_completed"]
    assert len(completion) == 1
    actual = completion[0]["payload"]["composition"]
    assert len(observations) == 1
    assert actual["fleet"] == fleet.name
    assert actual["files"] == observations[0]["files"]
    assert actual["git"] == observations[0]["git"]
    assert actual["composed_at"] == observations[0]["composed_at"]
    assert actual["bot_ids"] == ([bot_id] if bot_id else sorted(fleet.bots))


def observation(fleet="test-fleet", *, interrupted=False, digest="a" * 64):
    return {"schema": 1, "fleet": fleet, "composed_at": "2026-09-25T01:00:00+00:00",
            "files": {"fleet.yaml": {"path": "/fixture/fleet.yaml", "sha256": digest, "present": True}},
            "git": {"in_git": True, "branch": "main", "commit": "abc123",
                    "on_default_branch": True, "dirty": interrupted, "interrupted": interrupted},
            "bot_ids": ["lead"]}


def completion(scan, composition=None, *, fleet="test-fleet", complete=True, time="2026-09-25T01:00:00+00:00"):
    from claudlobby.plane.ids import derive_uid
    return {"event_type": "declaration", "emitter": "generate", "fleet": fleet,
            "event_id": derive_uid("ev", f"test-scan:{fleet}:{scan}"), "occurred_at": time,
            "payload": {"event": "scan_completed", "subject_kind": "host", "subject": "fixture",
                        "scan_id": scan, "scope": f"host+shared+fleet:{fleet}", "counts": {},
                        "complete": complete,
                        **({"composition": composition} if composition is not None else {})}}


_REAL_EMIT = emit_api.emit_batch


def test_two_identical_generates_preserve_receipts_without_entity_versions(generate_scene, monkeypatch):
    from claudlobby.plane.db import connect_ro, db_file
    from claudlobby.plane.composition_history import read_compositions
    paths, fleet, batches = generate_scene
    monkeypatch.setattr(emit_api, "emit_batch", _REAL_EMIT)
    assert core.cmd_generate(SimpleNamespace(bot=None, strict=False)) == 0
    with connect_ro(db_file(paths.root)) as conn:
        before = conn.execute("SELECT count(*) FROM registry_snapshots").fetchone()[0]
    assert core.cmd_generate(SimpleNamespace(bot=None, strict=False)) == 0
    with connect_ro(db_file(paths.root)) as conn:
        assert conn.execute("SELECT count(*) FROM registry_snapshots").fetchone()[0] == before
        result = read_compositions(conn, fleet.name)
    rows = result["observations"]
    assert len(rows) == 2 and rows[0]["scan_id"] != rows[1]["scan_id"]
    assert rows[0]["composition"]["files"] == rows[1]["composition"]["files"]
    assert all(r["status"] == "recorded" for r in rows)


def test_changed_provenance_survives_clean_repair_and_retry_without_shape_change(generate_scene, monkeypatch):
    from claudlobby.plane.db import connect_ro, db_file
    from claudlobby.plane.composition_history import read_compositions
    paths, fleet, batches = generate_scene
    monkeypatch.setattr(emit_api, "emit_batch", _REAL_EMIT)
    first = observation(fleet.name, interrupted=True)
    second = observation(fleet.name, digest="b" * 64)
    registry_emit.run_generate_scan(paths, fleet, composition=first)
    with connect_ro(db_file(paths.root)) as conn:
        count = conn.execute("SELECT count(*) FROM registry_snapshots").fetchone()[0]
    registry_emit.run_generate_scan(paths, fleet, composition=second)
    retry = completion("retry", second, fleet=fleet.name)
    assert _REAL_EMIT(paths.root, [retry])[0].status == "committed"
    assert _REAL_EMIT(paths.root, [retry])[0].status == "duplicate"
    with connect_ro(db_file(paths.root)) as conn:
        assert conn.execute("SELECT count(*) FROM registry_snapshots").fetchone()[0] == count
        rows = read_compositions(conn, fleet.name)["observations"]
    assert len(rows) == 3
    assert rows[-1]["composition"]["git"]["interrupted"] is True
    assert rows[0]["composition"]["git"]["interrupted"] is False
    assert rows[0]["composition"]["files"] != rows[-1]["composition"]["files"]


def test_reader_bounds_recording_order_and_discloses_old_and_unobserved_history(tmp_path):
    from claudlobby.plane.db import connect_ro, db_file
    from claudlobby.plane.composition_history import read_compositions
    root = tmp_path / "root"
    # Newer-recorded observation has an older clock: order is recording, not time.
    _REAL_EMIT(root, [completion("old-no-field"),
                     completion("clean", observation(), complete=False, time="2026-09-24T00:00:00+00:00"),
                     completion("foreign", observation("other"), fleet="other")])
    with connect_ro(db_file(root)) as conn:
        query = []
        conn.set_trace_callback(query.append)
        bounded = read_compositions(conn, "test-fleet", limit=1)
        assert [r["scan_id"] for r in bounded["observations"]] == ["clean"]
        assert any("LIMIT 1" in sql for sql in query)
        all_rows = read_compositions(conn, "test-fleet", limit=2)
    assert [r["status"] for r in all_rows["observations"]] == ["recorded", "not_recorded"]
    assert all_rows["observations"][0]["registry_complete"] is False
    assert all_rows["coverage"]["missing_composition"] == 1
    assert all_rows["coverage"]["complete_attempt_history"] is False
    assert "repaired before any generate observed it" in all_rows["coverage"]["note"]
    assert all(r.get("composition") is None or not r["composition"]["git"]["interrupted"]
               for r in all_rows["observations"])  # Never invent an earlier wedge.


@pytest.mark.parametrize("limit", [0, -1, 1001, True])
def test_bad_limits_refuse_before_any_query(limit):
    from claudlobby.plane.composition_history import read_compositions
    with pytest.raises(ValueError, match="between 1 and 1000"):
        read_compositions(None, "test-fleet", limit=limit)


def test_typed_contract_backcompat_bounds_and_corruption_disclosure(tmp_path):
    from claudlobby.plane.contracts import ContractViolation, validate_request
    from claudlobby.plane.db import connect, connect_ro, db_file
    from claudlobby.plane.composition_history import read_compositions
    validate_request(completion("legacy"))
    bad = observation()
    bad["git"]["interrupted"] = "false"
    with pytest.raises(ContractViolation):
        validate_request(completion("bad-type", bad))
    too_big = observation()
    too_big["files"]["fleet.yaml"]["path"] = "x" * 65536
    with pytest.raises(ContractViolation, match="64 KiB"):
        validate_request(completion("oversize", too_big))
    with pytest.raises(ContractViolation, match="match scan scope"):
        validate_request(completion("wrong-fleet", observation("foreign")))
    root = tmp_path / "root"
    _REAL_EMIT(root, [completion("broken", observation())])
    with connect(db_file(root)) as conn:
        conn.execute("UPDATE events SET detail='broken-json' WHERE event='scan_completed'")
    with connect_ro(db_file(root)) as conn:
        report = read_compositions(conn, "test-fleet")
    assert report["observations"][0]["status"] == "unreadable"
    assert report["coverage"]["unreadable"] == 1


def test_cli_json_and_missing_database_are_read_only(tmp_path, capsys):
    from claudlobby.__main__ import main
    missing = tmp_path / "missing-root"
    assert main(["--root", str(missing), "plane", "registry", "--compositions", "test-fleet", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["available"] is False and result["observations"] == []
    assert not missing.exists()
    root = tmp_path / "root"
    _REAL_EMIT(root, [completion("one", observation())])
    assert main(["--root", str(root), "plane", "registry", "--compositions", "test-fleet", "--limit", "1", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["available"] and len(result["observations"]) == 1
    assert result["observations"][0]["scan_id"] == "one"


@pytest.mark.parametrize("reason", ["disabled", "failed", "aborted"])
def test_unrecorded_generates_are_not_reported_as_history(generate_scene, monkeypatch, caplog, reason):
    paths, fleet, batches = generate_scene
    if reason == "disabled":
        monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    elif reason == "failed":
        def fail(*args, **kwargs):
            raise OSError("synthetic transport failure")
        monkeypatch.setattr(emit_api, "emit_batch", fail)
    else:
        def fail(*args, **kwargs):
            raise ValueError("synthetic compose failure")
        monkeypatch.setattr(core, "compose_fleet", fail)
    if reason == "aborted":
        with pytest.raises(ValueError, match="synthetic compose failure"):
            core.cmd_generate(SimpleNamespace(bot=None, strict=False))
    else:
        assert core.cmd_generate(SimpleNamespace(bot=None, strict=False)) == 0
        assert ("not recorded" if reason == "disabled" else "scan failed") in caplog.text
    assert batches == []
    assert not (paths.root / "state/plane/plane.db").exists()


def test_single_bot_observation_does_not_replace_fleet_snapshot(generate_scene):
    paths, fleet, batches = generate_scene
    sidecar = paths.runtime / "composed.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    before = '{"fleet":"prior-full-generate"}'
    sidecar.write_text(before)
    assert core.cmd_generate(SimpleNamespace(bot="lead", strict=False)) == 0
    assert sidecar.read_text() == before
    observation = batches[-1]["payload"]["composition"]
    assert observation["bot_ids"] == ["lead"]
