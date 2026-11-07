"""#2696: `bot restart --fresh` on the paths it shares with a plain restart.

A bot's own restart, the path `/restart` takes, hands its choice to a forked
child that outlives the requesting pane. A bot with no live session has none
to retire, and its receipt is renewed all the same.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from claudlobby import bot_operations, native_session
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401
from tests.test_bot_operations import _worker_cli


def _as_generated(monkeypatch, worker, bot):
    """Run the CLI as the generated bot, as `/restart` does from the bot's pane."""
    bot_dir = worker.root / "runtime/bots" / bot
    for key, value in (("CLAUDLOBBY_ROOT", str(worker.root)), ("FLEET_ROOT", str(worker.root)),
                       ("CLAUDLOBBY_INSTALLATION", str(worker.installation.directory)),
                       ("FLEET_NAME", "example"), ("BOT_ID", bot), ("BOT_DIR", str(bot_dir))):
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("bot", ["worker", "manager"])
@pytest.mark.parametrize("flags, fresh", [pytest.param(("--fresh",), True, id="fresh"),
                                          pytest.param((), False, id="plain")])
def test_a_bots_own_restart_hands_its_session_choice_to_the_scheduler(
        cold, monkeypatch, capsys, bot, flags, fresh):  # noqa: F811
    worker = _worker_cli(cold, monkeypatch, capsys)
    _as_generated(monkeypatch, worker, bot)
    scheduled = []

    def schedule(**kwargs):
        scheduled.append(kwargs)
        return bot_operations.BotLifecycleResult("example", bot, None, "", "requested", False,
                                                 "not_checked", request_id="r", log_path="log")
    monkeypatch.setattr(bot_operations, "_schedule_self_restart", schedule)
    worker.call("bot", "restart", bot, *flags)
    assert [(row["bot"], row.get("fresh", False)) for row in scheduled] == [(bot, fresh)]


_REQUESTING_CLI = """
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from claudlobby import bot_operations as b
root, bot_dir, fresh = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == "1"
@contextmanager
def admitted(*args, **kwargs):
    yield SimpleNamespace(revision="a" * 40, config_fingerprint="b" * 64)
b.mutation_admission = admitted
b.RuntimeIdentity.current = staticmethod(lambda: object())
def restart(**kwargs):
    kwargs["_on_lock"]()
    received = {key: kwargs[key] for key in ("bot", "running", "restart", "_self_child")}
    received["fresh"] = kwargs.get("fresh", False)
    (bot_dir / "child.json").write_text(json.dumps(received))
    return b.BotLifecycleResult("fleet", "bot", None, "unit", "running", True, "session_ready",
                                "fresh_session" if received["fresh"] else "native_session")
b.set_bot_running = restart
answer = b._schedule_self_restart(root=root, fleet="fleet", bot="bot", ceiling=None,
                                  bot_dir=bot_dir, fresh=fresh)
print(answer.handoff, flush=True)
os._exit(0)  # the requesting CLI exits, which releases the child
"""


@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "plain"])
def test_a_self_restart_carries_its_session_choice_into_the_forked_child(tmp_path, fresh):
    """The child that outlives the requesting pane makes the restart that was asked for."""
    bot_dir = tmp_path / "bot"
    (bot_dir / ".claude").mkdir(parents=True)
    (bot_dir / "logs").mkdir()
    now = datetime.now(timezone.utc)
    (bot_dir / ".claude/session.md").write_text(
        f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
    process = subprocess.run([sys.executable, "-c", _REQUESTING_CLI, str(tmp_path), str(bot_dir),
                              "1" if fresh else "0"], capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    label = "fresh_session" if fresh else "native_session"
    assert process.stdout.split() == [label]  # the requesting CLI's answer
    log, deadline = bot_dir / "logs/startup.log", time.monotonic() + 10
    while time.monotonic() < deadline and not (log.exists() and log.read_text().endswith("\n")):
        time.sleep(0.02)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(row["status"], row.get("handoff")) for row in rows] == [("complete", label)]
    assert json.loads((bot_dir / "child.json").read_text()) == {
        "bot": "bot", "running": True, "restart": True, "_self_child": True, "fresh": fresh}


@pytest.mark.parametrize("receipt", ["retained", "older bot"])
def test_a_fresh_restart_of_a_bot_with_no_live_session_renews_its_receipt(
        cold, monkeypatch, capsys, receipt):  # noqa: F811
    """No live session is left to retire, and the restart still starts the new identity."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    worker.native.session = "absent"
    bot_dir = worker.root / "runtime/bots/worker"
    original = native_session.read(bot_dir)
    if receipt == "older bot":
        # A bot from before receipts, which a plain launch refuses.
        native_session._path(bot_dir).unlink()
        (bot_dir / "data/.spawn").touch()
    fresh = worker.call("bot", "restart", "worker", "--fresh")["data"]
    assert fresh["state"] == "running" and fresh["handoff"] == "fresh_session"
    renewed = native_session.read(bot_dir)
    assert renewed["session_id"] != original["session_id"] and renewed["pending"] is False
    assert worker.native.actions == ["start"]


_DOMAIN = "linux:00000000000000000000000000000000:pid:[4026531836]"


def _provider_starts(monkeypatch, worker):
    """Report each pane's process start, as the Linux observer does (the shared fake
    reports pids only), and fail the readiness wait while the receipt is pending: the
    real wait polls its confirm until the ceiling and then returns 3 (#2748)."""
    native, call = worker.native, worker.native.call
    bot_dir = worker.root / "runtime/bots/worker"

    def observed(function, *args, timeout=30):
        result = call(function, *args, timeout=timeout)
        if function == "svc_bot_native_session" and args[3:] == ("", "start"):
            start = f"Linux:{1000 + native.native_pid}:{_DOMAIN}"
            return subprocess.CompletedProcess(result.args, 0, result.stdout.rstrip("\n") + "\t" + start + "\n", "")
        if function == "svc_activation_bot_ready" and native_session.read(bot_dir)["pending"]:
            return subprocess.CompletedProcess(result.args, 3, "", "")
        return result
    monkeypatch.setattr(native, "call", observed)


def _provider_record(worker, sid):
    """The provider's PID record for the current pane, in the captured Linux shape."""
    pid = worker.native.native_pid
    path = worker.account / "sessions" / f"{pid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": str(worker.root / "runtime/bots/worker"),
                                "procStart": str(1000 + pid), "pidDomain": _DOMAIN, "kind": "interactive",
                                "entrypoint": "cli", "version": "2.1.316"}))


def _launch_without_capture(bot, label, tmpdir, account, adapter):
    """The launch as the fake performs it, except its SessionStart capture never lands."""
    native_session.launch(bot, label, tmpdir, account)
    transcript = Path(native_session.read(bot)["transcript"])
    transcript.parent.mkdir(parents=True, exist_ok=True)
    if not transcript.exists():
        transcript.write_bytes(b"context of the launched session\n")
    return native_session.read(bot)


def test_a_plain_restart_after_a_fresh_start_whose_capture_missed_keeps_the_session(
        cold, monkeypatch, capsys):  # noqa: F811
    """#2748: the fresh restart reads unverified when its capture misses; the plain restart
    after it adopts the running session from the provider's record and continues it."""
    import tests.test_native_session as shapes
    worker = _worker_cli(cold, monkeypatch, capsys)
    _provider_starts(monkeypatch, worker)
    bot_dir = worker.root / "runtime/bots/worker"
    retired = native_session.read(bot_dir)["session_id"]
    with monkeypatch.context() as patch:
        patch.setattr(shapes, "retain_native_session", _launch_without_capture)
        fresh = worker.call("bot", "restart", "worker", "--fresh", expected=6)
    assert fresh["error"]["message"].startswith("bot lifecycle effect is unverified")
    renewed = native_session.read(bot_dir)
    assert renewed["session_id"] != retired and renewed["pending"] is True and renewed["process"] is None
    _provider_record(worker, renewed["session_id"])
    plain = worker.call("bot", "restart", "worker")["data"]
    assert plain["state"] == "running" and plain["handoff"] == "native_session"
    kept = native_session.read(bot_dir)
    assert kept["session_id"] == renewed["session_id"] and kept["pending"] is False
    assert "provider_start" not in kept  # the continued session's own capture confirmed it
    assert worker.native.actions == ["start", "start"]


@pytest.mark.parametrize("refusal", ["another pane", "another session"])
def test_a_refused_restart_says_why(cold, monkeypatch, capsys, refusal):  # noqa: F811
    """#2748: the door keeps a refused restart's own reason, as it does for a stop."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    _provider_starts(monkeypatch, worker)
    bot_dir = worker.root / "runtime/bots/worker"
    if refusal == "another pane":
        worker.native.native_pid += 1  # the pane is not the receipt's process
        reason = "native session is not the recorded process"
    else:
        # A pending launch whose provider record names another session (a /clear).
        native_session.renew(bot_dir, "com.example.worker", worker.private_tmpdir, worker.account)
        _launch_without_capture(bot_dir, "com.example.worker", worker.private_tmpdir, worker.account, None)
        _provider_record(worker, "00000000-0000-4000-8000-000000000002")
        reason = "native provider session is not the pending launch"
    receipt = native_session._path(bot_dir).read_bytes()
    refused = worker.call("bot", "restart", "worker", expected=6)
    assert refused["error"]["code"] == "unavailable" and refused["error"]["message"] == reason
    assert worker.native.actions == [] and native_session._path(bot_dir).read_bytes() == receipt
