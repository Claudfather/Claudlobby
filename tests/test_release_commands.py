"""Real CLI dispatch for preparation: private state, no native/service calls."""

import builtins
import json
from pathlib import Path

import pytest

from claudlobby.__main__ import main
from claudlobby import activation_state, composer, config, context, freshbox, migration_apply, resources
from claudlobby.commands import releases as commands
from claudlobby.config_plan import ConfigPlanBuilder, read_plan
from claudlobby.migration_plan import build_migration_manifest
from claudlobby.plane.migrations import SCHEMA_USER_VERSION
from tests.test_config_staging import staging_case, _fleet, _tree
from tests.test_migration_plan import releases, _database, _snapshot
from tests.test_releases import installed, r


def _call(capsys, argv, expected=0):
    assert main(argv) == expected
    capture = capsys.readouterr()
    lines = capture.out.splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert set(result) == {"schema_version", "ok", "command", "request_id", "release_id", "data", "error"}
    assert result["schema_version"] == 1 and result["ok"] is (expected == 0)
    if expected:
        assert set(result["error"]) == {"code", "message", "retryable", "hint"}
    else:
        assert result["error"] is None
    return result, capture


def test_json_syntax_and_scope_failures_preserve_legacy_help_and_hide_values(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("CLAUDLOBBY_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    for argv, command in ((["config", "plan", "--json"], "config.plan"),
                          (["host", "repos", "pull", "--json"], "host.repos.pull"),
                          (["host", "releases", "--json", "--unknown", "SECRET-value"], "host.releases")):
        with pytest.raises(SystemExit) as exit:
            main(argv)
        assert exit.value.code == 2
        capture = capsys.readouterr()
        body = json.loads(capture.out)
        assert body["command"] == command and body["error"]["code"] == "invalid_argument"
        assert "SECRET-value" not in capture.out + capture.err
    missing, _ = _call(capsys, ["host", "releases", "--json"], 2)
    assert "explicit host data root" in missing["error"]["message"]
    for scope in (["--fleet", "misleading"], ["--seed"]):
        result, _ = _call(capsys, ["--root", str(tmp_path), *scope, "host", "releases", "--json"], 2)
        assert result["error"]["code"] == "invalid_argument"
    with pytest.raises(SystemExit) as exit:
        main(["config", "validate", "--json", "--unknown"])
    assert exit.value.code == 2
    assert json.loads(capsys.readouterr().out)["command"] == "config.validate"
    with pytest.raises(SystemExit) as exit:
        main(["config", "plan", "--help"])
    assert exit.value.code == 0 and "--fleet-path" in capsys.readouterr().out

    def broken(*_):
        raise RuntimeError("SECRET-value")

    monkeypatch.setattr(commands, "_host_releases", broken)
    failure, capture = _call(capsys, ["--root", str(tmp_path), "host", "releases", "--json"], 1)
    assert failure["error"]["code"] == "internal_error"
    assert "SECRET-value" not in capture.out + capture.err


def test_host_release_diagnosis_works_without_application_dependencies(installed, capsys, monkeypatch):
    root, inputs, paths, _, directory = installed
    release = r.seal_release(root, inputs, paths)
    before = _snapshot(root)
    original = builtins.__import__

    def without_optional(name, *args, **kwargs):
        if name.split(".")[0] in {"pydantic", "yaml", "jinja2", "claudron", "sqlite3"}:
            raise ImportError("optional application dependency unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_optional)
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    found, _ = _call(capsys, ["host", "releases", "--json"])
    assert found["request_id"] is None and found["release_id"] is None
    assert found["data"]["items"][0]["release_id"] == release.release_id
    assert found["data"]["items"][0]["verification"] == "verified"
    assert found["data"]["next_cursor"] is None
    assert _snapshot(root) == before
    # Journal-only fixture: the former selection is active, but a subsequent
    # interrupted host activation blocks mutation even while its bytes verify.
    plan = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, (), effects={}).seal()
    with activation_state.locked_activation(root) as store:
        store.prepare("previous", plan, recovery_release_id=release.release_id, enrollment_digest="1" * 64)
        for step in activation_state.STEPS:
            store.begin("previous", step)
            if step == "selection_switched":
                store.select("previous")
            else:
                store.complete("previous", step, evidence_digest="2" * 64)
        store.prepare("interrupted", plan, recovery_release_id=release.release_id, enrollment_digest="1" * 64)
        store.begin("interrupted", "producers_paused")
    before = _snapshot(root)
    interrupted, _ = _call(capsys, ["host", "releases", "--json"], 4)
    assert interrupted["data"]["selected_activation"]["status"] == "active"
    assert interrupted["data"]["unfinished_activations"] == [{
        "activation_id": "interrupted", "status": "activating",
        "pending_step": "producers_paused", "verification": "verified"}]
    assert main(["host", "releases"]) == 4
    human = capsys.readouterr().err
    assert "Selected activation previous: active" in human
    assert "interrupted (activating, pending producers_paused)" in human
    assert _snapshot(root) == before
    (root / "state/activations/interrupted/activation.json").write_text("unreadable record")
    invalid, _ = _call(capsys, ["host", "releases", "--json"], 4)
    assert invalid["data"]["selected_activation"]["status"] == "active"
    assert invalid["data"]["activation_errors"][0]["activation_id"] == "interrupted"
    assert invalid["data"]["items"][0]["verification"] == "verified"
    (directory / paths.native / "keepalive.sh").write_text("changed")
    failed, _ = _call(capsys, ["host", "releases", "--json"], 4)
    assert failed["data"]["items"][0]["verification"] == "failed"
    # Explicit root is authoritative over the environment, never cwd or a fleet.
    empty_root = root.parent / "empty"
    empty_root.mkdir()
    empty, _ = _call(capsys, ["--root", str(empty_root), "host", "releases", "--json"])
    assert empty["data"]["items"] == [] and not list(empty_root.iterdir())


def test_config_plan_covers_declared_and_explicit_fleets_and_diff_never_dumps_bytes(staging_case, capsys, monkeypatch):
    case = staging_case
    monkeypatch.setattr(resources, "get_resources", lambda: case.package)
    _fleet(case.root, case.root / "local/system/local-fleet", "local-fleet", case.package)
    external = case.root.parent / "external/external-fleet"
    _fleet(case.root, external, "external-fleet", case.package)
    (case.root / ".env").write_text("STAGE_TOKEN=SECRET-config-value\nPLANE_EMIT_DISABLED=1\n")
    before = _tree(case.root, excluding=case.root / "state/config-plans")
    external_before, home_before = _tree(external), _tree(Path.home())
    argv = ["--root", str(case.root), "config", "plan", "--release", case.release.release_id,
            "--fleet-path", str(external / "fleet.yaml"), "--json"]
    planned, capture = _call(capsys, argv)
    assert planned["request_id"] and planned["command"] == "config.plan"
    assert planned["data"]["fleets"] == ["external-fleet", "local-fleet", "primary"]
    assert _tree(case.root, excluding=case.root / "state/config-plans") == before
    assert _tree(external) == external_before and _tree(Path.home()) == home_before
    assert "SECRET-config-value" not in capture.out + capture.err
    plan_id = planned["data"]["plan_id"]
    diff, capture = _call(capsys, ["--root", str(case.root), "config", "diff", plan_id, "--json"])
    assert diff["request_id"] is None and diff["data"] == planned["data"]
    assert "SECRET-config-value" not in capture.out + capture.err
    assert all(set(change) == {"path", "before", "after"} for change in diff["data"]["changes"])
    assert all(set(change["after"]) == {"kind", "state_sha256"} for change in diff["data"]["changes"])
    # Repeating the same source location is harmless; different sources may
    # never claim the same fleet name and redirect the planned runtime.
    repeated, _ = _call(capsys, argv[:-1] + ["--fleet-path", str(external), "--json"])
    assert repeated["data"]["fleets"] == planned["data"]["fleets"]
    duplicate = case.root.parent / "duplicate/external-fleet"
    _fleet(case.root, duplicate, "external-fleet", case.package)
    _call(capsys, argv[:-1] + ["--fleet-path", str(duplicate), "--json"], 4)
    plan = read_plan(case.root, plan_id)
    secret_target = case.root / "runtime/secret.conf"
    builder = ConfigPlanBuilder(case.root, plan.release_id, plan.release_seal, plan.fleets, effects={})
    builder.file(secret_target, b"ACTUAL_STAGED_SECRET=do-not-print\n")
    secret_plan = builder.seal()
    _, capture = _call(capsys, ["--root", str(case.root), "config", "diff", secret_plan.plan_id, "--json"])
    assert "ACTUAL_STAGED_SECRET" not in capture.out + capture.err and not secret_target.exists()


def test_config_diff_current_reports_drift_without_values_or_retired_routes(staging_case, capsys, monkeypatch):
    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)
    fleet, _ = config.load_fleet(case.paths.fleet_yaml)
    bot = "primary-manager"
    composer.compose_bot(fleet.bots[bot], fleet, case.paths)
    conf = case.paths.bot_runtime(bot) / "bot.conf"
    conf.write_text(conf.read_text() + "\nexport PRIVATE_MARKER=SECRET-current-value\n")

    current, capture = _call(capsys, ["--root", str(case.root), "config", "diff",
                                       "--bot", bot, "--json"])
    assert current["command"] == "config.diff"
    assert current["data"]["mode"] == "current"
    assert current["data"]["bot"] == bot
    assert any("bot.conf drift" in line for line in current["data"]["drift"])
    assert "SECRET-current-value" not in capture.out + capture.err
    assert "PRIVATE_MARKER" not in capture.out + capture.err

    for retired in ("diff", "promote"):
        with pytest.raises(SystemExit) as exit:
            main(["--root", str(case.root), retired])
        assert exit.value.code == 2
        capsys.readouterr()


def test_config_validate_runtime_keeps_audit_severities_and_hides_details(fleet_dir, capsys, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
    monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")

    def call(*options, expected=0):
        return _call(capsys, ["--root", str(fleet_dir), "config", "validate",
                              "--runtime", *options, "--json"], expected)[0]

    monkeypatch.setattr(freshbox, "audit_fleet", lambda *_args, **_kwargs: [
        freshbox.Finding("lead", "external_ref", freshbox.INFO, "SECRET-info-value")])
    info = call("--strict")
    assert info["data"]["mode"] == "runtime" and info["data"]["info_count"] == 1
    assert "SECRET-info-value" not in json.dumps(info)

    monkeypatch.setattr(freshbox, "audit_fleet", lambda *_args, **_kwargs: [
        freshbox.Finding("lead", "unused_declaration", freshbox.WARN, "SECRET-warn-value")])
    assert call()["data"]["warning_count"] == 1
    blocked = call("--strict", expected=4)
    assert blocked["error"]["code"] == "conflict"
    assert "SECRET-warn-value" not in json.dumps(blocked)

    monkeypatch.setattr(freshbox, "audit_fleet", lambda *_args, **_kwargs: [
        freshbox.Finding("lead", "orphan_grant", freshbox.FAIL, "SECRET-fail-value")])
    assert call(expected=4)["data"]["fail_count"] == 1
    selected = []
    monkeypatch.setattr(freshbox, "audit_bot", lambda bot, *_args, **_kwargs:
                        selected.append(bot.bot_id) or [])
    assert call("--bot", "lead")["data"]["bot"] == "lead"
    assert selected == ["lead"]
    assert call("--bot", "missing", expected=3)["error"]["code"] == "not_found"
    assert call("--warn-baseline", str(fleet_dir / "warnings.json"), expected=2)["error"]["code"] == "invalid_argument"
    with pytest.raises(SystemExit) as exit:
        main(["--root", str(fleet_dir), "freshbox"])
    assert exit.value.code == 2
    capsys.readouterr()


def test_migration_cli_preview_is_read_only_and_status_separates_recorded_progress(releases, capsys):
    root, _, release = releases
    _database(root).close()
    argv = ["--root", str(root), "migration", "plan", "--source-release", release.release_id,
            "--target-release", release.release_id, "--json"]
    before = _snapshot(root)
    result, _ = _call(capsys, argv)
    assert result["request_id"] is None and result["data"]["manifest"]["preview_only"]
    assert result["data"]["manifest"]["database"]["user_version"] == 1
    assert _snapshot(root) == before
    status_argv = ["--root", str(root), "migration", "status", "--json"]
    old, _ = _call(capsys, status_argv, 4)
    assert old["data"]["database"]["user_version"] == 1
    assert old["data"]["items"] == [] and _snapshot(root) == before

    # Fixture setup only: exercise the real journal owners to supply completed
    # migration evidence, without service calls or a public apply route.
    plan = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, (), effects={}).seal()
    manifest = build_migration_manifest(root, release, release)
    with activation_state.locked_activation(root) as store:
        store.prepare("upgrade", plan, recovery_release_id=release.release_id, enrollment_digest="1" * 64)
        for step in activation_state.STEPS[:activation_state.STEPS.index("backup_saved")]:
            store.begin("upgrade", step)
            store.complete("upgrade", step, evidence_digest=(
                manifest.manifest_id[2:] if step == "queues_classified" else "2" * 64))
        migration_apply.apply_migration(store, "upgrade", manifest)
    before = _snapshot(root)
    complete, _ = _call(capsys, status_argv)
    assert complete["data"]["database"]["user_version"] == SCHEMA_USER_VERSION
    assert complete["data"]["items"][0]["recorded"]["result"]["user_version"] == SCHEMA_USER_VERSION
    assert complete["data"]["items"][0]["migration_step_completed"]
    assert complete["data"]["items"][0]["activation_status"] == "activating"
    after = _snapshot(root)
    assert all(after[path] == content for path, content in before.items())
    # SQLite mode=ro can initialize its empty WAL and shared-memory index when
    # the last writer left neither. No existing WAL bytes may be waived here.
    extra = set(after) - set(before)
    assert extra <= {"state/plane/plane.db-wal", "state/plane/plane.db-shm"}
    if "state/plane/plane.db-wal" in extra:
        assert after["state/plane/plane.db-wal"] == b""
