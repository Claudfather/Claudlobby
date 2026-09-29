"""Recording outage pages use real native debounce with private carrier stubs."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import shlex
import threading
import time

import pytest

from claudlobby import recording_alerts as alerts
from claudlobby.config import BotConfig, FleetConfig
from claudlobby.context import Context
from claudlobby.message_transport import TransportDestination
from claudlobby.paths import Paths
from claudlobby.recording_alerts import (
    ChannelOutcome, clear_recording_degraded, notify_recording_degraded,
)
from tests.package_fixtures import source_package


REQUEST = "00112233-4455-6677-8899-aabbccddeeff"
AT = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def private_alert(tmp_path):
    root = tmp_path / "private-root"
    root.mkdir()
    socket_dir = tmp_path / "private-tmux"
    socket_dir.mkdir()
    native = tmp_path / "native"
    native.mkdir()
    source = source_package()
    (native / "lib-common.sh").write_text(
        f". {shlex.quote(str(source.native / 'lib-common.sh'))}\n"
        "bot_tmux() {\n"
        "  [ \"$1\" = svc.manager ] && [ \"$2\" = display-message ] &&\n"
        "    [ \"$3\" = -p ] && [ \"$4\" = -t ] && [ \"$5\" = =manager ] || return 2\n"
        "  mktemp \"$CLAUDLOBBY_ROOT/fingerprint.XXXXXXXX\" >/dev/null\n"
        "  printf '%s' '100-200'\n"
        "}\n"
        "bot_tmux_send() {\n"
        "  [ \"$2\" = =manager: ] || return 2\n"
        "  [ ! -e \"$CLAUDLOBBY_ROOT/manager-fails\" ] || { echo 'secret-token' >&2; return 1; }\n"
        "  if [ -e \"$CLAUDLOBBY_ROOT/manager-hold\" ]; then\n"
        "    touch \"$CLAUDLOBBY_ROOT/manager-entered\"\n"
        "    while [ -e \"$CLAUDLOBBY_ROOT/manager-hold\" ]; do sleep 0.02; done\n"
        "  fi\n"
        "  printf '%s\\n' \"$3\" >> \"$CLAUDLOBBY_ROOT/manager-capture\"\n"
        "}\n"
    )
    (native / "tg-post.sh").write_text(
        "#!/bin/bash\n"
        "[ ! -e \"$CLAUDLOBBY_ROOT/telegram-fails\" ] || { echo 'secret-token' >&2; exit 3; }\n"
        "printf '%s\\n' \"$1\" >> \"$CLAUDLOBBY_ROOT/telegram-capture\"\n"
    )
    (native / "tg-post.sh").chmod(0o755)
    package = replace(source, native=native)
    fleet = FleetConfig(name="fleet", service_prefix="svc", manager="manager",
                        bots={"manager": BotConfig("manager", "Manager", [])})
    context = Context(Paths(root, package=package), fleet, {})
    manager = TransportDestination(root, "fleet", "svc.manager", "manager", socket_dir)
    tier = {"FLEET_PULSE_ESCALATION_CHAT_ID": "-1001111111111",
            "FLEET_PULSE_ESCALATION_STATE_DIR": str(root / "channel")}
    return context, package, manager, tier


def _notify(case, *, component="message", **kwargs):
    context, package, manager, tier = case
    return notify_recording_degraded(context, package, manager,
                                     request_id=REQUEST, component=component, at=AT,
                                     trusted_tiers=tier, **kwargs)


def _clear(case):
    context, package, manager, _ = case
    return clear_recording_degraded(context, package, manager,
                                    request_id=REQUEST, component="message", at=AT)


def test_first_alert_suppresses_repeat_and_confirmed_write_clears_both(private_alert):
    context, package, manager, _ = private_alert
    first = _notify(private_alert, component="request_receipt")
    assert first.manager == ChannelOutcome("submitted", True)
    assert first.telegram == ChannelOutcome("carrier_accepted", True)
    assert _notify(private_alert, component="request_receipt").manager.status == "suppressed"
    assert _notify(private_alert, component="request_receipt").telegram.status == "suppressed"
    assert len((context.paths.root / "manager-capture").read_text().splitlines()) == 1
    assert len((context.paths.root / "telegram-capture").read_text().splitlines()) == 1
    clear = _clear(private_alert)
    assert clear.manager and clear.telegram
    again = _notify(private_alert, component="request_receipt")
    assert (again.manager.status, again.telegram.status) == ("submitted", "carrier_accepted")
    assert len((context.paths.root / "manager-capture").read_text().splitlines()) == 2
    assert "root=" + str(context.paths.root) in (context.paths.root / "manager-capture").read_text()
    assert ("request receipt persistence could not be confirmed" in
            (context.paths.root / "telegram-capture").read_text() and
            "request=" + REQUEST in (context.paths.root / "telegram-capture").read_text())


def test_manager_alert_uses_pane_target_when_telegram_is_unconfigured(private_alert):
    context, package, manager, _ = private_alert
    result = notify_recording_degraded(context, package, manager,
                                       request_id=REQUEST, component="message_intent", at=AT,
                                       trusted_tiers={})
    assert result.manager == ChannelOutcome("submitted", True)
    assert result.telegram.status == "unconfigured"
    assert "message intent persistence could not be confirmed" in (
        context.paths.root / "manager-capture").read_text()


def test_each_failed_carrier_retries_without_leaking_native_errors(private_alert, capsys):
    context, _, _, _ = private_alert
    (context.paths.root / "manager-fails").touch()
    (context.paths.root / "telegram-fails").touch()
    failed = _notify(private_alert)
    assert (failed.manager.status, failed.telegram.status) == ("failed", "failed")
    assert "secret-token" not in capsys.readouterr().err
    (context.paths.root / "manager-fails").unlink()
    (context.paths.root / "telegram-fails").unlink()
    retried = _notify(private_alert)
    assert (retried.manager.status, retried.telegram.status) == ("submitted", "carrier_accepted")


def test_unwritable_debounce_is_disclosed_without_muffling_alert(private_alert, capsys):
    context, package, manager, _ = private_alert
    state = context.paths.root / "state"
    state.mkdir()
    (state / "recording-alerts").write_text("not a directory")
    result = _notify(private_alert)
    assert result.manager == ChannelOutcome("submitted", False, "state_unavailable")
    assert result.telegram == ChannelOutcome("carrier_accepted", False, "state_unavailable")
    assert "debounce_available=0" in capsys.readouterr().err
    assert (context.paths.root / "manager-capture").exists()
    assert (context.paths.root / "telegram-capture").exists()
    assert _clear(private_alert).manager is False


def test_absent_explicit_pair_never_scans_other_bot_or_ambient_chat(private_alert, monkeypatch):
    context, package, manager, _ = private_alert
    monkeypatch.setenv("TELEGRAM_GROUP_CHAT_ID", "-1009999999999")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "ambient-secret")
    foreign = context.paths.runtime_bots / "stray"
    foreign.mkdir(parents=True)
    (foreign / "bot.conf").write_text("TELEGRAM_GROUP_CHAT_ID=-1009999999999\n")
    result = notify_recording_degraded(context, package, manager, request_id=REQUEST,
                                       component="message", at=AT, trusted_tiers={})
    assert result.manager.status == "submitted"
    assert result.telegram.status == "unconfigured"
    assert not (context.paths.root / "telegram-capture").exists()
    with pytest.raises(ValueError, match="active fleet manager"):
        _notify(private_alert[:2] + (replace(manager, session="stray"), private_alert[3]))


def _wait_for(path):
    deadline = time.monotonic() + 3
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), "private carrier did not enter the bounded hold"


def _observe_second_lock_attempt(monkeypatch):
    attempted = threading.Event()
    guard = threading.Lock()
    calls = 0
    real_flock = alerts.fcntl.flock
    def observe_flock(fd, operation):
        nonlocal calls
        if operation & alerts.fcntl.LOCK_EX:
            with guard:
                calls += 1
                if calls == 2:
                    attempted.set()
        return real_flock(fd, operation)
    monkeypatch.setattr(alerts.fcntl, "flock", observe_flock)
    return attempted


def test_simultaneous_alerts_share_one_debounce_decision_per_channel(private_alert, monkeypatch):
    root = private_alert[0].paths.root
    hold = root / "manager-hold"
    hold.touch()
    attempted = _observe_second_lock_attempt(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_notify, private_alert)
        try:
            _wait_for(root / "manager-entered")
            second = pool.submit(_notify, private_alert)
            assert attempted.wait(timeout=2)
            assert not second.done()
            # The second call attempted flock, but no second native
            # manager fingerprint may run until the first send commits a marker.
            assert len(list(root.glob("fingerprint.*"))) == 1
        finally:
            hold.unlink()
        outcomes = (first.result(timeout=12), second.result(timeout=12))
    assert sorted(item.manager.status for item in outcomes) == ["submitted", "suppressed"]
    assert sorted(item.telegram.status for item in outcomes) == ["carrier_accepted", "suppressed"]
    assert len((root / "manager-capture").read_text().splitlines()) == 1
    assert len((root / "telegram-capture").read_text().splitlines()) == 1


def test_confirmed_write_clear_waits_for_inflight_alert_marker(private_alert, monkeypatch):
    root = private_alert[0].paths.root
    hold = root / "manager-hold"
    hold.touch()
    attempted = _observe_second_lock_attempt(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_notify, private_alert)
        try:
            _wait_for(root / "manager-entered")
            clear = pool.submit(_clear, private_alert)
            assert attempted.wait(timeout=2)
            time.sleep(0.1)
            assert not clear.done()
        finally:
            hold.unlink()
        assert first.result(timeout=12).manager.status == "submitted"
        assert clear.result(timeout=12).manager
    assert _notify(private_alert).manager.status == "submitted"
    assert len((root / "manager-capture").read_text().splitlines()) == 2
