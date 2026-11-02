"""Read orientation from real composition without claiming live enforcement."""

import builtins
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3

import pytest
import yaml

from claudlobby import composer, context
from claudlobby.__main__ import main
from claudlobby.command_result import empty_fleet_conflict
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
    assert shown["registry"]["entity_count"] is None
    assert shown["registry"]["entity_counts_by_type"] is None
    assert "items" not in shown["registry"]

    listed = _call(capsys, case.root, "bot", "list")
    assert [(item["bot_id"], item["role"]) for item in listed["items"]] == [
        ("primary-manager", "manager"), ("primary-worker", "worker")]
    detail = _call(capsys, case.root, "bot", "show", "primary-worker")["bot"]
    assert detail["effective_configuration"]["equipment"]["skills"] == ["fleet-ops", "stage-skill"]
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
    # A YAML syntax error names its position, never the quoted source line.
    case.paths.fleet_yaml.write_text(case.paths.fleet_yaml.read_text()
                                     + "broken: [SECRET-orientation-yaml\n")
    assert main(["--root", str(case.root), "--json", "context", "show"]) == 4
    captured = capsys.readouterr()
    assert "SECRET-orientation" not in captured.out + captured.err
    assert "line " in json.loads(captured.out)["error"]["hint"]


_ASKED = "Bash(echo SECRET-orientation-ask)"
_ASKED_DIGEST = hashlib.sha256(_ASKED.encode()).hexdigest()


def _stage_skill_asking(case, *rules):
    """Rewrite the staged skill so that it declares ``rules`` as ask_grants (#2476)."""
    asks = ("ask_grants:\n" + "".join(f'  - "{rule}"\n' for rule in rules)) if rules else ""
    _write(case.root / "library/skills/stage-skill/SKILL.md",
           f"---\nname: stage-skill\n{asks}---\n# Stage skill\nUse the attached instructions.\n")


def _worker_composed(case, capsys):
    """Compose the worker's settings as the first test does, then read its `bot capabilities`."""
    loaded = context.load_context(case.paths)
    worker = loaded.fleet.bots["primary-worker"]
    rendered = composer.render_bot_files(worker, loaded.fleet, case.paths,
                                         boot_delay_s=0, cascade={})
    _write(case.paths.bot_runtime("primary-worker") / ".claude/settings.local.json",
           rendered[".claude/settings.local.json"].content)
    return _call(capsys, case.root, "bot", "capabilities",
                 "primary-worker")["bot"]["permissions"]["composed"]


def test_capabilities_lists_the_composed_ask_rules_as_digests_beside_allow_and_deny(
        staging_case, monkeypatch, capsys):
    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)
    _stage_skill_asking(case, _ASKED)

    composed = _worker_composed(case, capsys)

    assert composed["ask"] == [{"tool": "Bash", "sha256": _ASKED_DIGEST}]
    assert _ASKED_DIGEST not in [item["sha256"] for item in composed["allow"]]
    assert "SECRET-orientation" not in json.dumps(composed)  # the arguments stay withheld

    # The text view counts the ask rules and names their tools, as it does the deny rules.
    assert main(["--root", str(case.root), "bot", "capabilities", "primary-worker"]) == 0
    text = capsys.readouterr().out
    assert f"allow rules, 1 ask rules, {len(composed['deny'])} deny rules." in text
    assert "Composed ask tools: Bash" in text
    assert "SECRET-orientation" not in text


def test_capabilities_ask_is_empty_when_the_settings_hold_no_ask_key(staging_case, monkeypatch, capsys):
    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)

    composed = _worker_composed(case, capsys)

    settings = json.loads((case.paths.bot_runtime("primary-worker")
                           / ".claude/settings.local.json").read_text())
    assert "ask" not in settings["permissions"]
    assert composed["ask"] == []
    assert composed["allow"] and composed["deny"]  # a composed file, not an absent one


def test_capabilities_does_not_flag_a_fleet_grant_of_a_rule_a_skill_asks_about(
        staging_case, monkeypatch, capsys):
    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)
    doc = yaml.safe_load(case.paths.fleet_yaml.read_text())
    doc["fleet"]["bots"]["primary-worker"]["tool_permissions"]["allow"].append(_ASKED)
    case.paths.fleet_yaml.write_text(yaml.safe_dump(doc))

    def finding_kinds(composed):
        return [item["kind"] for item in composed["provenance_findings"]
                if item["rule"]["sha256"] == _ASKED_DIGEST]

    # No skill asks about the rule: the fleet's grant is drift, as before.
    _stage_skill_asking(case)
    composed = _worker_composed(case, capsys)
    assert finding_kinds(composed) == ["unsourced_grant"]
    # A skill asks about it: the fleet's grant is how it is granted. The rule is
    # allowed, no longer asked, and nothing is flagged.
    _stage_skill_asking(case, _ASKED)
    composed = _worker_composed(case, capsys)
    assert finding_kinds(composed) == []
    assert composed["ask"] == []
    assert _ASKED_DIGEST in [item["sha256"] for item in composed["allow"]]


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
    from claudlobby.plane import registry_read
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
    assert shown["last_scan_id"] == "scan-2"
    assert shown["last_scan_scope"] == "host+shared+fleet:primary"
    assert shown["last_scan_complete"] is False
    assert shown["entity_count"] == 0 and shown["entity_counts_by_type"] == {}
    assert "items" not in shown

    rows = ([{"entity_type": "bot", "entity_alias": f"SECRET-orientation-{n}"}
             for n in range(100)] + [{"entity_type": "fleet", "entity_alias": "primary"}])
    monkeypatch.setattr(registry_read, "current_entities", lambda *_args, **_kwargs: rows)
    summary = _call(capsys, case.root, "context", "show")["context"]["registry"]
    assert summary["entity_count"] == 101
    assert summary["entity_counts_by_type"] == {"bot": 100, "fleet": 1}
    assert "items" not in summary and "SECRET-orientation" not in json.dumps(summary)

    def unreadable(*_args, **_kwargs):
        raise ValueError("cannot read registry")

    monkeypatch.setattr(registry_read, "current_entities", unreadable)
    unknown = _call(capsys, case.root, "context", "show")["context"]["registry"]
    assert unknown["state"] == "unreadable"
    assert unknown["entity_count"] is None and unknown["entity_counts_by_type"] is None


_REFUSING = (("config", "validate"), ("fleet", "status"),
             ("context", "show", "--bot", "secondary-manager"))


@pytest.mark.parametrize("content, says", [
    (b"", "(0 bytes)"),
    (b"\n \t\n", "holds no YAML document"),
    (b"# retired fleet\n", "holds no YAML document"),
], ids=["zero-bytes", "whitespace-only", "comment-only"])
def test_empty_fleet_manifest_is_named_by_every_refusing_command(staging_case, monkeypatch, capsys,
                                                                 content, says):
    # #2267: an empty manifest names the file and why, at the same rc, and is left untouched.
    monkeypatch.setattr(context, "get_resources", lambda: staging_case.package)
    manifest = _fleet(staging_case.root, staging_case.root / "local/secondary", "secondary",
                      staging_case.package).fleet_yaml
    manifest.write_bytes(content)
    for operation in _REFUSING:
        assert main(["--root", str(staging_case.root), "--json", "--fleet", "secondary", *operation]) == 4
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "conflict"
        assert str(manifest) in error["message"] and says in error["message"]
        if content:
            assert f"({len(content)} bytes" in error["message"]
        assert "last good copy" in error["hint"]
        assert (f"git -C {shlex.quote(str(manifest.parent))} log -- fleet.yaml"
                in error["hint"])
    assert manifest.read_bytes() == content
    # A root with a space stays one argument in the suggested command.
    spaced = Path("/tmp/data team/local/secondary/fleet.yaml")
    assert "git -C '/tmp/data team/local/secondary' log -- fleet.yaml" in str(
        empty_fleet_conflict(spaced, len(content)).error.hint)
    odd = Path("/tmp/data/local/secondary/fleet copy.yaml")
    assert "log -- 'fleet copy.yaml'" in str(empty_fleet_conflict(odd, 0).error.hint)


@pytest.mark.parametrize("content", [
    b"fleets:\n  token: SECRET-orientation-planted\n",
    b"--- # SECRET-orientation-planted\n",
    b"fleet:\n  name: \xff\xfeSECRET-orientation-planted\n",
], ids=["malformed-mapping", "null-document", "invalid-encoding"])
def test_non_empty_invalid_manifest_keeps_the_generic_refusal(staging_case, monkeypatch, capsys, content):
    monkeypatch.setattr(context, "get_resources", lambda: staging_case.package)
    manifest = _fleet(staging_case.root, staging_case.root / "local/secondary", "secondary",
                      staging_case.package).fleet_yaml
    manifest.write_bytes(content)
    generic = {"config": "selected fleet configuration is invalid",
               "fleet": "context selectors or fleet configuration are invalid",
               "context": "context selectors or fleet configuration are invalid"}
    for operation in _REFUSING:
        assert main(["--root", str(staging_case.root), "--json", "--fleet", "secondary", *operation]) == 4
        captured = capsys.readouterr()
        assert "SECRET-orientation" not in captured.out + captured.err
        error = json.loads(captured.out)["error"]
        assert error["code"] == "conflict" and error["message"] == generic[operation[0]]
        assert "last good copy" not in str(error["hint"])
    assert manifest.read_bytes() == content
