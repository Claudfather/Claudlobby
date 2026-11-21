"""Starting a paired, linked computer with no team, through `owner_start.start`.

The computer is tests/no_team_start.py's: the updater's own stages run, and only
the native layer is a stand-in. Consent is the typed guide's: an object whose
`approve` returns for yes and raises for no.
"""

from __future__ import annotations

from pathlib import Path
import time

import pytest

from claudlobby import git_update as updater
from claudlobby.command_result import CommandFailure
from claudlobby.git_update import UpdateError, UpdateStore, read_update
from claudlobby.paths import Paths
from claudlobby.plane.owner_access import PrincipalRef
from claudlobby.plane.owner_source import start_state
from claudlobby.plane.workspace_assertion import WorkspaceAttestation
from claudlobby.plane import owner_start
from tests.no_team_start import Case
from tests.fixtures.current_runtime import current_runtime  # noqa: F401

OWNER = PrincipalRef("tailscale:controlplane.tailscale.com", "1001")
ISSUER = "https://accounts.example.test"
ORIGIN = "https://plane.example.test"


class Consent:
    """The typed guide's consent: `approve` returns for yes and raises for no.

    `during` runs while the card waits, before the answer.
    """

    def __init__(self, answer=True, during=None):
        self.answer, self.during, self.calls = answer, during, []

    def approve(self, action, scope, *, expires_at=None, **_kwargs):
        self.calls.append((action, scope))
        if self.during is not None:
            self.during()
        if not self.answer:
            raise CommandFailure("conflict", "the start was declined")


def _paired(case, *, workspace=True):
    access = case.access
    pairing = access.begin_pairing(OWNER)
    owner = access.confirm_pairing(pairing.token, expected_principal=OWNER)
    if workspace:
        linked = access.workspace()
        linked.initialize(expected_owner=owner)
        challenge = linked.begin(OWNER, issuer=ISSUER, operational_origin=ORIGIN)
        now = int(time.time())
        proposal = linked.stage(WorkspaceAttestation(
            ISSUER, "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002",
            owner.host_uid, challenge.nonce, ORIGIN, now, now + 120,
            "00000000-0000-4000-8000-000000000003"), OWNER)
        linked.confirm(expected_proposal=proposal)
        assert linked.status()[0] == "active"
    return owner


def _computer(tmp_path, monkeypatch, *, owner=None, workspace=True, **kwargs):
    case = Case(tmp_path, monkeypatch, owner=owner, **kwargs)
    _paired(case, workspace=workspace)
    return case, Paths(case.root, package=case.package(case.target))


def _start(case, paths, consent, **kwargs):
    kwargs.setdefault("directory", case.directory)
    return owner_start.start(paths, case.access, None, consent=consent, **kwargs)


def _files(root):
    # A SQLite reader may leave its shared-memory sidecars; nothing else may change.
    return sorted((path, path.stat().st_mtime_ns) for path in root.rglob("*")
                  if path.is_file() and not path.name.endswith(("-shm", "-wal")))


def _settings(case):
    return case.settings.read_bytes() if case.settings.exists() else None


def _untouched(case, before, settings):
    """Nothing was written: no record, configuration, database, unit or settings change."""
    state = case.root / "state"
    assert _files(case.root) == before and _settings(case) == settings
    assert not (state / "update.json").exists() and not (state / "config.json").exists()
    assert not (state / "plane/plane.db").exists() and not list(case.directory.iterdir())
    assert start_state(case.root, case.installation) == "fresh"


def _owner_plan(case, **config):
    from claudlobby.commands.host_owner_setup import _owner_override_plan

    return _owner_override_plan(case.root, {
        "origin": "https://host.example.test", "tailscale": str(case.python), "tailscale_socket": None,
        "socket": str(case.root / "state/owner/owner.sock"),
        "workspace_issuer": ISSUER, "workspace_origin": ORIGIN, **config})


@pytest.mark.parametrize("owner", [None, "off"])
def test_a_declined_start_leaves_nothing(tmp_path, monkeypatch, owner):
    case, paths = _computer(tmp_path, monkeypatch, owner=owner)
    before, settings = _files(case.root), _settings(case)
    with pytest.raises(CommandFailure, match="declined"):
        _start(case, paths, Consent(answer=False))
    _untouched(case, before, settings)


@pytest.mark.parametrize("owner,stub_extras", [(None, True), ("on", True), ("off", True), ("on", False)])
def test_the_card_lists_everything_the_start_installs(tmp_path, monkeypatch, owner, stub_extras):
    """Unit for unit, with the updater's own job definition published as a start publishes it."""
    case, paths = _computer(tmp_path, monkeypatch, owner=owner, real_job=True, stub_extras=stub_extras)
    consent = Consent()
    state, ids = _start(case, paths, consent)
    assert state == "starting"
    ((action, scope),) = consent.calls
    assert action == "start_workspace"
    case.run_job()
    assert start_state(case.root, case.installation) == "started"
    listed = ({name + ".service" for name in scope["services"]}
              | {name + suffix for name in scope["jobs"] for suffix in (".service", ".timer")}
              | {Path(scope["updater_job"]).name})
    assert {path.name for path in case.directory.iterdir() if path.is_file() and not path.is_symlink()} == listed
    wanted = [path for path in case.directory.glob("*.wants/*")]
    assert wanted and all(path.is_symlink() and path.resolve().name in listed for path in wanted)
    assert Path(scope["updater_job"]).parent == case.directory
    assert all(Path(path).is_file() for path in scope["installation_files"])
    assert (owner == "on") is ("claudlobby-plane-owner" in scope["services"])
    assert scope["native_directory"] == str(case.directory)
    assert scope["database"] == str(case.root / "state/plane/plane.db")
    assert (case.root / "state/plane/plane.db").is_file()
    assert scope["settings"]["path"] == str(case.settings)
    assert scope["settings"]["sha256"] == (owner_start._digest(case.settings) if owner else "absent")
    record = read_update(case.installation)
    assert ids == {"request_id": record["request_id"], "config_fingerprint": record["config_fingerprint"],
                   "target": case.target}
    assert record["initialization_scope"] == {"directory": str(case.directory), "manifests": []}


def test_a_planned_settings_change_is_on_the_card_and_written_only_after_approval(tmp_path, monkeypatch):
    from claudlobby.config import load_host_jobs

    case, paths = _computer(tmp_path, monkeypatch, owner="off", real_job=True)
    plan = _owner_plan(case)
    before = case.settings.read_bytes()
    with pytest.raises(CommandFailure, match="declined"):
        _start(case, paths, Consent(answer=False), owner_plan=plan)
    assert case.settings.read_bytes() == before and load_host_jobs()["plane-owner"]["enroll"] is False
    consent = Consent()
    assert _start(case, paths, consent, owner_plan=plan)[0] == "starting"
    ((_action, scope),) = consent.calls
    assert scope["settings"] == {"path": str(case.settings), "sha256": plan["binding"]["sha256"]}
    assert "claudlobby-plane-owner" in scope["services"]
    assert case.settings.read_bytes() == plan["content"]
    case.run_job()
    assert {path.name for path in case.directory.iterdir()} >= {
        name + ".service" for name in scope["services"]}


def test_settings_changed_while_the_card_waits_start_nothing(tmp_path, monkeypatch):
    case, paths = _computer(tmp_path, monkeypatch, owner="off")
    switched = b"host:\n  jobs:\n    disk-monitor:\n      enroll: false\n"
    with pytest.raises(CommandFailure, match="settings changed after approval"):
        _start(case, paths, Consent(during=lambda: case.settings.write_bytes(switched)))
    assert not (case.root / "state/update.json").exists() and not list(case.directory.iterdir())
    assert start_state(case.root, case.installation) == "fresh"


def test_a_start_under_way_returns_the_same_identifiers_and_asks_nothing(tmp_path, monkeypatch):
    case, paths = _computer(tmp_path, monkeypatch)
    state, ids = _start(case, paths, Consent())
    assert state == "starting"
    case.job_state = "running"
    again = Consent()
    assert _start(case, paths, again) == ("starting", ids)
    assert again.calls == [] and len(case.dispatches) == 1


def test_a_second_call_while_the_start_holds_its_lock_is_starting(tmp_path, monkeypatch):
    """Between preparation and dispatch a start holds the update lock; a second call waits for nothing."""
    case, paths = _computer(tmp_path, monkeypatch)
    with UpdateStore(case.installation) as store:
        record = updater._prepare_update_locked(store, target=case.target, initialization=case.scope,
                                                prepare_only=True)
        again = Consent()
        state, ids = _start(case, paths, again)
        assert (state, ids["request_id"]) == ("starting", record["request_id"])
        assert again.calls == [] and case.dispatches == []
    # With the lock free and no dispatch, that start died before dispatching.
    state, failure = _start(case, paths, Consent())
    assert (state, failure["request_id"]) == ("failed", record["request_id"])
    assert case.dispatches == []


def test_a_refused_dispatch_is_reported_with_its_request_and_then_reads_failed(tmp_path, monkeypatch):
    case, paths = _computer(tmp_path, monkeypatch)
    case.refuse_run = UpdateError("updater native launch was not acknowledged")
    with pytest.raises(CommandFailure, match="did not complete") as refused:
        _start(case, paths, Consent())
    record = read_update(case.installation)
    assert refused.value.data == {"request_id": record["request_id"]}
    assert "host update continue" in refused.value.error.hint
    assert record["job_dispatch"]["state"] == "pending" and case.dispatches == []
    again = Consent()
    state, failure = _start(case, paths, again)
    assert (state, failure["request_id"]) == ("failed", record["request_id"])
    assert again.calls == [] and case.dispatches == []


@pytest.mark.parametrize("job_state,expected", [("inactive", "failed"), ("failed", "failed"),
                                                ("running", "starting"), ("starting", "starting")])
def test_an_entered_job_is_read_from_its_native_state(tmp_path, monkeypatch, job_state, expected):
    case, paths = _computer(tmp_path, monkeypatch)
    _state, ids = _start(case, paths, Consent())
    with UpdateStore(case.installation) as store:
        record = store.load()
        record["job_dispatch"] = {**record["job_dispatch"], "state": "entered"}
        store.save(record)
    case.job_state = job_state
    state, detail = _start(case, paths, Consent())
    assert (state, detail["request_id"]) == (expected, ids["request_id"])
    assert len(case.dispatches) == 1


def test_a_second_card_approved_after_a_start_began_refuses_and_dispatches_nothing(tmp_path, monkeypatch):
    case, paths = _computer(tmp_path, monkeypatch)
    inner = []
    # While the first card waits, a second call is shown its own card, approved and run.
    first = Consent(during=lambda: inner.append(_start(case, paths, Consent())))
    with pytest.raises(CommandFailure, match="refuses an existing update operation"):
        _start(case, paths, first)
    ((state, ids),) = inner
    assert state == "starting" and case.dispatches == [ids["request_id"]]
    assert read_update(case.installation)["request_id"] == ids["request_id"]


def test_a_started_computer_returns_started_and_changes_nothing(tmp_path, monkeypatch):
    case, paths = _computer(tmp_path, monkeypatch)
    _start(case, paths, Consent())
    case.run_job()
    before = _files(case.root)
    again = Consent()
    assert _start(case, paths, again) == ("started", None)
    assert again.calls == [] and _files(case.root) == before


def test_a_stopped_start_returns_failed_with_its_request(tmp_path, monkeypatch):
    case, paths = _computer(tmp_path, monkeypatch)
    _state, ids = _start(case, paths, Consent())
    case.run_job()
    with UpdateStore(case.installation) as store:
        record = store.load()
        record["health_errors"] = {"claudlobby-plane-daemon.service": "unit failed to start"}
        store.save(record)
    again = Consent()
    state, failure = _start(case, paths, again)
    assert state == "failed" and failure["request_id"] == ids["request_id"]
    assert f"host update {ids['request_id']} did not finish" in failure["line"]
    assert "claudlobby-plane-daemon.service: unit failed to start" in failure["line"]
    assert again.calls == [] and len(case.dispatches) == 1


def test_the_updater_continues_a_start_only_by_its_own_request(tmp_path, monkeypatch):
    """Phase 2's `host update continue --expected-request` reaches apply_or_continue."""
    case, paths = _computer(tmp_path, monkeypatch)
    _state, ids = _start(case, paths, Consent())
    with pytest.raises(UpdateError, match="no longer current"):
        updater.apply_or_continue(case.installation, expected_request_id="00000000-0000-4000-8000-0000000000ff")
    result = updater.apply_or_continue(case.installation, expected_request_id=ids["request_id"])
    assert result["update"]["request_id"] == ids["request_id"]
    case.run_job()
    assert start_state(case.root, case.installation) == "started"


@pytest.mark.parametrize("missing", ["pairing", "workspace", "revoked pairing", "inactive workspace"])
def test_a_start_needs_an_active_pairing_and_linked_workspace(tmp_path, monkeypatch, missing):
    case = Case(tmp_path, monkeypatch)
    if missing != "pairing":
        owner = _paired(case, workspace=missing != "workspace")
        if missing == "revoked pairing":
            case.access.revoke_owner(expected_revision=owner.revision)
        elif missing == "inactive workspace":
            _state, binding = case.access.workspace().status()
            case.access.workspace().revoke(expected_binding=binding, expected_owner=owner)
            assert case.access.workspace().status()[0] != "active"
    paths = Paths(case.root, package=case.package(case.target))
    before, consent = _files(case.root), Consent()
    with pytest.raises(CommandFailure, match="pairing and linked workspace"):
        _start(case, paths, consent)
    assert consent.calls == [] and _files(case.root) == before


@pytest.mark.parametrize("retained", ["legacy_recovery_required", "allowed", "expired", "revoked", "launched"])
def test_a_computer_todays_guide_is_part_way_through_is_not_started(tmp_path, monkeypatch, retained):
    from claudlobby.plane.owner_setup import OwnerSetup, RetainedSetupState

    case, paths = _computer(tmp_path, monkeypatch)
    state = (RetainedSetupState("allowed", None, "00000000-0000-4000-8000-000000000009", "a" * 64)
             if retained == "launched" else RetainedSetupState(retained))
    monkeypatch.setattr(OwnerSetup, "retained_state", lambda self: state)
    before, consent = _files(case.root), Consent()
    with pytest.raises(CommandFailure, match="guided first-team setup is retained"):
        _start(case, paths, consent)
    assert consent.calls == [] and _files(case.root) == before


def test_planned_settings_for_another_workspace_or_file_are_refused(tmp_path, monkeypatch):
    case, paths = _computer(tmp_path, monkeypatch, owner="off")
    before, settings = _files(case.root), _settings(case)
    consent = Consent()
    with pytest.raises(CommandFailure, match="different workspace"):
        _start(case, paths, consent, owner_plan=_owner_plan(case, workspace_origin="https://other.example.test"))
    plan = _owner_plan(case)
    elsewhere = {**plan, "binding": {**plan["binding"], "override": str(case.settings.parent / "other.yaml")}}
    with pytest.raises(CommandFailure, match="not this computer's settings file"):
        _start(case, paths, consent, owner_plan=elsewhere)
    assert consent.calls == []
    _untouched(case, before, settings)


@pytest.mark.parametrize("where", ["absent", "outside the catalog"])
def test_the_native_directory_is_checked_before_the_card(tmp_path, monkeypatch, where):
    case, paths = _computer(tmp_path, monkeypatch)
    if where == "absent":
        case.directory.rmdir()
        directory = case.directory
    else:
        directory = tmp_path / "elsewhere"
        directory.mkdir()
    before, settings, consent = _files(case.root), _settings(case), Consent()
    with pytest.raises(CommandFailure, match="native directory"):
        _start(case, paths, consent, directory=directory)
    assert consent.calls == [] and _files(case.root) == before and _settings(case) == settings
    assert not (case.root / "state/update.json").exists()
    assert case.directory.exists() is (where != "absent")


def test_a_start_refuses_while_another_update_holds_the_lock(tmp_path, monkeypatch):
    case, paths = _computer(tmp_path, monkeypatch)
    with UpdateStore(case.installation):
        with pytest.raises(CommandFailure, match="did not complete") as refused:
            _start(case, paths, Consent())
    assert refused.value.data == {} and "host update status" in refused.value.error.hint
    assert not (case.root / "state/update.json").exists() and case.dispatches == []


def test_a_computer_with_a_team_is_not_started(current_runtime):
    root, installation, _, _host = current_runtime
    from claudlobby.resources import get_resources
    from dataclasses import replace

    paths = Paths(root, package=replace(get_resources(), installation=installation))
    consent = Consent()
    with pytest.raises(CommandFailure, match="only a fresh computer"):
        owner_start.start(paths, None, None, consent=consent, directory=root / "units")
    assert consent.calls == []


def test_todays_guide_still_refuses_a_computer_it_did_not_start(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from claudlobby import resources
    from claudlobby.commands import host_owner_setup

    case, paths = _computer(tmp_path, monkeypatch)
    _start(case, paths, Consent())
    case.run_job()
    monkeypatch.setattr(resources, "get_resources", lambda **_kwargs: paths.package)
    with pytest.raises(CommandFailure) as refused:
        host_owner_setup.run(SimpleNamespace(), paths, case.access, None)
    assert refused.value.error.code == "conflict"
    assert str(refused.value.error.message) == (
        "a first-team operation is retained; inspect its original receipt and continue locally")
    assert refused.value.data == {"request_id": read_update(case.installation)["request_id"]}


def _job_ended(case):
    """What the updater job's own wrapper records when its run ends, finished or not."""
    with UpdateStore(case.installation) as store:
        record = store.load()
        record["job_dispatch"] = {**record["job_dispatch"], "state": "finished"}
        store.save(record)


def test_a_start_interrupted_after_its_database_is_continued_by_its_request(tmp_path, monkeypatch):
    from claudlobby import update_operation

    case, paths = _computer(tmp_path, monkeypatch)
    _state, ids = _start(case, paths, Consent())
    identities = update_operation._initialize_identities
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("synthetic interruption after the database is published")
        return identities(*args, **kwargs)

    monkeypatch.setattr(update_operation, "_initialize_identities", interrupted)
    with pytest.raises(OSError, match="synthetic interruption"):
        case.run_job()
    _job_ended(case)
    database = case.root / "state/plane/plane.db"
    published = database.stat().st_ino
    assert start_state(case.root, case.installation) == "starting"
    again = Consent()
    state, failure = _start(case, paths, again)
    assert (state, failure["request_id"]) == ("failed", ids["request_id"])
    assert again.calls == [] and len(case.dispatches) == 1
    # Phase 2's `host update continue --expected-request <id>`, with no card.
    updater.apply_or_continue(case.installation, expected_request_id=ids["request_id"])
    case.run_job()
    assert start_state(case.root, case.installation) == "started"
    assert database.stat().st_ino == published and len(calls) == 2
    assert read_update(case.installation)["request_id"] == ids["request_id"]


def test_a_start_done_with_health_errors_is_cleared_by_its_request(tmp_path, monkeypatch):
    case, paths = _computer(tmp_path, monkeypatch)
    _state, ids = _start(case, paths, Consent())
    case.run_job()
    with UpdateStore(case.installation) as store:
        record = store.load()
        record["health_errors"] = {"claudlobby-plane-daemon.service": "unit failed to start"}
        store.save(record)
    assert _start(case, paths, Consent())[0] == "failed"
    updater.apply_or_continue(case.installation, expected_request_id=ids["request_id"])
    case.run_job()
    assert start_state(case.root, case.installation) == "started"
    assert _start(case, paths, Consent()) == ("started", None)
