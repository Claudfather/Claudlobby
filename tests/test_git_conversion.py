"""Read-only conversion baseline over real historical readers and registry rows."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby import activation_state, config_install
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.update_conversion import read_sealed_baseline
from tests.fixtures.current_runtime import current_runtime, tmp_path  # noqa: F401
from tests.test_activation_state import write_activation, write_selection
from tests.test_config_install import historical_config_install
from tests.test_config_plan import historical_plan
from tests.test_releases import historical_release, release_tree

_COMPLETE = ("producers_paused", "sessions_handed_off", "sessions_quiesced", "ingest_quiesced",
    "queues_classified", "backup_saved", "migration_applied", "selection_switched",
    "configuration_applied", "ingest_started", "bots_started", "verified", "producers_resumed")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _save_record(case):
    case.activation.write_text(json.dumps({"record": case.body, "sha256": activation_state._digest(case.body)}))


def _rows(plan, status):
    rows = {}
    for change in plan.changes:
        target = Path(change.target)
        parent = target.parent
        while not parent.exists():
            rows.setdefault(str(parent), {"target": str(parent), "before": {"resolved": str(parent), "node": {"kind": "absent"}},
                "after": {"kind": "directory", "mode": 0o755, "entries": {}}, "ensure": True,
                "parent": str(parent.parent.resolve()), "status": status})
            parent = parent.parent
        rows[str(target)] = {"target": str(target), "before": change.before, "after": config_install._desired(change.after),
            "ensure": change.after["kind"] == "directory", "parent": str(target.parent.resolve()), "status": status}
    result = [rows[key] for key in sorted(rows)]
    for row in result:
        if row["ensure"] and status == "applied":
            path = Path(row["target"])
            path.mkdir(mode=0o755, parents=True, exist_ok=True)  # Historical fixture authoring, no runtime apply.
            row["identity"] = [path.stat().st_dev, path.stat().st_ino]
    return result


def _journal(plan, identifier, status):
    path = historical_config_install(plan, identifier, status=status)
    raw = json.loads(path.read_bytes())
    raw["journal"]["rows"] = _rows(plan, {"applied": "applied", "prepared": "pending", "rolled_back": "reverted"}[status])
    for row in raw["journal"]["rows"]:
        before = row["before"]["node"]
        if before["kind"] == "file":
            content = Path(row["target"]).read_bytes()
            assert hashlib.sha256(content).hexdigest() == before["sha256"]
            (path.parent / "blobs" / before["sha256"]).write_bytes(content)
            raw["journal"]["blobs"].append(before["sha256"])
    _save_journal(path, raw)
    return path


def _save_journal(path, raw):
    raw["sha256"] = config_install._digest(config_install._json(raw["journal"]))
    path.write_bytes(config_install._json(raw))


@pytest.fixture
def baseline(current_runtime, request):
    root, installation, _, host = current_runtime
    (root / "state/config.json").unlink()  # This is a sealed source, never a fake current Git snapshot.
    variant = getattr(request, "param", None)
    install_directory = host.directory / "sealed" if variant == "ensure" else host.directory
    inputs, paths, _, _ = release_tree(root)
    release = historical_release(root, inputs, paths)
    manifest, projects = root / "fleet.yaml", root / "projects.yaml"
    builder = ConfigPlanBuilder(root, ("example",), effects={"fleet_manifests": {"example": str(manifest)}, "units": []})
    builder.effects["fleet_sources"] = {"example": {
        "fleet": {"path": str(manifest), "sha256": builder.input_content(manifest)},
        "projects": {"path": str(projects), "sha256": builder.input_content(projects)}}}
    entries = []
    for bot in ("manager", "worker"):
        stem = "com.example." + bot
        working = root / "runtime/bots" / bot
        source = working / (stem + ".service")
        env = {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root),
            "CLAUDLOBBY_RELEASE_ID": release.release_id, "CLAUDLOBBY_ARTIFACT_ID": release.inputs.artifact_id,
            "CLAUDLOBBY_CLI": str(release.cli_path), "CLAUDLOBBY_NATIVE_DIR": str(release.native_path),
            "CLAUDLOBBY_LIBRARY_DIR": str(release.directory / "package/_resources/library")}
        argv = [str(release.directory / release.paths.interpreter), "-I", "-B", "-m",
            "claudlobby.runtime_admission", "unit-start", str(root), release.release_id,
            stem, "bots", "exec", "--", str(release.native_path / "start-bot.sh"), str(working)]
        content = ("[Service]\nWorkingDirectory=" + str(working) + "\nExecStart=" + " ".join(argv) + "\n").encode()
        sha = hashlib.sha256(content).hexdigest()
        builder.file(source, content)
        row = {"source": str(source), "scope": "bot", "phase": "bots", "release_id": release.release_id,
            "working_directory": str(working), "environment": env, "fleet": "example", "bot": bot,
            "enroll": not (variant == "dormant" and bot == "worker"), "admission": {"kind": "unit-start-v1", "unit": stem, "argv": argv},
            "service": None, "platform": "Linux", "sha256": sha, "mode": 0o644}
        builder.effects["units"].append(row)
        if variant == "dormant" and bot == "worker":
            continue
        entries.append({"target": source.name, "installed": str(install_directory / source.name),
            "source": str(source), "phase": "bots", "original": True,
            "working_directory": str(working), "environment": env, "service": None,
            "after": {"kind": "file", "sha256": sha, "mode": 0o644},
            "publish": bot != "worker", "enablement": None})
    if variant == "pending":
        for name, phase in (("com.example.schedule.timer", "producers"), ("com.example.recorder.service", "ingest")):
            source = root / "runtime/_host/timers" / name
            content = ("[Service]\nWorkingDirectory=" + str(root) + "\nExecStart=/bin/true\n").encode()
            sha = hashlib.sha256(content).hexdigest()
            builder.file(source, content)
            env = deepcopy(entries[0]["environment"])
            row = {"source": str(source), "scope": "host", "phase": phase, "release_id": release.release_id,
                "working_directory": str(root), "environment": env, "fleet": None, "bot": None,
                "enroll": True, "admission": {}, "service": None, "platform": "Linux", "sha256": sha, "mode": 0o644}
            builder.effects["units"].append(row)
            entries.append({"target": name, "installed": str(install_directory / name), "source": str(source),
                "phase": phase, "original": True, "working_directory": str(root), "environment": env,
                "service": None, "after": {"kind": "file", "sha256": sha, "mode": 0o644},
                "publish": True, "enablement": None})
    plan = historical_plan(builder, release_id=release.release_id, release_seal=release.seal_sha256)
    aid, enrollment = "previous-active", "d" * 64
    publication_files = []
    for phase in ("ingest", "bots", "producers"):
        effects = {"owner": "activation-enrollment-v1", "activation_id": aid,
            "configuration_journal": aid, "candidate_plan": plan.plan_id, "install_directory": str(install_directory),
            "enrollment_digest": enrollment, "entries": entries, "retired_enablement": [], "stopped_masks": [], "phase": phase}
        pub_builder = ConfigPlanBuilder(root, ("example",), effects=effects)
        for entry in entries:
            if entry["phase"] == phase and entry["publish"]:
                pub_builder.file(Path(entry["installed"]), plan.blob(entry["after"]["sha256"]))
        pub = historical_plan(pub_builder, release_id=release.release_id, release_seal=release.seal_sha256)
        identifier = "enrollment-" + _digest([aid, phase])
        path = _journal(pub, identifier, "applied")
        publication_files.append(path)
    identities = {**deepcopy(host.snapshot["identities"]), "root": str(root), "plan_id": plan.plan_id,
        "release_id": release.release_id, "release_seal": release.seal_sha256}
    intent = {"release_id": release.release_id, "plan_id": plan.plan_id,
        "install_directory": str(install_directory), "enrollment_digest": enrollment,
        "bot_states": {entries[1]["target"]: {"disposition": "stopped"}} if len(entries) > 1 else {}}
    activation, body = write_activation(root, aid, completed=_COMPLETE, intent=intent,
        identity_bindings=identities, handoff_effects={}, start_effects={}, bot_quiescence={})
    selector, selected = write_selection(root, aid, release.release_id, plan.plan_id)
    return SimpleNamespace(root=root, package=host.package, installation=installation, plan=plan,
        release=release, activation=activation, body=body, selector=selector, selected=selected,
        publications=publication_files, entries=entries)


def _aborted_history(case, kind):
    """Author the retained early-abort receipts, never run the old coordinator."""
    aid = "later-aborted-" + kind
    bot_path = Path(case.entries[0]["installed"])
    producer_path = bot_path.parent / "com.example.retained.timer"
    for path in (bot_path, producer_path):
        path.write_bytes(b"original frozen native file\n")
    phases = {"producers": [producer_path.name], "bots": [bot_path.name], "ingest": []}
    enrollment = {"manager": "Linux", "data_root": str(case.root), "legacy_source": kind == "adoption",
        "bootstrap_empty": False, "issues": [], "units": [
            {"target": path.name, "installed": [{"path": str(path)}]} for path in (producer_path, bot_path)]}
    digest = _digest(enrollment)
    shared = {"owner": "activation-units-v1", "activation_id": aid, "enrollment_digest": digest,
        "enrollment": enrollment, "phases": phases, "original_release_id": None if kind == "adoption" else case.release.release_id,
        "native_observations": {}}
    plans, journals = {}, {}
    for phase in ("producers", "bots", "ingest"):
        builder = ConfigPlanBuilder(case.root, ("example",), effects={**shared, "phase": phase})
        for unit in enrollment["units"]:
            if unit["target"] in phases[phase]:
                builder.remove(Path(unit["installed"][0]["path"]))
        plan = historical_plan(builder, release_id=case.release.release_id, release_seal=case.release.seal_sha256)
        identifier = "units-" + _digest([aid, phase])
        journals[phase] = _journal(plan, identifier, "rolled_back" if phase == "producers" else "prepared")
        plans[phase] = plan
    intent = {"release_id": case.release.release_id, "plan_id": case.plan.plan_id,
        "source_kind": "legacy-unsealed" if kind == "adoption" else "sealed",
        "source_release_id": None if kind == "adoption" else case.release.release_id, "enrollment_digest": digest}
    completed = [] if kind == "adoption" else ["producers_paused"]
    evidence = {} if kind == "adoption" else {"producers_paused": "e" * 64}
    forward = dict(status="activating", completed=completed, pending="producers_paused" if kind == "adoption" else "sessions_handed_off",
                   evidence=evidence)
    receipt = dict(phase="producers", journal_id="units-" + _digest([aid, "producers"]),
        plan_id=plans["producers"].plan_id, targets=phases["producers"], operation="aborted")
    path, body = write_activation(case.root, aid, status="rolled_back", completed=completed,
        intent=intent, previous_selection=None if kind == "adoption" else case.selected, evidence=evidence,
        forward=forward, handoff_effects={} if kind == "adoption" else {bot_path.name: None},
        **{kind + "_abort": {"sql_user_version": 1, "result": {"evidence": _digest(receipt), "resumed": [], "cleared": []}}})
    if kind == "upgrade":
        _journal(case.plan, aid, "prepared")
    return SimpleNamespace(activation=path, body=body, journals=journals)


def _files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_read_valid_baseline_preserves_original_bytes_ids_and_stopped_placement(baseline):
    case = baseline
    before = _files(case.root)
    result = read_sealed_baseline(case.root, package=case.package)
    assert result.selection == case.selected and result.selector_bytes == case.selector.read_bytes()
    assert result.selector_sha256 == hashlib.sha256(result.selector_bytes).hexdigest()
    assert result.release.inputs.source_revision == case.release.inputs.source_revision
    assert result.plan == case.plan and result.manager == "Linux" and result.domain == ""
    assert result.install_directory == Path(case.body["intent"]["install_directory"])
    assert result.identities["fleets"]["example"]["bots"] == case.body["identity_bindings"]["fleets"]["example"]["bots"]
    assert len(result.declarations) == 2 and len(result.placements) == 2
    assert result.placements[1]["publish"] is False
    assert result.placements[1]["installed"] == case.entries[1]["installed"]
    assert not (case.root / "state/config.json").exists() and _files(case.root) == before


def test_conversion_checkpoint_uses_frozen_old_context_without_current_git_config(baseline, monkeypatch):
    from claudlobby import native_session as native, update_operation as operation
    from claudlobby.active_config import context_from_plan
    from claudlobby.composer import account_dir
    from claudlobby.isolation import expand_home, transcript_slug
    from tests.test_native_session import Observation, SID, provider_record
    from tests.test_update_operation import Store, record

    case = baseline
    original = read_sealed_baseline(case.root, package=case.package)
    context = context_from_plan(original.plan, "example", bot="manager", package=case.package)
    bot = context.paths.bot_runtime("manager")
    account = expand_home(account_dir(context.fleet.bots["manager"], context.fleet), Path.home())
    adapter = Observation(case.root)
    adapter.package = case.package
    provider_record(bot, account, adapter, SID)
    transcript = account / "projects" / transcript_slug(bot) / (SID + ".jsonl")
    transcript.parent.mkdir(parents=True, exist_ok=True); transcript.write_bytes(b"original sealed session\n")
    (bot / "data/.spawn").touch()
    optional = bot / "data/.handoff-request"; optional.write_bytes(b"original unresolved optional request\n")
    retained = optional.read_bytes(), case.selector.read_bytes(), case.activation.read_bytes()
    monkeypatch.setattr(native, "_context", lambda *_: pytest.fail("conversion cannot require current Git configuration"))
    measured = []
    monkeypatch.setattr(native, "require_provider", lambda ctx: measured.append((ctx.bot_id, ctx.fleet.service_prefix)))
    monkeypatch.setattr(operation, "conversion_baseline", lambda store, package: original)
    data = record()
    data["conversion_scope"] = {"original": True}
    data["consumers"] = {"bot": data["consumers"]["bot"]}
    data["consumers"]["bot"]["intent"].update(bot_binding=[str(bot), "com.example.manager", str(case.root / "tmux")],
        declaration={"fleet": "example", "bot": "manager"})
    store = Store(data)
    operation._checkpoint(store, "bot", adapter)
    proof = store.load()["consumers"]["bot"]["checkpoint"]
    assert proof["operation_id"] == "original" and proof["session"]["session_id"] == SID
    assert proof["session"]["account"] == str(account)
    assert measured == [("manager", "com.example")]
    assert not (case.root / "state/config.json").exists()
    assert (optional.read_bytes(), case.selector.read_bytes(), case.activation.read_bytes()) == retained


@pytest.mark.parametrize("drift", ["selection", "authored", "identity", "publication", "unknown_handoff", "pending_stop"])
def test_baseline_refuses_drift_and_unresolved_effects_without_repair(baseline, drift):
    case = baseline
    if drift == "selection":
        case.selected["plan_id"] = "p-" + "0" * 64
        case.selector.write_text(json.dumps(case.selected))
    elif drift == "authored":
        (case.root / "fleet.yaml").write_text((case.root / "fleet.yaml").read_text() + "\n# Pending change\n")
    elif drift == "identity":
        case.body["identity_bindings"]["fleets"]["example"]["manager_uid"] = "actor_" + "0" * 32
        _save_record(case)
    elif drift == "publication":
        raw = json.loads(case.publications[1].read_bytes())
        raw["journal"]["status"] = "prepared"
        raw["sha256"] = config_install._digest(config_install._json(raw["journal"]))
        case.publications[1].write_bytes(config_install._json(raw))
    elif drift == "unknown_handoff":
        case.body["handoff_effects"] = {"com.example.manager.service": None}
        _save_record(case)
    else:
        case.body["bot_quiescence"] = {"com.example.manager.service": {"result": None}}
        _save_record(case)
    before = _files(case.root)
    with pytest.raises(activation_state.ActivationRefusal):
        read_sealed_baseline(case.root, package=case.package)
    assert _files(case.root) == before


def test_settled_older_rollback_keeps_original_unknown_as_historical_evidence(baseline):
    case = baseline
    past, _ = write_activation(case.root, "older-rolled-back", status="rolled_back",
        completed=["selection_restored"], handoff_effects={"com.example.manager.service": None})
    original = past.read_bytes()
    result = read_sealed_baseline(case.root, package=case.package)
    assert result.selection == case.selected and past.read_bytes() == original
    assert result.evidence[str(past)] == hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize("effect", ["unknown_handoff", "pending_stop"])
def test_prior_active_selection_does_not_hide_later_unfinished_lifecycle(baseline, effect):
    case = baseline
    extra = ({"handoff_effects": {"com.example.manager.service": None}} if effect == "unknown_handoff"
             else {"bot_quiescence": {"com.example.manager.service": {"result": None}}})
    later, _ = write_activation(case.root, "later-upgrade", status="activating",
        pending="sessions_handed_off" if effect == "unknown_handoff" else "sessions_quiesced",
        completed=["producers_paused"], release_id=case.release.release_id,
        plan_id=case.plan.plan_id, previous_selection=case.selected, **extra)
    before = _files(case.root)
    with pytest.raises(activation_state.ActivationRefusal, match="outside the pre-publication parking boundary"):
        read_sealed_baseline(case.root, package=case.package)
    assert _files(case.root) == before and later.exists()
    assert json.loads(case.selector.read_bytes()) == case.selected


def test_archived_selector_requires_original_explicit_location_hash_and_selection(baseline):
    case = baseline
    original = case.selector.read_bytes()
    before = read_sealed_baseline(case.root, package=case.package)
    archive = case.root / "state/update-work/original-selector.json"
    archive.parent.mkdir(exist_ok=True)
    case.selector.rename(archive)  # Test authoring only; reader never retires the selector.
    descriptor = {"path": str(archive), "sha256": hashlib.sha256(original).hexdigest()}
    result = read_sealed_baseline(case.root, package=case.package, expected_selection=case.selected,
                                  archived_selector=descriptor)
    assert result.selector_bytes == original and result.selector_path == archive
    assert str(archive) not in result.evidence and str(case.selector) not in result.evidence
    assert result.evidence == before.evidence
    for expected, supplied in ((None, descriptor), (case.selected, {**descriptor, "sha256": "0" * 64})):
        with pytest.raises(activation_state.ActivationRefusal):
            read_sealed_baseline(case.root, package=case.package, expected_selection=expected,
                                 archived_selector=supplied)
    assert archive.read_bytes() == original and not case.selector.exists()
    with pytest.raises(activation_state.ActivationRefusal):
        read_sealed_baseline(case.root, package=case.package)  # No ambient archive fallback.


@pytest.mark.parametrize("baseline", ["ensure"], indirect=True)
def test_applied_publication_accepts_canonical_ancestor_ensure_rows(baseline):
    raw = json.loads(baseline.publications[1].read_bytes())
    assert any(row["ensure"] and row["status"] == "applied" for row in raw["journal"]["rows"])
    assert read_sealed_baseline(baseline.root, package=baseline.package).selection == baseline.selected


@pytest.mark.parametrize("baseline", ["ensure"], indirect=True)
@pytest.mark.parametrize("tamper", ["missing_target", "changed_target", "foreign_ensure"])
def test_publication_still_requires_exact_targets_and_only_canonical_ensures(baseline, tamper):
    path = baseline.publications[1]
    raw = json.loads(path.read_bytes())
    rows = raw["journal"]["rows"]
    target = next(row for row in rows if not row["ensure"])
    if tamper == "missing_target":
        rows.remove(target)
    elif tamper == "changed_target":
        target["after"]["sha256"] = "0" * 64
    else:
        ensure = deepcopy(next(row for row in rows if row["ensure"]))
        ensure["target"] = str(baseline.root / "unrelated-directory")
        ensure["parent"] = str(baseline.root)
        ensure["before"]["resolved"] = ensure["target"]
        rows.append(ensure)
    _save_journal(path, raw)
    with pytest.raises(activation_state.ActivationRefusal):
        read_sealed_baseline(baseline.root, package=baseline.package)


@pytest.mark.parametrize("baseline", ["dormant"], indirect=True)
def test_baseline_keeps_unenrolled_frozen_bot_row_without_native_authority(baseline):
    result = read_sealed_baseline(baseline.root, package=baseline.package)
    assert {row["bot"] for row in result.units} == {"manager", "worker"}
    assert next(row for row in result.units if row["bot"] == "worker")["enroll"] is False
    assert [declaration.bot for declaration in result.declarations] == ["manager", "worker"]
    assert len(result.placements) == 1
    assert result.placements[0]["source"] == str(result.declarations[0].source)


@pytest.mark.parametrize("kind", ["upgrade", "adoption"])
def test_terminal_early_abort_admits_only_original_untouched_parking(baseline, kind):
    history = _aborted_history(baseline, kind)
    before = _files(baseline.root)
    result = read_sealed_baseline(baseline.root, package=baseline.package)
    assert result.selection == baseline.selected and _files(baseline.root) == before
    assert json.loads(history.journals["bots"].read_bytes())["journal"]["status"] == "prepared"
    assert json.loads(history.activation.read_bytes())["record"]["handoff_effects"] == history.body["handoff_effects"]


@pytest.mark.parametrize("tamper", ["progress", "receipt", "parent"])
def test_prepared_parking_without_exact_terminal_abort_proof_is_refused(baseline, tamper):
    history = _aborted_history(baseline, "upgrade")
    if tamper == "progress":
        path = history.journals["bots"]
        raw = json.loads(path.read_bytes())
        raw["journal"]["rows"][0]["status"] = "installing"
        _save_journal(path, raw)
    else:
        if tamper == "receipt":
            history.body["upgrade_abort"]["result"]["evidence"] = "0" * 64
        else:
            history.body["intent"]["enrollment_digest"] = "0" * 64
        history.activation.write_text(json.dumps({"record": history.body, "sha256": activation_state._digest(history.body)}))
    before = _files(baseline.root)
    with pytest.raises(activation_state.ActivationRefusal):
        read_sealed_baseline(baseline.root, package=baseline.package)
    assert _files(baseline.root) == before


def _pending_history(case):
    """Sanitized Pi capture shape: paused producers, parked bots, untouched ingest.

    The 2026-11-04 read-only rollout receipt retained a null per-bot stop result;
    this smaller roster retains that uncertainty without resuming its old owner.
    """
    from claudlobby.supervision_inventory import EnrollmentInventory, EnrolledUnit, FileSnapshot

    aid = "later-pending"
    original = read_sealed_baseline(case.root, package=case.package)
    units = []
    for declaration in original.declarations:
        entry = next(row for row in case.entries if row["source"] == str(declaration.source))
        content = case.plan.blob(entry["after"]["sha256"])
        for path in (declaration.source, Path(entry["installed"])):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        props = (("UnitFileState", "enabled"), ("LoadState", "loaded"), ("ActiveState", "active"))
        units.append(EnrolledUnit(declaration, entry["target"], FileSnapshot.read(declaration.source),
                                  (FileSnapshot.read(Path(entry["installed"])),), props))
    enrollment = EnrollmentInventory(case.root, "Linux", "", tuple(units), (), (), ()).payload()
    digest = _digest(enrollment)
    phases = {phase: [entry["target"] for entry in case.entries if entry["phase"] == phase]
              for phase in ("producers", "bots", "ingest")}
    shared = {"owner": "activation-units-v1", "activation_id": aid, "enrollment_digest": digest,
              "enrollment": enrollment, "phases": phases, "original_release_id": case.release.release_id,
              "native_observations": {}}
    journals = {}
    for phase in ("producers", "bots", "ingest"):
        builder = ConfigPlanBuilder(case.root, ("example",), effects={**shared, "phase": phase})
        for entry in case.entries:
            if entry["target"] in phases[phase]:
                builder.remove(Path(entry["installed"]))
        plan = historical_plan(builder, release_id=case.release.release_id, release_seal=case.release.seal_sha256)
        journals[phase] = _journal(plan, "units-" + _digest([aid, phase]),
                                   "prepared" if phase == "ingest" else "applied")
        if phase != "ingest":
            for entry in case.entries:
                if entry["target"] in phases[phase]:
                    Path(entry["installed"]).unlink()
    manifest = case.root / "fleet.yaml"
    manifest.write_text(manifest.read_text() + "\n# Retained pending configuration\n")
    builder = ConfigPlanBuilder(case.root, ("example",), effects=deepcopy(case.plan.effects))
    for sources in builder.effects["fleet_sources"].values():
        for descriptor in sources.values():
            descriptor["sha256"] = builder.input_content(Path(descriptor["path"]))
    for change in case.plan.changes:
        builder.file(Path(change.target), case.plan.blob(change.after["sha256"]), mode=change.after["mode"])
    pending = historical_plan(builder, release_id=case.release.release_id, release_seal=case.release.seal_sha256)
    config = _journal(pending, aid, "prepared")
    intent = {"release_id": case.release.release_id, "plan_id": pending.plan_id, "source_kind": "sealed",
              "source_release_id": case.release.release_id, "enrollment_digest": digest,
              "install_directory": str(original.install_directory), "quiesce_mode": "per-bot-v1", "preserve_bot_state": True}
    activation, body = write_activation(case.root, aid, status="activating", completed=["producers_paused"],
        pending="sessions_handed_off", intent=intent, previous_selection=case.selected,
        evidence={"producers_paused": "e" * 64}, quiesce_mode="per-bot-v1",
        handoff_effects={"com.example.manager.service": "handed_off", "com.example.worker.service": "handed_off"},
        bot_quiescence={"com.example.manager.service": {"result": {"quiet": True}},
                        "com.example.worker.service": {"result": None}}, start_effects={})
    return SimpleNamespace(activation=activation, body=body, plan=pending, journals=journals, config=config)


@pytest.mark.parametrize("baseline", ["pending"], indirect=True)
def test_pending_prepublication_baseline_binds_owned_images_and_preserves_unknown(baseline):
    history = _pending_history(baseline)
    before = _files(baseline.root)
    result = read_sealed_baseline(baseline.root, package=baseline.package)
    assert result.selection == baseline.selected and result.plan == baseline.plan
    assert result.authored_plan == history.plan
    assert result.interruption["activation_id"] == history.body["activation_id"]
    assert set(result.parked) == {entry["installed"] for entry in baseline.entries if entry["phase"] != "ingest"}
    for entry in baseline.entries:
        path = Path(entry["installed"])
        if entry["phase"] == "ingest":
            assert path.read_bytes() == baseline.plan.blob(entry["after"]["sha256"])
            continue
        parked = result.parked[str(path)]
        assert not path.exists() and parked["snapshot"]["sha256"] == entry["after"]["sha256"]
        blob = Path(parked["journal"]).parent / "blobs" / entry["after"]["sha256"]
        assert result.evidence[str(blob)] == hashlib.sha256(blob.read_bytes()).hexdigest()
    assert history.body["bot_quiescence"]["com.example.worker.service"]["result"] is None
    assert _files(baseline.root) == before


@pytest.mark.parametrize("baseline", ["pending"], indirect=True)
@pytest.mark.parametrize("defect", ["publication", "extra_owner", "authored", "blob", "reappeared"])
def test_pending_prepublication_conversion_refuses_other_effects_without_repair(baseline, defect):
    history = _pending_history(baseline)
    if defect == "publication":
        history.body["completed"].append("selection_switched")
        history.activation.write_text(json.dumps({"record": history.body, "sha256": activation_state._digest(history.body)}))
    elif defect == "extra_owner":
        write_activation(baseline.root, "another-pending", status="activating", pending="sessions_handed_off")
    elif defect == "authored":
        path = baseline.root / "fleet.yaml"
        path.write_text(path.read_text() + "# Unexplained drift\n")
    elif defect == "blob":
        journal = history.journals["bots"]
        raw = json.loads(journal.read_bytes())["journal"]
        digest = next(row["before"]["node"]["sha256"] for row in raw["rows"] if not row["ensure"])
        (journal.parent / "blobs" / digest).write_bytes(b"foreign before image")
    else:
        Path(baseline.entries[0]["installed"]).write_bytes(b"reappeared native file")
    before = _files(baseline.root)
    with pytest.raises(activation_state.ActivationRefusal):
        read_sealed_baseline(baseline.root, package=baseline.package)
    assert _files(baseline.root) == before
