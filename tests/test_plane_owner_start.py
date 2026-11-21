"""Starting a paired, linked computer with no team, through `owner_start.start`.

The computer is tests/no_team_start.py's: the updater's own stages run, and only
the native layer is a stand-in. Consent is the typed guide's: an object whose
`approve` returns for yes and raises for no.
"""

from __future__ import annotations

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
    """The typed guide's consent: `approve` returns for yes and raises for no."""

    def __init__(self, answer=True):
        self.answer, self.calls = answer, []

    def approve(self, action, scope, *, expires_at=None, **_kwargs):
        self.calls.append((action, scope))
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
        proposal = linked.stage(
            WorkspaceAttestation(
                ISSUER,
                "00000000-0000-4000-8000-000000000001",
                "00000000-0000-4000-8000-000000000002",
                owner.host_uid,
                challenge.nonce,
                ORIGIN,
                now,
                now + 120,
                "00000000-0000-4000-8000-000000000003",
            ),
            OWNER,
        )
        linked.confirm(expected_proposal=proposal)
        assert linked.status()[0] == "active"
    return owner


def _computer(tmp_path, monkeypatch, *, owner=None, workspace=True):
    case = Case(tmp_path, monkeypatch, owner=owner)
    _paired(case, workspace=workspace)
    return case, Paths(case.root, package=case.package(case.target))


def _start(case, paths, consent, **kwargs):
    return owner_start.start(
        paths, case.access, None, consent=consent, directory=case.directory, **kwargs
    )


def _files(root):
    # A SQLite reader may leave its shared-memory sidecars; nothing else may change.
    return sorted(
        (path, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(("-shm", "-wal"))
    )


def _units(directory):
    """Rendered resident services and scheduled jobs, by native label."""
    names = {
        path.name
        for path in directory.iterdir()
        if path.is_file() and "@." not in path.name
    }
    timers = {name.removesuffix(".timer") for name in names if name.endswith(".timer")}
    services = {
        name.removesuffix(".service") for name in names if name.endswith(".service")
    } - timers
    return sorted(services), sorted(timers)


def _settings(case):
    return case.settings.read_bytes() if case.settings.exists() else None


@pytest.mark.parametrize("owner", [None, "off"])
def test_a_declined_start_leaves_nothing(tmp_path, monkeypatch, owner):
    case, paths = _computer(tmp_path, monkeypatch, owner=owner)
    before, settings = _files(case.root), _settings(case)
    with pytest.raises(CommandFailure, match="declined"):
        _start(case, paths, Consent(answer=False))
    assert _files(case.root) == before and _settings(case) == settings
    state = case.root / "state"
    assert not (state / "update.json").exists() and not (state / "config.json").exists()
    assert not (state / "plane/plane.db").exists() and not list(
        case.directory.iterdir()
    )
    assert start_state(case.root, case.installation) == "fresh"


@pytest.mark.parametrize("owner", [None, "on", "off"])
def test_the_card_lists_what_the_start_installs(tmp_path, monkeypatch, owner):
    case, paths = _computer(tmp_path, monkeypatch, owner=owner)
    consent = Consent()
    state, ids = _start(case, paths, consent)
    assert state == "starting"
    ((action, scope),) = consent.calls
    assert action == "start_workspace"
    case.run_job()
    assert start_state(case.root, case.installation) == "started"
    services, jobs = _units(case.directory)
    assert (scope["services"], scope["jobs"]) == (services, jobs)
    assert ("claudlobby-plane-owner" in services) is (owner == "on")
    assert scope["native_directory"] == str(case.directory)
    assert scope["database"] == str(case.root / "state/plane/plane.db")
    assert (case.root / "state/plane/plane.db").is_file()
    record = read_update(case.installation)
    assert ids == {
        "request_id": record["request_id"],
        "config_fingerprint": record["config_fingerprint"],
        "target": case.target,
    }
    assert record["initialization_scope"] == {
        "directory": str(case.directory),
        "manifests": [],
    }


def test_a_planned_settings_change_is_on_the_card_and_written_only_after_approval(
    tmp_path, monkeypatch
):
    from claudlobby.commands.host_owner_setup import _owner_override_plan
    from claudlobby.config import load_host_jobs

    case, paths = _computer(tmp_path, monkeypatch, owner="off")
    plan = _owner_override_plan(
        case.root,
        {
            "origin": "https://host.example.test",
            "tailscale": str(case.python),
            "tailscale_socket": None,
            "socket": str(case.root / "state/owner/owner.sock"),
            "workspace_issuer": ISSUER,
            "workspace_origin": ORIGIN,
        },
    )
    before = case.settings.read_bytes()
    with pytest.raises(CommandFailure, match="declined"):
        _start(case, paths, Consent(answer=False), owner_plan=plan)
    assert (
        case.settings.read_bytes() == before
        and load_host_jobs()["plane-owner"]["enroll"] is False
    )
    consent = Consent()
    assert _start(case, paths, consent, owner_plan=plan)[0] == "starting"
    ((_action, scope),) = consent.calls
    assert scope["settings"] == {
        "path": str(case.settings),
        "sha256": plan["binding"]["sha256"],
    }
    assert "claudlobby-plane-owner" in scope["services"]
    assert case.settings.read_bytes() == plan["content"]
    case.run_job()
    assert _units(case.directory) == (scope["services"], scope["jobs"])


def test_a_start_under_way_returns_the_same_identifiers_and_asks_nothing(
    tmp_path, monkeypatch
):
    case, paths = _computer(tmp_path, monkeypatch)
    state, ids = _start(case, paths, Consent())
    assert state == "starting"
    again = Consent()
    assert _start(case, paths, again) == ("starting", ids)
    assert again.calls == [] and len(case.dispatches) == 1


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
        record["health_errors"] = {
            "claudlobby-plane-daemon.service": "unit failed to start"
        }
        store.save(record)
    again = Consent()
    state, failure = _start(case, paths, again)
    assert state == "failed" and failure["request_id"] == ids["request_id"]
    assert f"host update {ids['request_id']} did not finish" in failure["line"]
    assert "claudlobby-plane-daemon.service: unit failed to start" in failure["line"]
    assert again.calls == [] and len(case.dispatches) == 1


def test_a_start_is_continued_only_by_its_own_request(tmp_path, monkeypatch):
    """Phase 2's `host update continue --expected-request` reaches this function."""
    case, paths = _computer(tmp_path, monkeypatch)
    _state, ids = _start(case, paths, Consent())
    with pytest.raises(UpdateError, match="no longer current"):
        updater.apply_or_continue(
            case.installation,
            expected_request_id="00000000-0000-4000-8000-0000000000ff",
        )
    result = updater.apply_or_continue(
        case.installation, expected_request_id=ids["request_id"]
    )
    assert result["update"]["request_id"] == ids["request_id"]
    case.run_job()
    assert start_state(case.root, case.installation) == "started"


@pytest.mark.parametrize("missing", ["pairing", "workspace"])
def test_a_start_needs_a_paired_and_linked_computer(tmp_path, monkeypatch, missing):
    case = Case(tmp_path, monkeypatch)
    if missing == "workspace":
        _paired(case, workspace=False)
    paths = Paths(case.root, package=case.package(case.target))
    consent = Consent()
    with pytest.raises(CommandFailure, match="pairing and linked workspace"):
        _start(case, paths, consent)
    assert consent.calls == [] and not (case.root / "state/update.json").exists()


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
        "a first-team operation is retained; inspect its original receipt and continue locally"
    )
    assert refused.value.data == {
        "request_id": read_update(case.installation)["request_id"]
    }


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
    with pytest.raises(Exception):
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
