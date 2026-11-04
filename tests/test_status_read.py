"""Public status routes retain native observations and Plane uncertainty."""

from types import SimpleNamespace

import pytest

from claudlobby.command_result import CommandFailure
from claudlobby.commands import status_read
from claudlobby.status import BotStatus


def test_fleet_and_bot_status_keep_native_and_plane_facts_separate(active, monkeypatch):
    from claudlobby.commands import orientation
    from claudlobby import activation_state, status, switches

    root, host = active
    monkeypatch.setattr(orientation, "_context", lambda args: pytest.fail("pending authored context read"))
    monkeypatch.setattr(activation_state, "read_selection", lambda root: pytest.fail("historical selection read"))
    resolved, scopes = [], []
    monkeypatch.setattr(switches, "resolve", lambda paths, fleet: resolved.append(1) or [])

    def _collect(fleet, paths, only=None):
        scopes.append(only)
        return [BotStatus(name="worker", service_active=True, service_sub="running",
                          tmux_alive=True, plane_unreachable="Plane unavailable")]
    monkeypatch.setattr(status, "collect_fleet_status", _collect)

    fleet = status_read.dispatch(SimpleNamespace(root=root, fleet=None, seed=False, public_command="fleet.status"))
    assert scopes == [None] and len(resolved) == 1
    bot = status_read.dispatch(SimpleNamespace(root=root, fleet=None, seed=False, public_command="bot.status", bot_id="worker"))
    # one bot: only its own probes, and no fleet switch header it never renders
    assert scopes == [None, "worker"] and len(resolved) == 1
    for row in (fleet.data["bots"][0], bot.data["bot"]):
        assert row["service_active"] is True and row["tmux_alive"] is True
        assert row["plane_unreachable"] == "Plane unavailable"
        assert row["pane_state"] is None and row["state"] == "unknown"
    assert fleet.release_id is None

    def unreadable(*args):
        raise RuntimeError("private diagnostic must not be echoed")
    monkeypatch.setattr(switches, "resolve", unreadable)
    failed = status_read.dispatch(SimpleNamespace(root=root, fleet=None, seed=False, public_command="fleet.status"))
    assert failed.data["switches_state"] == "unavailable"
    assert "switches_off" not in failed.data
    assert "switch states unavailable" in "\n".join(failed.lines).lower()
    assert "host doctor --switches" in "\n".join(failed.lines)
    assert "private diagnostic" not in str(failed)

    unknown = switches.SwitchState(switches.by_key("task-recheck"), True, "?",
                                   unknown=True, unknown_reason="resolver",
                                   detail=switches.RESOLVER_DETAIL)
    monkeypatch.setattr(switches, "resolve", lambda *args: [unknown])
    unobserved = status_read.dispatch(SimpleNamespace(root=root, fleet=None, seed=False, public_command="fleet.status"))
    assert unobserved.data["switches_state"] == "unknown"
    assert unobserved.data["switches_off"] == []
    assert unobserved.data["switches_unknown"] == [{"key": "task-recheck", "reason": "resolver"}]
    assert "task-recheck" in "\n".join(unobserved.lines)
    assert "unknown" in "\n".join(unobserved.lines)


def test_uptime_refuses_unavailable_plane_before_reporting_zero(active, monkeypatch):
    from claudlobby.commands import orientation
    from claudlobby import activation_state, brief, source_state

    root, host = active
    monkeypatch.setattr(orientation, "_context", lambda args: pytest.fail("pending authored context read"))
    monkeypatch.setattr(activation_state, "read_selection", lambda root: pytest.fail("historical selection read"))
    monkeypatch.setattr(source_state, "scan_dir", lambda path: (SimpleNamespace(reachable=True), []))
    monkeypatch.setattr(brief, "plane_session", lambda paths, fleet: (None, "database unavailable"))
    with pytest.raises(CommandFailure) as raised:
        status_read.dispatch(SimpleNamespace(root=root, fleet=None, seed=False, public_command="fleet.uptime", bot=None, window="24h"))
    assert raised.value.error.code == "unavailable"


@pytest.mark.parametrize("manager", ["Linux", "Darwin"])
@pytest.mark.parametrize("evidence", ["complete", "unknown", "wrong_placement"])
def test_git_native_status_uses_exact_inventory_placement_and_preserves_unknown(tmp_path, monkeypatch, manager, evidence):
    from dataclasses import replace
    import os
    from claudlobby import active_config, config_units, status, supervision_inventory
    from claudlobby.installation import Installation
    from tests.package_fixtures import source_package
    installation = Installation(tmp_path / "installation", tmp_path / "data")
    package = replace(source_package(), installation=installation, source_revision="a" * 40)
    paths = SimpleNamespace(root=installation.data_root, package=package)
    directory = tmp_path / "native"
    source = tmp_path / ("bot.service" if manager == "Linux" else "bot.plist")
    declaration = SimpleNamespace(fleet="test", bot="worker", scope="bot", source=source)
    domain = f"user/{os.getuid()}" if manager == "Darwin" else ""
    snapshot = {"supervision": {"manager": manager, "directory": str(directory), "domain": domain}}
    monkeypatch.setattr(active_config, "read_current_config", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(config_units, "current_git_declarations", lambda *args: (declaration,))
    unit = SimpleNamespace(declaration=declaration, installed=(SimpleNamespace(path=str(directory / source.name)),),
                           target=domain + "/bot" if domain else source.name,
                           properties=(("ActiveState", "active"),))
    if evidence == "wrong_placement":
        if manager == "Darwin":
            unit.target = f"gui/{os.getuid()}/bot"
        else:
            unit.installed = (SimpleNamespace(path=str(tmp_path / "other" / source.name)),)
    inventory = SimpleNamespace(manager=manager, units=(unit,))
    def collect(*args, **kwargs):
        assert kwargs["only_names"] == frozenset({source.name})
        if evidence == "unknown":
            raise supervision_inventory.InventoryError("native binding unverified")
        return SimpleNamespace(require_complete=lambda: inventory)
    monkeypatch.setattr(supervision_inventory, "collect_enrollment", collect)
    observed = status._current_service_observations(SimpleNamespace(name="test"), paths, ["worker"])
    assert observed["worker"] == ((True, "active", True) if evidence == "complete" else (False, status._SVC_UNDETERMINED, None))

from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401 — private selected root
from tests.test_releases import installed  # noqa: F401 — cold dependency
from tests.test_task_read_cli import active  # noqa: F401 — real current fleet identities


def test_git_status_and_doctor_use_committed_fleet_not_authored_or_historical_selection(active, monkeypatch):
    from claudlobby import doctor, status
    from claudlobby.commands import host_doctor, orientation
    from tests.test_task_read_cli import _use_git_current
    root, package, snapshot = _use_git_current(active, monkeypatch)
    # Pending authoring and a broken historical selection cannot define this read.
    (root / "fleet.yaml").write_text("not valid fleet YAML: [\n")
    monkeypatch.setattr(orientation, "_context", lambda *args: pytest.fail("authored status context used"))
    monkeypatch.setattr(status, "_check_tmux_sessions", lambda *args: {"worker"})
    monkeypatch.setattr(status, "_check_systemd_service", lambda *args: pytest.fail("legacy native target used"))
    monkeypatch.setattr(status, "_check_launchd_service", lambda *args: pytest.fail("guessed launchd domain used"))
    monkeypatch.setattr(status, "_live_context", lambda *args: None)
    args = SimpleNamespace(root=root, fleet=None, seed=False, public_command="fleet.status")
    result = status_read.dispatch(args)
    assert {row["name"] for row in result.data["bots"]} == {"manager", "worker"}
    assert all(row["service_undetermined"] for row in result.data["bots"])
    assert result.release_id is None and result.data["source_revision"] == snapshot["revision"]
    assert result.data["running_source_revision"] is None
    checked = []
    monkeypatch.setattr(doctor, "run_doctor", lambda fleet, paths, **kwargs:
                        checked.append(set(fleet.bots)) or doctor.DoctorReport())
    monkeypatch.setattr("claudlobby.commands._helpers._load_env", lambda *args: None)
    diagnosed = host_doctor.dispatch(SimpleNamespace(root=root, fleet=None, seed=False,
                                      markdown=False, switches=False, delivery=False))
    assert checked == [{"manager", "worker"}]
    assert diagnosed.release_id is None and diagnosed.data["config_fingerprint"] == snapshot["fingerprint"]
    assert diagnosed.data["running_source_revision"] is None
    from claudlobby.active_config import load_active_context
    from claudlobby import activation_state
    monkeypatch.setattr(activation_state, "read_selection", lambda *args: pytest.fail("historical selection used"))
    provenance = orientation._release(load_active_context(root, "example", package=package))
    assert provenance["source_revision"] == snapshot["revision"]
    assert provenance["selected_release_id"] is None and provenance["running_source_revision"] is None
    assert provenance["running_release"] == "unknown"


def test_git_doctor_unknown_native_evidence_never_passes_services(tmp_path, monkeypatch):
    from claudlobby import doctor, status
    paths = SimpleNamespace(package=SimpleNamespace(installation=object()))
    fleet = SimpleNamespace(bots={"worker": object()})
    monkeypatch.setattr(status, "_current_service_observations", lambda *args:
                        {"worker": (False, status._SVC_UNDETERMINED, None)})
    monkeypatch.setattr(status, "_check_tmux_sessions", lambda *args: {"worker"})
    report = doctor.DoctorReport()
    doctor.check_services(fleet, paths, report)
    assert not report.passed and len(report.warnings) == 1
    assert "unverified" in report.warnings[0].detail


@pytest.mark.parametrize("enrolled, live, stopped", [
    (False, False, True), (None, False, False), (True, False, False), (False, True, False),
])
def test_git_status_stop_record_requires_observed_deenrollment_and_no_live_session(active, monkeypatch,
                                                                                enrolled, live, stopped):
    from claudlobby import active_config, brief, status
    from claudlobby.stop_record import write_stop
    from tests.test_task_read_cli import _use_git_current
    root, package, _ = _use_git_current(active, monkeypatch)
    context = active_config.load_active_context(root, "example", package=package)
    bot_dir = context.paths.bot_runtime("worker")
    bot_dir.mkdir(parents=True, exist_ok=True)
    record = write_stop(bot_dir, by="operator", request_id="retained-stop",
                        reason="Keep this bot stopped")
    monkeypatch.setattr(status, "_current_service_observations", lambda *args:
                        {"worker": (False, status._SVC_UNDETERMINED if enrolled is None else "inactive", enrolled)})
    monkeypatch.setattr(status, "_check_tmux_sessions", lambda *args: {"worker"} if live else set())
    monkeypatch.setattr(status, "_read_service_label", lambda *args: pytest.fail("mutable bot.conf read"))
    monkeypatch.setattr(status, "unit_installed", lambda *args, **kwargs: pytest.fail("legacy unit inference"))
    monkeypatch.setattr(status, "_live_context", lambda *args: None)
    monkeypatch.setattr(brief, "plane_session", lambda *args: (None, "not observed in this test"))
    row, = status.collect_fleet_status(context.fleet, context.paths, only="worker")
    assert (row.state == "stopped") is stopped
    if stopped:
        assert (row.stopped_since, row.stopped_by, row.stop_reason) == (
            record["stopped_at"], record["by"], record["reason"])
    else:
        assert (row.stopped_since, row.stopped_by, row.stop_reason) == (None, None, None)


@pytest.mark.parametrize("manager, state, live, healthy", [
    ("Linux", "active", True, True), ("Linux", "inactive", True, False),
    ("Darwin", "active", True, True), ("Darwin", "inactive", True, True),
    ("Darwin", "inactive", False, False),
])
def test_git_doctor_accepts_only_live_darwin_exited_launcher(monkeypatch, manager, state, live, healthy):
    import platform
    from claudlobby import doctor, status
    paths = SimpleNamespace(package=SimpleNamespace(installation=object()))
    fleet = SimpleNamespace(bots={"worker": object()})
    monkeypatch.setattr(platform, "system", lambda: manager)
    monkeypatch.setattr(status, "_current_service_observations", lambda *args:
                        {"worker": (state == "active", state, True)})
    monkeypatch.setattr(status, "_check_tmux_sessions", lambda *args: {"worker"} if live else set())
    report = doctor.DoctorReport()
    doctor.check_services(fleet, paths, report)
    assert bool(report.passed) is healthy
    assert report.has_failures is not healthy
    assert not report.warnings


def test_git_status_refuses_current_config_change_during_native_read(active, monkeypatch):
    from copy import deepcopy
    from claudlobby import active_config, status
    from tests.test_task_read_cli import _use_git_current
    root, package, snapshot = _use_git_current(active, monkeypatch)
    def raced(*args, **kwargs):
        changed = deepcopy(snapshot)
        changed["fleets"]["example"]["fleet"] += "\n# Next committed configuration\n"
        changed["fingerprint"] = active_config._fingerprint(changed)
        active_config.publish_current_config(changed, installation=package.installation,
                                             expected_previous=snapshot["fingerprint"])
        return [BotStatus(name="worker")]
    monkeypatch.setattr(status, "collect_fleet_status", raced)
    monkeypatch.setattr("claudlobby.switches.resolve", lambda *args: [])
    with pytest.raises(CommandFailure) as failure:
        status_read.dispatch(SimpleNamespace(root=root, fleet=None, seed=False, public_command="fleet.status"))
    assert failure.value.error.retryable
    assert failure.value.data["config_fingerprint"] == snapshot["fingerprint"]
