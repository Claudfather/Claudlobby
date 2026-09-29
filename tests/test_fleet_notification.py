"""Selected fleet signals retain scope and send during recorder failure."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from claudlobby import fleet_notification as operation
from claudlobby.command_result import CommandFailure
from claudlobby.commands import fleet_notify
from claudlobby.plane.emit_api import validate_item
from claudlobby.plane.migrations import DowngradeError
from claudlobby.plane.schema_state import PendingMigrationError
from claudlobby.recording_alerts import send_fleet_notification
from tests.plane_setup import initialize_plane
from tests.package_fixtures import source_package
from tests.test_recording_alerts import AT, REQUEST, private_alert  # noqa: F401


def test_public_notification_refuses_worker_and_foreign_fleet_before_effect(
        private_alert, monkeypatch):  # noqa: F811
    context, package, manager, _ = private_alert
    monkeypatch.setattr(operation, "resolve_paths", lambda **_: SimpleNamespace(root=context.paths.root))
    origin = SimpleNamespace(fleet=context.fleet, bot_id="worker")
    admitted_now = [False]
    @contextmanager
    def admitted(*args, **kwargs):
        admitted_now[0] = True
        try:
            yield SimpleNamespace(release_id="selected", native_path=package.native)
        finally:
            admitted_now[0] = False
    monkeypatch.setattr(operation, "mutation_admission", admitted)
    def scope(**kwargs):
        assert admitted_now[0], "selected fleet and manager must be read under admission"
        return context, origin
    monkeypatch.setattr(operation, "resolve_operation_scope", scope)
    with pytest.raises(operation.FleetNotificationError, match="manager"):
        operation.notify_fleet(root=context.paths.root, fleet="fleet", level="alert",
                               event="disk_high", message="Disk nearly full", identity=object())
    manager_origin = SimpleNamespace(fleet=context.fleet, bot_id="manager")
    monkeypatch.setattr(operation, "resolve_operation_scope", lambda **_: (context, manager_origin))
    monkeypatch.delenv("CLAUDLOBBY_RELEASE_ID", raising=False)
    with pytest.raises(operation.FleetNotificationError, match="bound selected release"):
        operation.notify_fleet(root=context.paths.root, fleet="fleet", level="alert",
                               event="disk_high", message="Disk nearly full", identity=object())
    foreign = SimpleNamespace(fleet=SimpleNamespace(name="foreign"), bot_id="manager")
    monkeypatch.setattr(operation, "resolve_operation_scope", lambda **_: (context, foreign))
    with pytest.raises(operation.FleetNotificationError, match="manager"):
        operation.notify_fleet(root=context.paths.root, fleet="fleet", level="alert",
                               event="disk_high", message="Disk nearly full", identity=object())


@pytest.mark.parametrize("failure", [OSError, PendingMigrationError, DowngradeError])
def test_recording_outage_still_uses_exact_configured_carriers(private_alert, monkeypatch, failure):  # noqa: F811
    context, package, manager, tier = private_alert
    monkeypatch.setattr(operation, "resolve_paths", lambda **_: SimpleNamespace(root=context.paths.root))
    monkeypatch.setattr(operation, "resolve_operation_scope", lambda **_: (context, None))
    @contextmanager
    def admitted(*args, **kwargs):
        yield SimpleNamespace(release_id="selected", native_path=package.native)
    monkeypatch.setattr(operation, "mutation_admission", admitted)
    monkeypatch.setattr(operation, "read_selection", lambda _: {"release_id": "selected"})
    monkeypatch.setattr(operation, "_transport", lambda *_: manager)
    monkeypatch.setattr(operation, "resolve_tiers", lambda *a, **k: {
        key: SimpleNamespace(value=value) for key, value in tier.items()})
    def recorder(*args, **kwargs):
        validate_item(args[1][0], {})
        assert args[1][0]["fleet"] == "fleet"
        assert args[1][0]["payload"]["event"] == "fleet_alert"
        assert args[1][0]["source_ref"].startswith("fleet-events:sha:")
        assert args[1][0]["payload"]["data"]["data"]["message"] == "Disk nearly full"
        assert kwargs["require_commit"] is True
        raise failure("recorder unavailable")
    result = operation.notify_fleet(root=context.paths.root, fleet="fleet", level="alert",
                                    event="disk_high", message="Disk nearly full", identity=object(),
                                    emit=recorder)
    assert result.recording == "unknown"
    assert (result.manager.status, result.telegram.status) == ("submitted", "carrier_accepted")
    assert "FLEET ALERT [disk_high]: Disk nearly full" in (
        context.paths.root / "manager-capture").read_text()
    assert "FLEET ALERT [disk_high]: Disk nearly full" in (
        context.paths.root / "telegram-capture").read_text()


def test_unconfigured_telegram_is_disclosed_without_ambient_fallback(private_alert, monkeypatch):  # noqa: F811
    context, package, manager, _ = private_alert
    monkeypatch.setenv("TELEGRAM_GROUP_CHAT_ID", "-1009999999999")
    result = send_fleet_notification(context, package, manager, message="FLEET NOTICE [test]: ready",
                                     event="test", request_id=REQUEST, at=AT, trusted_tiers={})
    assert result.manager.status == "submitted"
    assert result.telegram.status == "unconfigured"
    assert not (context.paths.root / "telegram-capture").exists()


def test_committed_notify_event_is_visible_to_selected_fleet_reader(private_alert, monkeypatch):  # noqa: F811
    from claudlobby.paths import load_lib_module
    from claudlobby.plane.db import connect_ro, db_file
    from claudlobby.recording_alerts import ChannelOutcome, RecordingAlertOutcome

    context, package, manager, _ = private_alert
    initialize_plane(context.paths.root)
    monkeypatch.setattr(operation, "resolve_paths", lambda **_: SimpleNamespace(root=context.paths.root))
    monkeypatch.setattr(operation, "resolve_operation_scope", lambda **_: (context, None))
    monkeypatch.setattr(operation, "read_selection", lambda _: {"release_id": "selected"})
    monkeypatch.setattr(operation, "_transport", lambda *_: manager)
    monkeypatch.setattr(operation, "resolve_tiers", lambda *a, **k: {})
    @contextmanager
    def admitted(*args, **kwargs):
        yield SimpleNamespace(release_id="selected", native_path=package.native)
    monkeypatch.setattr(operation, "mutation_admission", admitted)
    sent = RecordingAlertOutcome(ChannelOutcome("submitted", True),
                                 ChannelOutcome("unconfigured", None))
    result = operation.notify_fleet(root=context.paths.root, fleet="fleet", level="alert",
                                    event="disk_high", message="Disk nearly full",
                                    identity=object(), send=lambda *a, **k: sent)
    assert result.recording == "committed"
    reader = load_lib_module(source_package().native, "plane-readers.py")
    with connect_ro(db_file(context.paths.root)) as conn:
        rows = reader.fleet_events(conn, "fleet", event_id=result.event_id)
    assert len(rows) == 1
    assert reader.public(rows[0])["data"]["message"] == "Disk nearly full"


def test_no_channel_and_unverified_recording_are_distinct_results(private_alert, monkeypatch):  # noqa: F811
    context, package, manager, _ = private_alert
    (context.paths.root / "manager-fails").touch()
    channels = send_fleet_notification(context, package, manager,
                                       message="FLEET ALERT [test]: check", event="test",
                                       request_id=REQUEST, at=AT, trusted_tiers={})
    assert (channels.manager.status, channels.telegram.status) == ("failed", "unconfigured")
    def result(recording):
        return operation.FleetNotificationResult("fleet", "release", "ev_" + "a" * 32,
                                                 REQUEST, "alert", "test", recording,
                                                 channels.manager, channels.telegram)
    args = SimpleNamespace(seed=False, root=context.paths.root, fleet="fleet", level="alert",
                           event="test", message="check")
    monkeypatch.setattr(operation, "notify_fleet", lambda **_: result("committed"))
    with pytest.raises(CommandFailure) as missing:
        fleet_notify.dispatch(args)
    assert missing.value.error.code == "notification_failed"
    assert missing.value.data["telegram"]["status"] == "unconfigured"
    monkeypatch.setattr(operation, "notify_fleet", lambda **_: result("unknown"))
    with pytest.raises(CommandFailure) as degraded:
        fleet_notify.dispatch(args)
    assert degraded.value.error.code == "recording_degraded"
    assert degraded.value.data["recording"] == "unknown"
