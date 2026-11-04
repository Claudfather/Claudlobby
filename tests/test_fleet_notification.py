"""Selected fleet signals retain scope and send during recorder failure."""

from contextlib import contextmanager
from dataclasses import replace
import shlex
from types import SimpleNamespace

import pytest

from claudlobby import fleet_notification as operation
from claudlobby.command_result import CommandFailure
from claudlobby.commands import fleet_notify
from claudlobby.composer import compose_fleet_timers
from claudlobby.config import FLEET_PULSE_ENV_KEYS, FleetPulseConfig, load_fleet
from claudlobby.plane.emit_api import validate_item
from claudlobby.plane.migrations import DowngradeError
from claudlobby.plane.schema_state import PendingMigrationError
from claudlobby.recording_alerts import (clear_recording_degraded, notify_recording_degraded,
                                         send_fleet_notification)
from tests.plane_setup import initialize_plane
from tests.package_fixtures import source_package
from tests.test_fleet_pulse_knobs import _make_paths, _write
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
    with pytest.raises(CommandFailure, match="bound release"):
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
    context = replace(context, fleet=replace(context.fleet, fleet_pulse=FleetPulseConfig(
        escalation_chat_id=tier["FLEET_PULSE_ESCALATION_CHAT_ID"],
        escalation_state_dir=tier["FLEET_PULSE_ESCALATION_STATE_DIR"])))
    monkeypatch.setattr(operation, "resolve_paths", lambda **_: SimpleNamespace(root=context.paths.root))
    monkeypatch.setattr(operation, "resolve_operation_scope", lambda **_: (context, None))
    @contextmanager
    def admitted(*args, **kwargs):
        yield SimpleNamespace(release_id="selected", native_path=package.native)
    monkeypatch.setattr(operation, "mutation_admission", admitted)
    monkeypatch.setattr(operation, "native_environment", lambda paths, current: {})
    monkeypatch.setattr(operation, "_transport", lambda *_: manager)
    monkeypatch.setattr(operation, "resolve_tiers", lambda *a, **k: {})
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
    monkeypatch.setattr(operation, "native_environment", lambda paths, current: {})
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


_ESCALATION = ("FLEET_PULSE_ESCALATION_CHAT_ID", "FLEET_PULSE_ESCALATION_STATE_DIR")


def _pulse_fleet(tmp_path):
    """The fixture's fleet with a fleet_pulse block, and the escalation pair its
    composed fleet-pulse unit carries: the target the pulse escalates to."""
    root = tmp_path / "declared"
    fleet, defaults = load_fleet(_write(root, f"""\
        fleet:
          name: fleet
          manager: manager
          service_prefix: svc
          bots:
            manager:
              expertise: [eng]
          fleet_pulse:
            escalation_chat_id: "-1002222222222"
            escalation_state_dir: {tmp_path / "escalation-channel"}
            renotify_after_s: 60
    """))
    paths = _make_paths(root)
    compose_fleet_timers(fleet, paths, defaults)
    unit = next((paths.runtime_fleet / "timers").glob("*.fleet-pulse.service")).read_text()
    env = dict(item.split("=", 1) for line in unit.splitlines() if line.startswith("Environment=")
               for item in shlex.split(line.removeprefix("Environment=")))
    return fleet, "|".join(env[key] for key in _ESCALATION)


def _env_copy(tmp_path):
    """Another pair in the manager's .env tiers: the copy freshbox calls inert."""
    return {"FLEET_PULSE_ESCALATION_CHAT_ID": SimpleNamespace(value="-1003333333333"),
            "FLEET_PULSE_ESCALATION_STATE_DIR": SimpleNamespace(value=str(tmp_path / "env-channel")),
            "TELEGRAM_BOT_TOKEN": SimpleNamespace(value="8888888:AAAAAAAAAAAAAAAAAAAA")}


def _record_targets(package):
    """The Telegram carrier records the chat and sender each alert goes to."""
    (package.native / "tg-post.sh").write_text(
        "#!/bin/bash\n"
        "printf '%s|%s\\n' \"$TELEGRAM_GROUP_CHAT_ID\" \"$TELEGRAM_STATE_DIR\" "
        ">> \"$CLAUDLOBBY_ROOT/telegram-target\"\n")


def test_notify_escalates_to_the_target_the_pulse_unit_carries(
        private_alert, monkeypatch, tmp_path):  # noqa: F811
    """#2234: `fleet notify` and fleet-pulse read one escalation target, the
    fleet.yaml `fleet_pulse:` block. A .env copy reaches neither."""
    context, package, manager, _ = private_alert
    fleet, pulse_target = _pulse_fleet(tmp_path)
    context = replace(context, fleet=fleet)
    _record_targets(package)
    monkeypatch.setattr(operation, "resolve_paths", lambda **_: SimpleNamespace(root=context.paths.root))
    monkeypatch.setattr(operation, "resolve_operation_scope", lambda **_: (context, None))
    @contextmanager
    def admitted(*args, **kwargs):
        yield SimpleNamespace(release_id="selected", native_path=package.native)
    monkeypatch.setattr(operation, "mutation_admission", admitted)
    monkeypatch.setattr(operation, "native_environment", lambda paths, current: {})
    monkeypatch.setattr(operation, "_transport", lambda *_: manager)
    monkeypatch.setattr(operation, "resolve_tiers", lambda *a, **k: _env_copy(tmp_path))
    result = operation.notify_fleet(root=context.paths.root, fleet="fleet", level="alert",
                                    event="disk_high", message="Disk nearly full", identity=object(),
                                    emit=lambda *a, **k: [SimpleNamespace(status="committed")])
    assert result.telegram.status == "carrier_accepted"
    assert (context.paths.root / "telegram-target").read_text().splitlines() == [pulse_target]


def test_a_failed_message_write_alerts_the_same_target(private_alert, monkeypatch, tmp_path):  # noqa: F811
    """The message-write alert reaches the pulse's target, also when the
    manager's .env tiers cannot be read."""
    from claudlobby import env_tiers
    from claudlobby.application_messages import _alert_tiers

    context, package, manager, _ = private_alert
    fleet, pulse_target = _pulse_fleet(tmp_path)
    context = replace(context, fleet=fleet)
    _record_targets(package)
    def unreadable(*args, **kwargs):
        raise OSError("tiers unreadable")
    for resolve in (lambda *a, **k: _env_copy(tmp_path), unreadable):
        monkeypatch.setattr(env_tiers, "resolve", resolve)
        tiers, _available = _alert_tiers(SimpleNamespace(selected=context))
        notify_recording_degraded(context, package, manager, request_id=REQUEST,
                                  component="message", at=AT, trusted_tiers=tiers)
        clear_recording_degraded(context, package, manager, request_id=REQUEST,
                                 component="message", at=AT)
    assert (context.paths.root / "telegram-target").read_text().splitlines() == [pulse_target] * 2


def test_alerts_read_only_keys_the_fleet_pulse_block_composes():
    """A variable renamed in FLEET_PULSE_ENV_KEYS cannot drop out of alerts unseen."""
    from claudlobby.recording_alerts import _FLEET_PULSE_KEYS
    assert _FLEET_PULSE_KEYS <= set(FLEET_PULSE_ENV_KEYS.values())


# #2654: a fleet that declares no pair alerts its manager's own chat, as the
# manager's own bot. `aide` sorts before `manager` and is a channel bot in the
# fleet chat, so a scan for the first channel bot in the chat would pick it.
_TELEGRAM = "channels: [plugin:telegram@claude-plugins-official]"


def _no_pair_fleet(tmp_path, *, chat='"-1004444444444"', manager=_TELEGRAM, pulse=""):
    lines = ["fleet:", "  name: fleet", "  manager: manager", "  service_prefix: svc"]
    if chat:
        lines.append(f"  telegram_group_chat_id: {chat}")
    lines += ["  bots:", "    aide:", "      expertise: [eng]", f"      {_TELEGRAM}",
              "    manager:", "      expertise: [eng]"]
    lines += [f"      {line}" for line in manager.splitlines()]
    lines += [f"  {line}" for line in pulse.splitlines()]
    fleet, _defaults = load_fleet(_write(tmp_path / "no-pair", "\n".join(lines) + "\n"))
    return fleet


def _tiers(**values):
    return {key: SimpleNamespace(value=value) for key, value in values.items()}


def _record_senders(package):
    """The Telegram carrier records each alert's chat and sender, and whether a
    token rode in with them: without one, tg-post reads the sender's own."""
    (package.native / "tg-post.sh").write_text(
        "#!/bin/bash\n"
        "printf '%s|%s|%s\\n' \"$TELEGRAM_GROUP_CHAT_ID\" \"$TELEGRAM_STATE_DIR\" "
        "\"${TELEGRAM_BOT_TOKEN:+token}\" >> \"$CLAUDLOBBY_ROOT/telegram-target\"\n")


def _notify_with(context, package, manager, monkeypatch, *, tiers=None,
                 message="svc.creds-check.service failed"):
    """`fleet notify` as the selected manager, the manager's .env tiers given."""
    monkeypatch.setattr(operation, "resolve_paths", lambda **_: SimpleNamespace(root=context.paths.root))
    monkeypatch.setattr(operation, "resolve_operation_scope", lambda **_: (context, None))
    @contextmanager
    def admitted(*args, **kwargs):
        yield SimpleNamespace(release_id="selected", native_path=package.native)
    monkeypatch.setattr(operation, "mutation_admission", admitted)
    monkeypatch.setattr(operation, "native_environment", lambda paths, current: {})
    monkeypatch.setattr(operation, "_transport", lambda *_: manager)
    monkeypatch.setattr(operation, "resolve_tiers", lambda *a, **k: dict(tiers or {}))
    return operation.notify_fleet(root=context.paths.root, fleet="fleet", level="alert",
                                  event="job_failed", message=message, identity=object(),
                                  emit=lambda *a, **k: [SimpleNamespace(status="committed")])


def _targets(context):
    target = context.paths.root / "telegram-target"
    return target.read_text().splitlines() if target.exists() else []


@pytest.mark.parametrize(("manager_yaml", "chat", "handle"), [
    (_TELEGRAM, "-1004444444444", "manager"),
    (f'{_TELEGRAM}\ntelegram:\n  chat_id: "-1005555555555"', "-1005555555555", "manager"),
    (f"{_TELEGRAM}\ntelegram:\n  handle: lead_bot", "-1004444444444", "lead_bot"),
], ids=["fleet-chat", "own-chat", "declared-handle"])
def test_a_fleet_with_no_pair_alerts_the_managers_own_chat_as_the_manager(
        private_alert, monkeypatch, tmp_path, manager_yaml, chat, handle):  # noqa: F811
    """#2654: with no pair declared, the second channel is the chat the manager
    is composed with, sent as the manager's own bot, with no token from the CLI."""
    context, package, manager, _ = private_alert
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    context = replace(context, fleet=_no_pair_fleet(tmp_path, manager=manager_yaml))
    _record_senders(package)
    result = _notify_with(context, package, manager, monkeypatch)
    assert (result.manager.status, result.telegram.status) == ("submitted", "carrier_accepted")
    sender = tmp_path / "home" / f".claude/channels/telegram-{handle}"
    assert _targets(context) == [f"{chat}|{sender}|"]
    # The chat is the one the manager's bot.conf is composed with.
    from claudlobby.composer import _bot_template_context
    composed = _bot_template_context(context.fleet.bots["manager"], context.fleet,
                                     _make_paths(tmp_path / "no-pair"))
    assert str(composed["TELEGRAM_GROUP_CHAT_ID"]) == chat


def test_a_declared_pair_wins_over_the_managers_own(private_alert, monkeypatch, tmp_path):  # noqa: F811
    context, package, manager, _ = private_alert
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    escalation = tmp_path / "escalation-channel"
    context = replace(context, fleet=_no_pair_fleet(tmp_path, pulse=(
        f'fleet_pulse:\n  escalation_chat_id: "-1002222222222"\n  escalation_state_dir: {escalation}')))
    _record_senders(package)
    result = _notify_with(context, package, manager, monkeypatch)
    assert result.telegram.status == "carrier_accepted"
    assert _targets(context) == [f"-1002222222222|{escalation}|"]


@pytest.mark.parametrize(("chat", "manager_yaml", "pulse"), [
    ('"-1004444444444"', "channels: []", ""),
    ("", _TELEGRAM, ""),
    ('"-1004444444444"', _TELEGRAM, 'fleet_pulse:\n  escalation_chat_id: "-1002222222222"'),
], ids=["manager-without-telegram", "manager-without-a-chat", "chat-without-its-sender"])
def test_with_no_pair_and_no_managers_own_chat_the_second_channel_stays_unconfigured(
        private_alert, monkeypatch, tmp_path, chat, manager_yaml, pulse):  # noqa: F811
    """As before #2654: no other bot's sender is scanned for, and a chat declared
    without its sender is refused, as the pulse refuses it, not replaced."""
    context, package, manager, _ = private_alert
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    context = replace(context, fleet=_no_pair_fleet(tmp_path, chat=chat, manager=manager_yaml,
                                                    pulse=pulse))
    _record_senders(package)
    result = _notify_with(context, package, manager, monkeypatch)
    assert (result.manager.status, result.telegram.status) == ("submitted", "unconfigured")
    assert _targets(context) == []


def test_a_tier_pair_still_wins_and_a_lone_tier_token_never_rides_the_managers_pair(
        private_alert, monkeypatch, tmp_path):  # noqa: F811
    context, package, manager, _ = private_alert
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    context = replace(context, fleet=_no_pair_fleet(tmp_path))
    _record_senders(package)
    token = "8888888:AAAAAAAAAAAAAAAAAAAA"
    paired = _notify_with(context, package, manager, monkeypatch, message="tier pair", tiers=_tiers(
        TELEGRAM_GROUP_CHAT_ID="-1006666666666", TELEGRAM_STATE_DIR=str(tmp_path / "tier-channel"),
        TELEGRAM_BOT_TOKEN=token))
    lone = _notify_with(context, package, manager, monkeypatch, message="lone token",
                        tiers=_tiers(TELEGRAM_BOT_TOKEN=token))
    assert (paired.telegram.status, lone.telegram.status) == ("carrier_accepted", "carrier_accepted")
    sender = tmp_path / "home" / ".claude/channels/telegram-manager"
    assert _targets(context) == [f"-1006666666666|{tmp_path / 'tier-channel'}|token",
                                 f"-1004444444444|{sender}|"]


def test_a_failed_message_write_alerts_the_managers_own_chat_too(
        private_alert, monkeypatch, tmp_path):  # noqa: F811
    from claudlobby import env_tiers
    from claudlobby.application_messages import _alert_tiers

    context, package, manager, _ = private_alert
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    context = replace(context, fleet=_no_pair_fleet(tmp_path))
    _record_senders(package)
    monkeypatch.setattr(env_tiers, "resolve", lambda *a, **k: {})
    tiers, available = _alert_tiers(SimpleNamespace(selected=context))
    result = notify_recording_degraded(context, package, manager, request_id=REQUEST,
                                       component="message", at=AT, trusted_tiers=tiers)
    assert (available, result.telegram.status) == (True, "carrier_accepted")
    sender = tmp_path / "home" / ".claude/channels/telegram-manager"
    assert _targets(context) == [f"-1004444444444|{sender}|"]
