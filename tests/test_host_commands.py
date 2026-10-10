"""Public host routes with real plans/journals and explicit lifecycle collaborator."""

import json
from pathlib import Path
from types import SimpleNamespace
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


def test_host_doctor_discloses_macos_reaper_native_limit(tmp_path, monkeypatch):
    from claudlobby import activation_state, context, switches
    from claudlobby.commands import _helpers, host_doctor

    paths = SimpleNamespace(root=tmp_path, fleet_yaml=tmp_path / "fleet.yaml", package=object())
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
    for route in ("status", "activate", "abort-adoption"):
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


def test_repair_start_routes_through_environment_guard_and_discloses_unknown_write(
        candidate, monkeypatch, capsys):
    from claudlobby.commands import operator_context
    root, release, plan, directory = candidate
    argv = ["--root", str(root), "--json", "host", "repair-start", "act-1",
            "--fleet", "example", "--bot", "worker", "--reason", "exited before bridge"]
    calls, guarded = [], []

    def repair(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 2:
            raise OSError("fsync failed after replace")
        return SimpleNamespace(body={"intent": {"release_id": release.release_id, "plan_id": plan.plan_id,
                                                "install_directory": str(directory)},
                                     "start_repairs": [{"attempt": {"target": "com.example.worker.service",
                                                                    "fence": {"ceiling": 1, "fence": "RR_FENCE_x"}}}]})

    monkeypatch.setattr(activation, "repair_failed_bot_start", repair)
    monkeypatch.setenv("BOT_ID", "worker")
    assert main(argv) != 0  # A bot caller is refused before any repair owner runs.
    body = json.loads(capsys.readouterr().out)
    assert body["command"] == "host.repair-start" and body["error"]["code"] == "conflict"
    assert calls == []
    monkeypatch.delenv("BOT_ID")
    monkeypatch.setattr(operator_context, "require_operator_context", guarded.append)
    body = call(capsys, argv)
    # Only the environment guard runs here; the backend owns native ancestry.
    assert guarded == [None] and calls[0][0] == (root, "act-1")
    assert calls[0][1] == {"fleet": "example", "bot": "worker", "reason": "exited before bridge"}
    assert body["data"]["recording"] == "committed"
    assert body["data"]["target_release_id"] == release.release_id
    assert main(argv) != 0
    body = json.loads(capsys.readouterr().out)
    assert body["data"]["recording"] == "unknown" and "inspect" in body["error"]["hint"]


def test_supervision_reap_requires_an_explicit_mode_with_full_schema_command(tmp_path):
    result = _run(PARSE, "--json", "host", "supervision", "reap-orphans",
                  tmp_path=tmp_path)
    assert result.returncode == 2 and result.stderr == ""
    body = json.loads(result.stdout)
    assert body["schema_version"] == 1
    assert body["command"] == "host.supervision.reap-orphans"
    assert body["error"]["code"] == "invalid_argument"
    assert body["error"]["hint"] == "inspect claudlobby host supervision reap-orphans --help"


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
    upgrades = []

    def upgrade(selected_root, activation_id, plan_id, install_directory):
        upgrades.append((selected_root, activation_id, plan_id, install_directory))
        with state.locked_activation(root) as store:
            store.prepare(activation_id, plan, recovery_release_id=release.release_id,
                          source_release_id=release.release_id, enrollment_digest="1" * 64)
            for step in state.STEPS:
                store.begin(activation_id, step)
                record = (store.select(activation_id) if step == "selection_switched" else
                          store.complete(activation_id, step, evidence_digest="2" * 64))
        return record

    monkeypatch.setattr(activation, "upgrade_activation", upgrade)
    second = call(capsys, argv)
    assert upgrades == [(root, second["request_id"], plan.plan_id, directory)]
    assert second["data"]["upgrade_supported"] is True


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


def test_activate_resume_reuses_recorded_id_and_reports_unsupported_start_stage(candidate, monkeypatch, capsys):
    root, release, plan, directory = candidate
    with state.locked_activation(root) as store:
        store.prepare("interrupted", plan, recovery_release_id=release.release_id,
                      enrollment_digest="1" * 64)
        for step in state.STEPS[:state.STEPS.index("bots_started")]:
            store.begin("interrupted", step)
            if step == "selection_switched":
                store.select("interrupted")
            else:
                store.complete("interrupted", step, evidence_digest="2" * 64)
        store.begin("interrupted", "bots_started")
    before = snapshot(root)
    argv = ["--root", str(root), "--json", "host", "activate", plan.plan_id,
            "--install-directory", str(directory), "--resume", "interrupted"]
    refusal = call(capsys, argv, 4)
    assert refusal["request_id"] == "interrupted"
    assert refusal["data"]["activation_id"] == "interrupted"
    assert "cannot safely resume" in refusal["error"]["message"]
    assert snapshot(root) == before

    calls = []
    def resumed(selected_root, activation_id, plan_id, install_directory):
        calls.append((selected_root, activation_id, plan_id, install_directory))
        raise state.ActivationError("kept pending")
    monkeypatch.setattr(activation, "resumable_running_step", lambda _: "queues_classified")
    monkeypatch.setattr(activation, "resume_activation", resumed)
    refusal = call(capsys, argv, 4)
    assert calls == [(root, "interrupted", plan.plan_id, directory)]
    assert refusal["request_id"] == "interrupted"


def test_abort_adoption_binds_id_and_sql_precondition_before_owner(candidate, monkeypatch, capsys):
    import sqlite3
    from claudlobby import activation_units
    root, release, plan, _ = candidate
    with state.locked_activation(root) as store:
        store.prepare("adopt", plan, recovery_release_id=release.release_id,
                      enrollment_digest="1" * 64, legacy_source=True)
        store.begin("adopt", "producers_paused")
    database = root / "state/plane/plane.db"
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA user_version=12")
    conn.close()
    calls = []

    def abort(store, activation_id, *, reason, release, sql_user_version):
        # Native/file restoration belongs to the separately tested owner; keep
        # its real durable record transitions for this CLI boundary.
        calls.append((activation_id, reason, release.release_id, sql_user_version))
        store.begin_adoption_abort(activation_id, reason=reason, release_id=release.release_id,
                                   artifact_id=release.inputs.artifact_id,
                                   sql_user_version=sql_user_version)
        store.finish_adoption_abort(activation_id, evidence_digest="3" * 64, resumed=[])
        return SimpleNamespace(targets=("clock.timer",), operation="aborted")

    monkeypatch.setattr(activation_units, "abort_early_adoption", abort)
    argv = ["--root", str(root), "--json", "host", "abort-adoption", "adopt",
            "--reason", "masked reader refused", "--expected-sql-version"]
    before = snapshot(root)
    wrong = call(capsys, argv + ["13"], 4)
    assert wrong["request_id"] is None and wrong["data"]["activation_id"] == "adopt"
    assert "preflight expectation" in wrong["error"]["message"]
    assert calls == [] and snapshot(root) == before
    result = call(capsys, argv + ["12"])
    assert result["request_id"] is None and result["release_id"] == release.release_id
    assert result["data"]["activation_id"] == "adopt"
    assert calls == [("adopt", "masked reader refused", release.release_id, 12)]
    assert result["data"]["recording"] == "committed"
    assert result["data"]["recorded_activation"]["status"] == "rolled_back"
    assert result["data"]["restored_targets"] == ["clock.timer"]
    assert state.read_activation(root, "adopt").body["adoption_abort"]["sql_user_version"] == 12


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
    # No record for this ID: the host is unchanged and there is nothing to resume.
    assert denied["data"]["recording"] == "unchanged"
    assert "no activation record" in denied["error"]["hint"] and "--resume" not in denied["error"]["hint"]
    assert "pending step" not in json.dumps(denied)
    assert "SECRET-value" not in json.dumps(denied) and snapshot(root) == before
    def native_refuse(*_):
        raise state.ActivationError("svc_activation_pause refused (3): SECRET-value")
    monkeypatch.setattr(activation, "bootstrap_activation", native_refuse)
    native = call(capsys, argv, 4)
    assert native["error"]["message"] == "conflict: svc_activation_pause refused (3)"
    assert "SECRET-value" not in json.dumps(native) and snapshot(root) == before
    # The owner's drift refusal names its frozen target; anything else stays generic.
    for text, shown in (("native enrollment changed: com.example.creds-check.service",
                         "native enrollment changed since inventory: com.example.creds-check.service"),
                        ("native enrollment changed: x SECRET-value", "activation did not complete")):
        monkeypatch.setattr(activation, "bootstrap_activation",
                            lambda *_, text=text: (_ for _ in ()).throw(state.ActivationError(text)))
        drift = call(capsys, argv, 4)
        assert shown in drift["error"]["message"] and "SECRET-value" not in json.dumps(drift)


def test_activate_discloses_lock_preflight_without_claiming_a_pending_step(candidate, monkeypatch, capsys):
    root, _, plan, directory = candidate
    monkeypatch.setattr(activation, "bootstrap_activation", lambda *_:
                        (_ for _ in ()).throw(state.ActivationError("another host activation holds the lock")))
    result = call(capsys, ["--root", str(root), "--json", "host", "activate", plan.plan_id,
                           "--install-directory", str(directory)], 4)
    assert result["data"]["recorded_activation"] is None
    assert result["error"]["message"] == (
        "conflict: host activation lock is held; no activation record was created")
    assert "pending step" not in result["error"]["hint"]
    # Jobs that outlast host activate's wait are named, not taken for another activation (#2208).
    jobs = "a running job or host operation still holds the lock after a 300 s wait"
    monkeypatch.setattr(activation, "bootstrap_activation", lambda *_:
                        (_ for _ in ()).throw(state.ActivationError(jobs)))
    busy = call(capsys, ["--root", str(root), "--json", "host", "activate", plan.plan_id,
                         "--install-directory", str(directory)], 4)
    assert busy["data"]["recorded_activation"] is None
    assert busy["error"]["message"] == f"conflict: {jobs}; no activation record was created"
    assert "pending step" not in busy["error"]["hint"]

    target = "gui/501/claudlobby-browser-reaper"
    monkeypatch.setattr(activation, "bootstrap_activation", lambda *_:
                        (_ for _ in ()).throw(activation.CandidateDisabledOverride((target,))))
    disabled = call(capsys, ["--root", str(root), "--json", "host", "activate", plan.plan_id,
                             "--install-directory", str(directory)], 4)
    assert target in disabled["error"]["message"]
    assert "explicitly enable" in disabled["error"]["hint"]
    assert disabled["data"]["recorded_activation"] is None
    assert disabled["data"]["recording"] == "unchanged"
    assert "pending step" not in json.dumps(disabled)

    # The real pre-pause handoff preflight refuses before any record or pause;
    # the operator must see the product-owned reason, not a pending-step guess.
    from claudlobby.activation_handoffs import preflight_canonical_handoffs
    old_bot = root / "old-bot"
    old_bot.mkdir()
    monkeypatch.setattr(activation, "bootstrap_activation", lambda *_: preflight_canonical_handoffs(
        root, roster={"alpha": ("manager", ("worker",))}, bot_dirs={("alpha", "worker"): old_bot}))
    before = snapshot(root)
    handoff = call(capsys, ["--root", str(root), "--json", "host", "activate", plan.plan_id,
                            "--install-directory", str(directory)], 4)
    assert handoff["error"]["message"] == (
        "conflict: activation refused: old fleet manager has no installed handoff owner; "
        "no activation record was created")
    assert handoff["data"]["recorded_activation"] is None
    assert handoff["data"]["recording"] == "unchanged"
    assert "pending step" not in json.dumps(handoff) and snapshot(root) == before

    import subprocess
    monkeypatch.setattr(activation, "bootstrap_activation", lambda *_: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(["svc", "SECRET-arg"], 5, stderr=b"SECRET-stderr")))
    timeout = call(capsys, ["--root", str(root), "--json", "host", "activate", plan.plan_id,
                            "--install-directory", str(directory)], 6)
    assert timeout["error"]["code"] == "unavailable"
    assert timeout["data"]["recording"] == "unchanged"
    assert "SECRET" not in json.dumps(timeout)


def test_activate_shows_run_intent_refusal_with_host_unchanged(candidate, monkeypatch, capsys):
    root, _, plan, directory = candidate
    # The real pre-record owner: a de-enrolled old bot the candidate still declares.
    context = SimpleNamespace(paths=SimpleNamespace(bot_runtime=lambda bot: root / bot),
                              fleet=SimpleNamespace(name="alpha", manager="manager",
                                                    bots={"worker": SimpleNamespace(autonomous_runner=None)}))
    stopped = SimpleNamespace(installed=(), properties=(), declaration=SimpleNamespace(
        scope="bot", fleet="alpha", bot="worker", working_directory=root / "worker"))
    monkeypatch.setattr(activation, "bootstrap_activation", lambda *_:
                        activation._refuse_unrecorded_run_intent(root, (stopped,), (context,)))
    before = snapshot(root)
    result = call(capsys, ["--root", str(root), "--json", "host", "activate", plan.plan_id,
                           "--install-directory", str(directory)], 4)
    message = result["error"]["message"]
    assert message.startswith("conflict: activation refused: run intent blocks activation before any record: "
                              "activation would start deliberately stopped bots (alpha/worker)")
    assert "re-run config plan" in message and message.endswith("no activation record was created")
    assert result["data"]["recorded_activation"] is None
    assert result["data"]["recording"] == "unchanged"
    assert "no activation record" in result["error"]["hint"] and "--resume" not in result["error"]["hint"]
    assert snapshot(root) == before


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
    assert active["data"]["upgrade_supported"] is True
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
def test_status_refuses_non_directory_storage(tmp_path, capsys, area):
    root = tmp_path / "root"
    target = root if area == "root" else root / "state" if area == "state" else root / "state/plane"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("private-body")
    result = call(capsys, ["--root", str(root), "host", "status", "--json"], 4)
    assert "private-body" not in json.dumps(result)


def test_status_retains_recorded_conflict_before_storage_refusal(candidate, capsys):
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
def test_status_refuses_redirected_storage_without_foreign_details(tmp_path, capsys, area):
    root, foreign = tmp_path / "root", tmp_path / "foreign"
    root.mkdir()
    foreign.mkdir()
    (foreign / "private-marker").write_text("secret-value")
    target = root if area == "root" else root / "state" if area == "state" else root / "state/plane"
    if area == "root":
        root.rmdir()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(foreign, target_is_directory=True)
    result = call(capsys, ["--root", str(root), "host", "status", "--json"], 4)
    assert "secret-value" not in json.dumps(result) and "private-marker" not in json.dumps(result)
    assert "foreign" not in result["error"]["message"]
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
    result = call(capsys, ["--root", str(tmp_path), "host", "status", "--json"], 6)
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
    call(capsys, ["--root", str(tmp_path / "absent"), "host", "activate", "p-" + "a" * 64,
                  "--install-directory", str(tmp_path), "--json"], 3)
    assert list(tmp_path.iterdir()) == []
