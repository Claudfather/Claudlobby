"""Public host routes with real plans/journals and explicit lifecycle collaborator."""

import json
from pathlib import Path
from uuid import UUID

import pytest

from claudlobby.__main__ import main
from claudlobby import activation, activation_state as state, resources
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
    for route in ("status", "activate"):
        result = _run(PARSE, "host", route, "--help", tmp_path=tmp_path)
        assert result.returncode == 0, result.stderr
    result = _run(PARSE, "--json", "host", "activate", "p-secret", tmp_path=tmp_path)
    assert result.returncode == 2 and json.loads(result.stdout)["command"] == "host.activate"
    assert "p-secret" not in result.stdout + result.stderr
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(tmp_path))
    result = call(capsys, ["--json", "host", "status"], 2)
    assert "explicit --root" in result["error"]["message"]
    with pytest.raises(SystemExit) as exc:
        main(["host", "status", "--root", str(tmp_path), "--json"])
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["command"] == "host.status"
    assert list(tmp_path.iterdir()) == []


def test_activate_freezes_one_id_and_delegates_exact_reviewed_candidate(candidate, monkeypatch, capsys):
    root, release, plan, directory = candidate
    calls = []
    def bootstrap(selected_root, activation_id, plan_id, install_directory):
        calls.append((selected_root, activation_id, plan_id, install_directory))
        assert (selected_root, plan_id, install_directory) == (root, plan.plan_id, directory)
        # Native/SQL/config behavior belongs to the separately tested backend;
        # retain a real durable owner result for this CLI boundary fixture.
        with state.locked_activation(root) as store:
            store.prepare(activation_id, plan, recovery_release_id=release.release_id, enrollment_digest="1" * 64)
            for step in state.STEPS:
                store.begin(activation_id, step)
                if step == "selection_switched":
                    store.select(activation_id)
                else:
                    record = store.complete(activation_id, step, evidence_digest="2" * 64)
        return record
    monkeypatch.setattr(activation, "bootstrap_activation", bootstrap)
    argv = ["--root", str(root), "--json", "host", "activate", plan.plan_id,
            "--install-directory", str(directory)]
    before = snapshot(root)
    monkeypatch.setattr(resources, "selected_cli", lambda: directory / "wrong-cli")
    wrong = call(capsys, argv, 7)
    assert wrong["error"]["code"] == "release_mismatch" and calls == [] and snapshot(root) == before
    monkeypatch.setattr(resources, "selected_cli", lambda: release.cli_path)
    result = call(capsys, argv)
    assert str(UUID(result["request_id"])) == result["request_id"]
    assert calls == [(root, result["request_id"], plan.plan_id, directory)]
    assert result["data"]["activation_id"] == result["request_id"]
    assert result["data"]["recorded_activation"]["status"] == "active"
    assert result["data"]["recording"] == "committed" and result["release_id"] == release.release_id


def test_explicit_first_adoption_routes_to_legacy_owner_without_changing_cold_default(
        candidate, monkeypatch, capsys):
    root, release, plan, directory = candidate
    calls = []

    def adopt(selected_root, activation_id, plan_id, install_directory):
        calls.append((selected_root, activation_id, plan_id, install_directory))
        with state.locked_activation(root) as store:
            store.prepare(activation_id, plan, recovery_release_id=release.release_id,
                          enrollment_digest="1" * 64, legacy_source=True)
            for step in state.STEPS:
                store.begin(activation_id, step)
                record = (store.select(activation_id) if step == "selection_switched" else
                          store.complete(activation_id, step, evidence_digest="2" * 64))
        return record

    monkeypatch.setattr(activation, "adopt_existing_activation", adopt)
    monkeypatch.setattr(activation, "bootstrap_activation", lambda *_: pytest.fail("cold owner called"))
    result = call(capsys, ["--root", str(root), "--json", "host", "activate", plan.plan_id,
                           "--install-directory", str(directory), "--adopt-existing"])
    assert calls == [(root, result["request_id"], plan.plan_id, directory)]
    record = state.read_activation(root, result["request_id"])
    assert record.status == "active"
    assert record.body["intent"]["source_kind"] == "legacy-unsealed"
    assert record.body["intent"]["source_release_id"] is None


def test_generated_context_and_existing_estate_refuse_with_inspection_guidance(candidate, monkeypatch, capsys):
    root, release, plan, directory = candidate
    calls = []
    def refuse(*args):
        calls.append(args)
        raise state.ActivationError("existing estate SECRET-value")
    monkeypatch.setattr(activation, "bootstrap_activation", refuse)
    argv = ["--root", str(root), "host", "activate", plan.plan_id, "--install-directory", str(directory), "--json"]
    before = snapshot(root)
    monkeypatch.setenv("BOT_ID", "")  # malformed generated context cannot become an operator
    denied = call(capsys, argv, 4)
    assert "operator shell" in denied["error"]["message"] and calls == []
    assert snapshot(root) == before
    monkeypatch.delenv("BOT_ID")
    denied = call(capsys, argv, 4)
    assert len(calls) == 1 and denied["data"]["activation_id"] == calls[0][1]
    assert "host status" in denied["error"]["hint"] and "does not recover an interrupted activation" in denied["error"]["hint"]
    assert "SECRET-value" not in json.dumps(denied) and snapshot(root) == before
    def native_refuse(*_):
        raise state.ActivationError("svc_activation_pause refused (3): SECRET-value")
    monkeypatch.setattr(activation, "bootstrap_activation", native_refuse)
    native = call(capsys, argv, 4)
    assert native["error"]["message"] == "conflict: svc_activation_pause refused (3)"
    assert "SECRET-value" not in json.dumps(native) and snapshot(root) == before


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
    assert active["data"]["activation_errors"] == [] and snapshot(root) == before
    with state.locked_activation(root) as store:
        store.prepare("interrupted", plan, recovery_release_id=release.release_id, enrollment_digest="1" * 64)
        store.begin("interrupted", "producers_paused")
    before = snapshot(root)
    pending = call(capsys, argv, 4)
    assert pending["data"]["recorded_status"] == "incomplete"
    assert pending["data"]["selected_activation"]["status"] == "active"
    assert pending["data"]["unfinished_activations"][0]["pending_step"] == "producers_paused"
    assert snapshot(root) == before
    (root / "state/activations/torn").mkdir()
    torn = call(capsys, argv, 4)
    assert any(row["activation_id"] == "torn" for row in torn["data"]["activation_errors"])
