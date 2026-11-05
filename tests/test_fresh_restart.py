"""#2696: `bot restart --fresh` on the paths it shares with a plain restart.

A bot's own restart, the path `/restart` takes, hands its choice to a forked
child that outlives the requesting pane. A bot with no live session has none
to retire, and its receipt is renewed all the same.
"""

from datetime import datetime, timezone
import json
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
