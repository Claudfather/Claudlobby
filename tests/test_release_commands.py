"""Real CLI dispatch for preparation: private state, no native/service calls."""

import builtins
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.__main__ import main
from claudlobby import activation_state, config_install, context, freshbox, migration_apply, resources
from claudlobby import releases as release_store
from claudlobby.commands import releases as commands
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.migration_plan import build_migration_manifest
from claudlobby.plane.migrations import SCHEMA_USER_VERSION
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
    for argv, command in ((["host", "repos", "pull", "--json"], "host.repos.pull"),
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
        main(["config", "diff", "--help"])
    assert exit.value.code == 0 and "FINGERPRINT" in capsys.readouterr().out

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


def test_config_validate_runtime_keeps_audit_severities_and_hides_details(fleet_dir, capsys, monkeypatch):
    from tests.package_fixtures import source_package

    monkeypatch.setattr(context, "get_resources", source_package)
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
    monkeypatch.setattr(freshbox, "audit_fleet", lambda *_args, **_kwargs: [
        freshbox.Finding("lead", "orphan_unit", freshbox.WARN,
                         "worker.plist — stale supervision unit; SECRET-path")])
    orphan = call()
    assert orphan["data"]["findings"][0]["unit"] == "worker.plist"
    assert "reap-orphans" in orphan["data"]["findings"][0]["remedy"]
    assert "SECRET-path" not in json.dumps(orphan)
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


def test_config_reads_report_unbuilt_resources_as_unavailable(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(context, "get_resources", lambda: (_ for _ in ()).throw(
        RuntimeError("private build detail")))
    for command in (["config", "validate"], ["config", "explain", "fleet.name"]):
        result, capture = _call(capsys, ["--root", str(tmp_path), *command, "--json"], 6)
        assert result["error"]["code"] == "unavailable"
        assert "sealed release" in result["error"]["hint"]
        assert "private build detail" not in capture.out + capture.err


def test_converter_failure_preserves_cron_backup_without_provider_stderr(monkeypatch):
    import logging
    from types import SimpleNamespace
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import migration_converters

    def owner(_args):
        logging.getLogger("claudlobby").error(
            "your old crontab is preserved as /tmp/crontab-backup-123.txt")
        logging.getLogger("claudlobby").error("`crontab -` install failed: SECRET-provider-error")
        return 4

    monkeypatch.setattr(migration_converters, "import_module", lambda *_: SimpleNamespace(cmd_cron_migrate=owner))
    args = SimpleNamespace(migration_command="cron", archive=False, apply=True)
    with pytest.raises(CommandFailure) as failure:
        migration_converters.dispatch(args)
    assert failure.value.data["backup_path"] == "/tmp/crontab-backup-123.txt"
    assert "SECRET-provider-error" not in str(failure.value.data) + str(failure.value.error)


def test_converter_copy_failure_keeps_item_status_without_exception_text(monkeypatch):
    import logging
    from types import SimpleNamespace
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import migration_converters

    def owner(_args):
        logging.getLogger("claudlobby").error("FAILED  /private/old/notes.txt: SECRET-copy-error")
        return 1

    monkeypatch.setattr(migration_converters, "import_module", lambda *_: SimpleNamespace(cmd_data_migrate=owner))
    with pytest.raises(CommandFailure) as failure:
        migration_converters.dispatch(SimpleNamespace(migration_command="data", apply=True))
    assert failure.value.data["diagnostics"] == [
        {"status": "failed", "source": "/private/old/notes.txt"}]
    assert "SECRET-copy-error" not in str(failure.value.data) + str(failure.value.error)


def test_migration_status_reads_historical_progress_without_execution(releases, capsys):
    from claudlobby import activation_enrollment, activation_units, config_install

    root, _, release = releases
    _database(root).close()
    before = _snapshot(root)
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
        for name, owner, phase in (
            (activation_units.journal_id("upgrade", "producers"), "activation-units-v1", "producers"),
            (activation_enrollment.journal_id("upgrade", "directories"),
             "activation-enrollment-v1", "directories"),
        ):
            journal_plan = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, (),
                effects={"owner": owner, "activation_id": "upgrade", "phase": phase}).seal()
            config_install.prepare_config(journal_plan, name)
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
    broken = root / "state/activations" / ("units-" + "f" * 64)
    broken.mkdir()
    blocked, _ = _call(capsys, status_argv, 4)
    assert {item["activation_id"] for item in blocked["data"]["items"]} == {"upgrade", broken.name}
    assert blocked["data"]["blockers"] == [
        f"activation migration evidence cannot be verified: {broken.name}"]


@pytest.mark.parametrize('argv', [
    ['host', 'activate'], ['host', 'repair-start'], ['host', 'repair-after-reset'],
    ['host', 'abort-adoption'], ['host', 'abort-upgrade'],
    ['config', 'plan', '--release', 'r-' + 'a' * 64],
    ['migration', 'plan', '--source-release', 'r-' + 'a' * 64],
])
def test_removed_sealed_write_routes_refuse_at_parser(tmp_path, capsys, argv):
    with pytest.raises(SystemExit) as refused:
        main(['--root', str(tmp_path), *argv])
    assert refused.value.code == 2
    capsys.readouterr()
    assert list(tmp_path.iterdir()) == []


def test_committed_git_diff_uses_owned_snapshot_and_hides_authored_values(tmp_path, monkeypatch):
    from claudlobby import active_config
    from claudlobby.config_plan import path_state
    from claudlobby.resources import get_resources
    from claudlobby.installation import Installation
    root = tmp_path / 'data'
    root.mkdir()
    target = root / 'generated.conf'
    target.write_text('SECRET actual bytes')
    directory = root / 'mutable'
    directory.mkdir()
    (directory / 'private').write_text('SECRET mutable data')
    installation = Installation(tmp_path / 'installation', root)
    package = SimpleNamespace(installation=installation, source_revision='a' * 40)
    snapshot = {'revision': package.source_revision, 'fingerprint': 'b' * 64,
                'owned_outputs': [{'target': str(target), 'state': {'kind': 'file', 'mode': 0o600, 'sha256': 'c' * 64}},
                                  {'target': str(directory), 'state': {'kind': 'directory', 'mode': directory.stat().st_mode & 0o777}}]}
    monkeypatch.setattr(resources, 'get_resources', lambda: package)
    monkeypatch.setattr(active_config, 'read_current_config', lambda *_a, **_k: snapshot)
    checks = []
    monkeypatch.setattr(active_config, 'runtime_read_stamp', lambda *_a, **_k:
                        SimpleNamespace(check=lambda *_a, **_k: checks.append('checked')))
    result = commands._config_current_drift(SimpleNamespace(root=root, fleet=None, bot=None, seed=False))
    assert result.release_id is None and result.data['config_fingerprint'] == snapshot['fingerprint']
    changes = {item['path']: item for item in result.data['changes']}
    assert changes[str(target)]['change'] == 'content'
    assert changes[str(directory)]['change'] == 'unchanged'
    assert checks == ['checked'] and 'SECRET' not in json.dumps(result.data)
    assert (directory / 'private').read_text() == 'SECRET mutable data'


def test_pending_git_diff_reads_canonical_render_without_new_planning(tmp_path, monkeypatch):
    from claudlobby import config_plan
    from claudlobby.installation import Installation
    installation = Installation(tmp_path / 'installation', tmp_path / 'data')
    package = SimpleNamespace(installation=installation)
    rendered = SimpleNamespace(fingerprint='a' * 64, revision='b' * 40, fleets=('example',), changes=(), effects={})
    calls = []
    monkeypatch.setattr(resources, 'get_resources', lambda: package)
    monkeypatch.setattr(config_plan, 'read_rendered', lambda root, fingerprint:
                        calls.append((root, fingerprint)) or rendered)
    result = commands._config_diff(SimpleNamespace(bot=None, fingerprint=rendered.fingerprint), installation.data_root)
    assert calls == [(installation.data_root, rendered.fingerprint)]
    assert result.release_id is None and result.data['source_revision'] == rendered.revision
    assert 'plan_id' not in result.data and 'release_seal' not in result.data
