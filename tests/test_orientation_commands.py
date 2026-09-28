"""Read orientation from real composition without claiming live enforcement."""

import builtins
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
import yaml

from claudlobby import composer, context
from claudlobby.__main__ import main
from tests.test_config_staging import staging_case, _fleet, _tree, _write


def _call(capsys, root, *operation, expected=0):
    assert main(["--root", str(root), "--json", *operation]) == expected
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert set(result) == {"schema_version", "ok", "command", "request_id", "release_id", "data", "error"}
    assert result["ok"] is (expected == 0) and result["request_id"] is None
    assert "SECRET-orientation" not in captured.out + captured.err
    return result["data"]


def test_composed_manager_worker_project_orientation_never_claims_running_permissions(staging_case, monkeypatch, capsys):
    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)
    doc = yaml.safe_load(case.paths.fleet_yaml.read_text())
    worker = doc["fleet"]["bots"]["primary-worker"]
    worker["tool_permissions"]["allow"].append("Bash(echo SECRET-orientation-grant)")
    worker["env"] = {"PRIVATE_VALUE": "SECRET-orientation-env"}
    case.paths.fleet_yaml.write_text(yaml.safe_dump(doc))
    _write(case.root / "projects.yaml", """projects:
  delivery:
    title: Delivery
    repos: [example/delivery]
    mission_file: missions/delivery.md
    validation: {tier: preview}
""")
    _write(case.root / "missions/delivery.md", "# Mission\nShip a reviewed change.\n")
    loaded = context.load_context(case.paths)
    for bot in loaded.fleet.bots.values():
        # Actual renderer, copied into this private test's composed destinations;
        # no registration, enrollment, daemon, OS startup or agent process.
        rendered = composer.render_bot_files(bot, loaded.fleet, case.paths,
                                             boot_delay_s=0, cascade={})
        for name in ("CLAUDE.md", "bot.conf", ".mcp.json", ".claude/settings.local.json"):
            _write(case.paths.bot_runtime(bot.bot_id) / name, rendered[name].content)
    composer.write_manifest_provenance(loaded.fleet, case.paths)
    worker_dir = case.paths.bot_runtime("primary-worker")
    _write(worker_dir / ".mcp.json", '{"mcpServers":{"private":{"env":{"TOKEN":"SECRET-orientation-mcp"}}}}')
    before = _tree(case.root)

    shown = _call(capsys, case.root, "context", "show", "--bot", "primary-worker")["context"]
    assert (shown["fleet"], shown["bot"], shown["manager"]) == ("primary", "primary-worker", "primary-manager")
    assert shown["composition"]["manifest_inputs_match"] is True
    assert shown["composition"]["equipment_freshness"] == "not_checked"
    assert shown["release"]["running_release"] == "unknown"
    assert shown["registry"]["state"] == "unavailable"

    listed = _call(capsys, case.root, "bot", "list")
    assert [(item["bot_id"], item["role"]) for item in listed["items"]] == [
        ("primary-manager", "manager"), ("primary-worker", "worker")]
    detail = _call(capsys, case.root, "bot", "show", "primary-worker")["bot"]
    assert detail["effective_configuration"]["equipment"]["skills"] == ["stage-skill", "fleet-ops"]
    capabilities = _call(capsys, case.root, "bot", "capabilities", "primary-worker")["bot"]
    assert capabilities["permissions"]["enforcement"] == "unknown"
    assert capabilities["runtime_observed"]["session"] == "not_probed"
    perms = capabilities["permissions"]["composed"]
    assert perms["state"] == "present"
    assert "Write" not in [item["tool"] for item in perms["allow"]]
    assert "Write" in [item["tool"] for item in perms["deny"]]
    assert hashlib.sha256(b"Bash(echo SECRET-orientation-grant)").hexdigest() in [item["sha256"] for item in perms["allow"]]
    assert any(item["kind"] == "unsourced_grant" for item in perms["provenance_findings"])
    fleet = _call(capsys, case.root, "fleet", "show")["fleet"]
    assert fleet["manager"] == "primary-manager" and fleet["workers"] == ["primary-worker"]
    projects = _call(capsys, case.root, "project", "list")
    project = _call(capsys, case.root, "project", "show", "delivery")["project"]
    assert projects["items"] == [project]
    assert project["mission_file"] == "missions/delivery.md"
    assert project["validation"] == {
        "tier": "preview", "source": "projects_yaml_or_loader_default",
        "source_path": str(case.paths.projects_yaml), "checks_observed": "unknown",
        "executed_by_this_command": False}
    assert _tree(case.root) == before
    assert main(["--root", str(case.root), "bot", "capabilities", "primary-worker"]) == 0
    assert "runtime permission enforcement unknown" in capsys.readouterr().out

    (worker_dir / ".claude/settings.local.json").unlink()
    absent = _call(capsys, case.root, "bot", "capabilities", "primary-worker")["bot"]
    assert absent["permissions"]["composed"]["state"] == "absent"
    assert absent["permissions"]["enforcement"] == "unknown"
    case.paths.fleet_yaml.write_text(case.paths.fleet_yaml.read_text() + "# changed after compose\n")
    drift = _call(capsys, case.root, "context", "show")["context"]["composition"]
    assert drift["manifest_inputs_match"] is False and "fleet.yaml" in drift["changed_inputs"]


def test_generated_context_defaults_are_explicit_and_never_replace_bad_identity(staging_case, monkeypatch, capsys):
    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)
    assert main(["--root", str(case.root), "--fleet", "../SECRET-orientation",
                 "context", "show", "--json"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["code"] == "invalid_argument"
    assert "SECRET-orientation" not in captured.out + captured.err
    paths = _fleet(case.root, case.root / "local/system/local-fleet", "local-fleet", case.package)
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(case.root))
    monkeypatch.setenv("FLEET_NAME", "local-fleet")
    monkeypatch.setenv("BOT_ID", "local-fleet-worker")
    monkeypatch.setenv("BOT_NAME", "unrelated-display-name")
    assert main(["context", "show", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)["data"]["context"]
    assert shown["source"]["fleet_yaml"] == str(paths.fleet_yaml)
    assert shown["bot"] == "local-fleet-worker" and shown["manager"] == "local-fleet-manager"
    monkeypatch.setenv("BOT_ID", "")
    with pytest.raises(ValueError, match="BOT_ID"):
        context.generated_selectors(include_bot=True)
    assert context.generated_selectors(fleet="primary", bot="primary-manager", include_bot=True) == (
        "primary", "primary-manager")
    monkeypatch.setenv("FLEET_NAME", "../primary")
    with pytest.raises(ValueError, match="FLEET_NAME"):
        context.generated_selectors()
    # Explicit seed selection never inherits a running bot/fleet identity.
    assert context.generated_selectors(include_bot=True, seed=True) == (None, None)
    _call(capsys, case.root, "context", "show", expected=4)
    _call(capsys, case.root, "--fleet", "primary", "context", "show", "--bot", "primary-manager")


def test_orientation_help_is_lazy_and_syntax_errors_do_not_echo_values(monkeypatch, capsys, staging_case):
    original = builtins.__import__

    def broken(name, *args, **kwargs):
        if name.split(".")[0] in {"yaml", "jinja2", "pydantic"} or name.endswith(".orientation"):
            raise ImportError("SECRET-orientation-import")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", broken)
        with pytest.raises(SystemExit) as exited:
            main(["bot", "capabilities", "--help"])
        assert exited.value.code == 0
        assert "BOT" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exited:
        main(["--json", "bot", "show", "--secret=SECRET-orientation-syntax"])
    captured = capsys.readouterr()
    assert exited.value.code == 2 and "SECRET-orientation" not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result["command"] == "bot.show" and result["error"]["code"] == "invalid_argument"
    monkeypatch.setattr(context, "get_resources", lambda: staging_case.package)
    _call(capsys, staging_case.root, "project", "show", "not-declared", expected=3)
    assert main(["--root", str(staging_case.root), "bot", "show", "not-declared", "--json"]) == 3
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "not_found" and "bot list" in error["hint"]


def test_registry_freshness_names_selected_fleet_and_incomplete_scan(staging_case, monkeypatch, capsys):
    from claudlobby.plane.registry_read import last_scan
    from tests.plane_setup import initialize_plane

    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)
    database = initialize_plane(case.root)
    # Stored observations exercise the real read owner and orientation output;
    # no emit/daemon/native process is needed to test freshness selection.
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        for sequence, fleet, complete in ((1, "primary", True), (2, "primary", False), (3, "other", True)):
            timestamp = f"2026-09-28T00:00:0{sequence}+00:00"
            scan_id = f"scan-{sequence}"
            detail = json.dumps({"scan_id": scan_id, "scope": f"host+shared+fleet:{fleet}",
                                 "counts": {}, "complete": complete})
            conn.execute("INSERT INTO ingest_ledger VALUES (?, ?, 'events', ?)",
                         (sequence, scan_id, timestamp))
            conn.execute("""INSERT INTO events
                (ingest_seq, event_id, schema_version, occurred_at, ingested_at, host_uid,
                 emitter, kind, event, subject_kind, subject_uid, detail)
                VALUES (?, ?, '1', ?, ?, 'host', 'generate', 'declaration',
                        'scan_completed', 'host', 'host', ?)""",
                         (sequence, scan_id, timestamp, timestamp, detail))
        assert last_scan(conn)["scope"] == "host+shared+fleet:other"
        assert last_scan(conn, fleet="never-scanned") is None
    shown = _call(capsys, case.root, "context", "show")["context"]["registry"]
    assert shown["state"] == "read"
    assert shown["last_scan_at"] == "2026-09-28T00:00:02+00:00"
    assert shown["last_scan_scope"] == "host+shared+fleet:primary"
    assert shown["last_scan_complete"] is False
