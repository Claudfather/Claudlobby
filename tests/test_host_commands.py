"""Public host routes with real plans/journals and explicit lifecycle collaborator."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.__main__ import main
from claudlobby import activation_state as state, resources
from claudlobby.config_install import prepare_config
from claudlobby.config_plan import ConfigPlanBuilder
from tests.test_cli_loading import _run, PARSE
from tests.test_releases import installed, r


def call(capsys, argv, expected=0):
    assert main(argv) == expected
    output = capsys.readouterr()
    assert len(output.out.splitlines()) == 1
    body = json.loads(output.out)
    assert body["ok"] is (expected == 0) and body["schema_version"] == 1
    assert set(body) == {"schema_version", "ok", "command", "request_id", "release_id", "data", "error"}
    return body


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_host_doctor_discloses_macos_reaper_native_limit(tmp_path, monkeypatch):
    from claudlobby import activation_state, context, switches
    from claudlobby.commands import _helpers, host_doctor

    paths = SimpleNamespace(root=tmp_path, fleet_yaml=tmp_path / "fleet.yaml", package=SimpleNamespace(installation=None))
    monkeypatch.setattr(context, "resolve_paths", lambda **_: paths)
    monkeypatch.setattr(context, "declared_paths", lambda *a, **k: [])
    monkeypatch.setattr(activation_state, "read_selection", lambda _: None)
    monkeypatch.setattr(_helpers, "_load_env", lambda _: None)
    monkeypatch.setattr(switches, "resolve", lambda *_: [])
    monkeypatch.setattr(switches, "format_table", lambda _: "switch table")
    monkeypatch.setattr(host_doctor.sys, "platform", "darwin")
    result = host_doctor.dispatch(SimpleNamespace(seed=False, root=tmp_path, fleet=None,
                                                  markdown=False, switches=True, delivery=False))
    assert result.data["platform_limitations"][0]["state"] == "native_reaping_disabled"
    assert "orphan-browser-reaper: OFF on macOS" in result.lines[-1]


@pytest.fixture
def candidate(installed, monkeypatch, tmp_path):
    root, inputs, paths, _, _ = installed
    release = r.seal_release(root, inputs, paths)
    plan = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, (), effects={}).seal()
    directory = tmp_path / "private-native-user"
    directory.mkdir()
    monkeypatch.setattr(resources, "selected_cli", lambda: release.cli_path)
    for name in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE"):
        monkeypatch.delenv(name, raising=False)
    return root, release, plan, directory


def test_host_help_and_parse_are_lazy_and_global_scope_order_is_explicit(tmp_path, monkeypatch, capsys):
    for route in ("status",):
        result = _run(PARSE, "host", route, "--help", tmp_path=tmp_path)
        assert result.returncode == 0, result.stderr
    for removed in ("activate", "repair-start", "repair-after-reset", "abort-adoption", "abort-upgrade"):
        result = _run(PARSE, "host", removed, "--help", tmp_path=tmp_path)
        assert result.returncode == 2
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(tmp_path))
    result = call(capsys, ["--json", "host", "status"], 2)
    assert "explicit --root" in result["error"]["message"]
    with pytest.raises(SystemExit) as exc:
        main(["host", "status", "--root", str(tmp_path), "--json"])
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["command"] == "host.status"
    assert list(tmp_path.iterdir()) == []


def test_supervision_reap_requires_an_explicit_mode_with_full_schema_command(tmp_path):
    result = _run(PARSE, "--json", "host", "supervision", "reap-orphans",
                  tmp_path=tmp_path)
    assert result.returncode == 2 and result.stderr == ""
    body = json.loads(result.stdout)
    assert body["schema_version"] == 1
    assert body["command"] == "host.supervision.reap-orphans"
    assert body["error"]["code"] == "invalid_argument"
    assert body["error"]["hint"] == "inspect claudlobby host supervision reap-orphans --help"


def test_supervision_failed_refuses_on_a_host_with_no_selected_release(tmp_path, capsys):
    body = call(capsys, ["--root", str(tmp_path), "--json", "host", "supervision", "failed"], expected=6)
    assert body["command"] == "host.supervision.failed"
    assert body["error"]["code"] == "unavailable"
    assert body["error"]["message"] == ("failed units cannot be read: "
                                        "no release is selected on this host, so it owns no unit yet")


def test_supervision_failed_reports_the_read_with_full_schema_command(tmp_path, monkeypatch, capsys):
    from claudlobby import failed_units
    data = {"release_id": "r-test", "platform": "Linux", "declared": 3, "units": [
        {"unit": "com.t.creds-check.service", "scope": "fleet", "fleet": "F", "bot": None, "surface": "systemd",
         "state": "failed", "detail": "ActiveState=failed SubState=failed Result=exit-code ExecMainStatus=4",
         "template": None}]}
    roots = []
    monkeypatch.setattr(failed_units, "read_failed_units", lambda root: roots.append(root) or data)
    body = call(capsys, ["--root", str(tmp_path), "--json", "host", "supervision", "failed"])
    assert body["command"] == "host.supervision.failed" and body["release_id"] == "r-test"
    assert body["data"] == data and roots == [tmp_path.resolve()]
    assert main(["--root", str(tmp_path), "host", "supervision", "failed"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "failed: com.t.creds-check.service (fleet F), systemd "
        "ActiveState=failed SubState=failed Result=exit-code ExecMainStatus=4"]


def test_host_status_distinguishes_absent_active_and_interrupted_recorded_state(candidate, capsys, tmp_path):
    root, release, plan, _ = candidate
    empty = tmp_path / "empty"
    empty.mkdir()
    absent = call(capsys, ["--root", str(empty), "host", "status", "--json"])
    assert absent["request_id"] is None and absent["data"]["recorded_status"] == "unselected"
    assert absent["data"]["runtime_observation"] == "unknown" and absent["data"]["bootstrap_eligibility"] == "not_checked"
    assert list(empty.iterdir()) == []
    with state.locked_activation(root) as store:
        store.prepare("previous", plan, recovery_release_id=release.release_id, enrollment_digest="1" * 64)
        for step in state.STEPS:
            store.begin("previous", step)
            if step == "selection_switched":
                store.select("previous")
            else:
                store.complete("previous", step, evidence_digest="2" * 64)
    journal = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, (),
                                effects={"owner": "activation-units-v1"}).seal()
    prepare_config(journal, "units-" + "a" * 64)
    before = snapshot(root)
    argv = ["--root", str(root), "host", "status", "--json"]
    active = call(capsys, argv)
    assert active["data"]["recorded_status"] == "active" and active["data"]["runtime_observation"] == "unknown"
    assert active["data"]["upgrade_supported"] is False
    assert active["data"]["activation_errors"] == [] and snapshot(root) == before
    with state.locked_activation(root) as store:
        store.prepare("interrupted", plan, recovery_release_id=release.release_id, enrollment_digest="1" * 64)
        store.begin("interrupted", "producers_paused")
    before = snapshot(root)
    pending = call(capsys, argv, 4)
    assert pending["data"]["recorded_status"] == "incomplete"
    assert pending["data"]["selected_activation"]["status"] == "active"
    assert pending["data"]["upgrade_supported"] is False
    assert pending["data"]["unfinished_activations"][0]["pending_step"] == "producers_paused"
    assert pending["data"]["recovery_supported"] is False
    assert snapshot(root) == before
    assert main(["--root", str(root), "host", "status"]) == 4
    captured = capsys.readouterr()
    plain = captured.out + captured.err
    assert "interrupted" in plain and "producers_paused" in plain
    assert "no supported recovery step" in plain
    assert "--resume ID" not in plain and snapshot(root) == before
    (root / "state/activations/torn").mkdir()
    torn = call(capsys, argv, 4)
    assert any(row["activation_id"] == "torn" for row in torn["data"]["activation_errors"])


def test_status_missing_root_reports_real_passive_facts_without_creating_it(tmp_path, capsys, monkeypatch):
    from claudlobby import setup_observations as observations
    from claudlobby.commands import host
    import sqlite3
    import subprocess

    root = tmp_path / "not-created"
    package = SimpleNamespace(artifact_id="artifact", source_revision=None, content_sha256="digest")
    monkeypatch.setattr(resources, "get_resources", lambda: package)
    monkeypatch.setattr(observations.shutil, "which", lambda name: None if name == "claude" else "/unused/tool")

    def forbidden(*args, **kwargs):
        pytest.fail("passive status attempted a runtime effect or recorded-state backend")

    monkeypatch.setattr(host, "_host_releases", forbidden)
    monkeypatch.setattr(host, "_operator_shell", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    before = snapshot(tmp_path)
    result = call(capsys, ["--root", str(root), "host", "status", "--json"])
    data = result["data"]
    assert not root.exists() and snapshot(tmp_path) == before
    assert result["command"] == "host.status" and result["request_id"] is None
    assert result["release_id"] is None and data["selection"] is None
    assert data["recorded_status"] == "unselected" and data["releases"] == []
    facts = data["preactivation"]
    assert facts["root"] == facts["plane_storage"] == "absent"
    assert facts["package"] == {"state": "present", "artifact_id": "artifact",
                                "source_revision": None, "content_sha256": "digest"}
    assert facts["executables"] == {"tmux": "present", "claude": "missing", "jq": "present"}
    assert "provider_login" in facts["not_checked"]
    assert data["bootstrap_eligibility"] == "not_checked" and "can_activate" not in data


def test_status_empty_root_adds_observations_without_changing_diagnosis(tmp_path, capsys):
    result = call(capsys, ["--root", str(tmp_path), "host", "status", "--json"])
    data = result["data"]
    assert data["recorded_status"] == "unselected" and data["recovery"] == []
    assert data["preactivation"]["root"] == "directory"
    assert data["preactivation"]["plane_storage"] == "absent"
    assert list(tmp_path.iterdir()) == []


def test_status_empty_plane_is_not_bootstrap_permission(tmp_path, capsys):
    plane = tmp_path / "state/plane"
    plane.mkdir(parents=True)
    result = call(capsys, ["--root", str(tmp_path), "host", "status", "--json"])
    assert result["data"]["preactivation"]["plane_storage"] == "empty"
    assert result["data"]["bootstrap_eligibility"] == "not_checked"
    assert list(plane.iterdir()) == []


@pytest.mark.parametrize("area", ["state", "plane"])
def test_status_non_directory_plane_is_only_an_observation(tmp_path, capsys, area):
    root = tmp_path / "root"
    target = root / "state" if area == "state" else root / "state/plane"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("private-body")
    result = call(capsys, ["--root", str(root), "host", "status", "--json"], 4 if area == "state" else 0)
    assert result["data"]["preactivation"]["plane_storage"] == "invalid"
    assert "private-body" not in json.dumps(result)


def test_status_retains_recorded_conflict_with_invalid_plane_observation(candidate, capsys):
    root, _, _, _ = candidate
    (root / "state/selected-release.json").write_text("not-json-private")
    (root / "state/plane").write_text("private-body")
    result = call(capsys, ["--root", str(root), "host", "status", "--json"], 4)
    assert result["data"]["recorded_status"] == "indeterminate"
    assert result["data"]["selection_error"]
    assert result["data"]["preactivation"]["plane_storage"] == "invalid"
    assert result["error"]["message"] == "conflict: recorded host state is indeterminate"


def test_status_isolated_cli_preserves_absent_root_and_private_home(tmp_path):
    import subprocess
    import sys
    from tests.conftest import constructed_env

    home, tools = tmp_path / "home", tmp_path / "tools"
    home.mkdir()
    tools.mkdir()
    sentinel = tmp_path / "executed-native-tool"
    for name in ("tmux", "claude", "jq"):
        tool = tools / name
        tool.write_text('#!/bin/sh\nprintf executed > "$TOOL_SENTINEL"\nexit 97\n')
        tool.chmod(0o700)
    env = constructed_env(HOME=home, PATH=tools, PYTHONDONTWRITEBYTECODE="1",
                          TOOL_SENTINEL=sentinel, CLAUDLOBBY_ROOT=tmp_path / "ambient",
                          BOT_DIR=tmp_path / "foreign-bot", BOT_ID="ignored-bot")
    root = tmp_path / "absent"
    before = snapshot(tmp_path)
    for _ in range(2):
        result = subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root),
                                 "host", "status", "--json"], env=env, cwd=tmp_path,
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["data"]["preactivation"]["executables"] == dict.fromkeys(("tmux", "claude", "jq"), "present")
        assert body["data"]["preactivation"]["root"] == "absent"
        assert body["request_id"] is None and body["release_id"] is None
    assert not root.exists() and not sentinel.exists() and not list(home.iterdir())
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("entry", ["plane.db", "plane.db-wal", "daemon.sock", "staged", "unknown", "owner-access.db"])
def test_status_discloses_any_plane_content_without_opening_or_removing_it(tmp_path, capsys, entry):
    plane = tmp_path / "state/plane"
    plane.mkdir(parents=True)
    (plane / entry).write_bytes(b"unread private contents")
    before = snapshot(tmp_path)
    result = call(capsys, ["--root", str(tmp_path), "host", "status", "--json"])
    assert result["data"]["preactivation"]["plane_storage"] == "nonempty"
    assert result["data"]["bootstrap_eligibility"] == "not_checked"
    assert b"unread private contents" not in json.dumps(result).encode()
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("area", ["state", "plane"])
def test_status_plane_redirection_is_only_an_observation_without_foreign_details(tmp_path, capsys, area):
    root, foreign = tmp_path / "root", tmp_path / "foreign"
    root.mkdir()
    foreign.mkdir()
    (foreign / "private-marker").write_text("secret-value")
    target = root / "state" if area == "state" else root / "state/plane"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(foreign, target_is_directory=True)
    result = call(capsys, ["--root", str(root), "host", "status", "--json"], 4 if area == "state" else 0)
    assert "secret-value" not in json.dumps(result) and "private-marker" not in json.dumps(result)
    if area == "state":
        assert "foreign" not in result["error"]["message"]
    else:
        assert result["data"]["preactivation"]["plane_storage"] == "redirected"
    assert list(foreign.iterdir()) == [foreign / "private-marker"]


@pytest.mark.parametrize("spelling", ["symlink", "relative", "parent"])
def test_status_existing_root_aliases_preserve_normalization(tmp_path, capsys, monkeypatch, spelling):
    root = tmp_path / "real"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    value = str(alias) if spelling == "symlink" else "real" if spelling == "relative" else "real/../real"
    result = call(capsys, ["--root", value, "host", "status", "--json"])
    assert result["data"]["root"] == str(root.resolve())
    assert result["data"]["recorded_status"] == "unselected"
    assert result["data"]["preactivation"]["root"] == "directory"
    assert list(root.iterdir()) == []


def test_status_classifies_unreadable_storage_without_exception_text(tmp_path, capsys, monkeypatch):
    from claudlobby import setup_observations as observations
    plane = tmp_path / "state/plane"
    plane.mkdir(parents=True)
    original = observations.os.scandir

    def unreadable(path):
        if Path(path) == plane:
            raise PermissionError("private-path-and-token")
        return original(path)

    monkeypatch.setattr(observations.os, "scandir", unreadable)
    result = call(capsys, ["--root", str(tmp_path), "host", "status", "--json"])
    assert result["data"]["preactivation"]["plane_storage"] == "unavailable"
    assert "private-path-and-token" not in json.dumps(result)


def test_status_missing_resources_and_discovery_error_remain_observations(tmp_path, capsys, monkeypatch):
    from claudlobby import setup_observations as observations

    def missing():
        raise RuntimeError("private-package-path")

    def discovery(name):
        if name == "jq":
            raise OSError("private-native-detail")
        return None

    monkeypatch.setattr(resources, "get_resources", missing)
    monkeypatch.setattr(observations.shutil, "which", discovery)
    result = call(capsys, ["--root", str(tmp_path / "absent"), "host", "status", "--json"])
    facts = result["data"]["preactivation"]
    assert facts["package"]["state"] == "unavailable" and facts["package"]["artifact_id"] is None
    assert facts["executables"] == {"tmux": "missing", "claude": "missing", "jq": "unavailable"}
    assert "private-" not in json.dumps(result)


def test_status_rejects_relative_missing_root_and_keeps_mutation_root_requirement(tmp_path, capsys, monkeypatch):
    from claudlobby.commands import host
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(host, "_operator_shell", lambda: None)
    call(capsys, ["--root", "absent", "host", "status", "--json"], 2)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind,expected", [("file", 3), ("file-alias", 3), ("ancestor-file", 3),
                                            ("ancestor-file-alias", 3), ("dangling-leaf", 0), ("dangling-ancestor", 0)])
def test_status_missing_root_edges_follow_existing_normalization(tmp_path, capsys, kind, expected):
    file = tmp_path / "file"
    file.write_text("private-content")
    alias = tmp_path / "alias"
    missing = tmp_path / "absent"
    if kind == "file":
        root = file
    elif kind == "ancestor-file":
        root = file / "child"
    else:
        alias.symlink_to(missing if kind.startswith("dangling") else file)
        root = alias / "child" if kind in {"dangling-ancestor", "ancestor-file-alias"} else alias
    before = snapshot(tmp_path)
    result = call(capsys, ["--root", str(root), "host", "status", "--json"], expected)
    if expected == 0:
        assert result["data"]["root"] == str(root.resolve())
        assert result["data"]["preactivation"]["root"] == "absent"
    else:
        assert result["error"]["code"] == "not_found" and result["data"] == {}
    assert not missing.exists() and snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["file", "file-alias", "ancestor-file", "ancestor-file-alias"])
def test_status_relative_non_directory_roots_keep_not_found(tmp_path, capsys, monkeypatch, kind):
    file = tmp_path / "file"
    file.write_text("private-content")
    alias = tmp_path / "alias"
    alias.symlink_to(file)
    monkeypatch.chdir(tmp_path)
    name = "alias" if kind.endswith("alias") else "file"
    value = name + "/child" if kind.startswith("ancestor") else name
    before = snapshot(tmp_path)
    result = call(capsys, ["--root", value, "host", "status", "--json"], 3)
    assert result["error"]["code"] == "not_found" and result["data"] == {}
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("storage", ["invalid", "redirected", "unavailable"])
def test_status_active_recorded_diagnosis_survives_passive_storage_limit(candidate, capsys, monkeypatch, storage):
    from claudlobby import setup_observations as observations
    root, release, plan, _ = candidate
    with state.locked_activation(root) as store:
        store.prepare("active", plan, recovery_release_id=release.release_id, enrollment_digest="1" * 64)
        for step in state.STEPS:
            store.begin("active", step)
            if step == "selection_switched":
                store.select("active")
            else:
                store.complete("active", step, evidence_digest="2" * 64)
    monkeypatch.setattr(observations, "_plane_storage", lambda _: storage)
    result = call(capsys, ["--root", str(root), "host", "status", "--json"])
    assert result["data"]["recorded_status"] == "active"
    assert result["data"]["upgrade_supported"] is False
    assert result["data"]["preactivation"]["plane_storage"] == storage


def test_status_bare_release_refusal_keeps_empty_diagnostic_data(tmp_path, capsys):
    store, foreign = tmp_path / "state/releases", tmp_path / "foreign"
    foreign.mkdir()
    store.parent.mkdir()
    store.symlink_to(foreign, target_is_directory=True)
    result = call(capsys, ["--root", str(tmp_path), "host", "status", "--json"], 4)
    assert result["data"] == {}
    assert result["error"]["message"] == "conflict: release store is redirected"


@pytest.mark.parametrize("existing", [False, True])
def test_status_text_displays_fixed_observations_without_identifiers(tmp_path, capsys, monkeypatch, existing):
    from claudlobby import setup_observations as observations
    root = tmp_path / "private-root-name"
    if existing:
        root.mkdir()
    monkeypatch.setattr(resources, "get_resources", lambda: SimpleNamespace(
        artifact_id="private-artifact-id", source_revision="private-source-id", content_sha256="private-hash"))
    monkeypatch.setattr(observations.shutil, "which", lambda _: None)
    assert main(["--root", str(root), "host", "status"]) == 0
    text = capsys.readouterr().out
    assert "package=present; tmux=missing, claude=missing, jq=missing" in text
    assert "Plane=absent" in text and "were not checked" in text
    assert "browser pairing, runtime readiness" in text
    assert "private-" not in text


def test_status_missing_and_empty_recorded_fields_have_same_shape(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    absent = call(capsys, ["--root", str(tmp_path / "missing"), "host", "status", "--json"])["data"]
    present = call(capsys, ["--root", str(empty), "host", "status", "--json"])["data"]
    assert absent.keys() == present.keys()
    assert {k: v for k, v in absent.items() if k not in {"root", "preactivation"}} == {
        k: v for k, v in present.items() if k not in {"root", "preactivation"}}
