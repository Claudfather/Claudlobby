"""Selected session evidence and bounded native log reads stay distinct."""

from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from claudlobby.command_result import CommandFailure, execute
from claudlobby.commands import runtime_read
from claudlobby.fleet_operations import FleetBotObservation, FleetReconcileResult


LIB = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts"


def _context(tmp_path):
    root = tmp_path / "data-root"
    source = tmp_path / "external-fleet"
    source.mkdir()
    (source / "fleet.yaml").write_text("fleet:\n  name: example\n")
    return SimpleNamespace(
        fleet=SimpleNamespace(name="example", bots={"lead": object(), "worker": object()}),
        paths=SimpleNamespace(root=root, source_dir=source, lib=LIB),
    )


def _args(command, bot=None, lines=10):
    return SimpleNamespace(public_command=command, bot_id=bot, lines=lines, seed=False,
                           root=None, fleet=None)


def test_runtime_scope_uses_frozen_selection_not_authored_overlay(monkeypatch, tmp_path):
    from claudlobby import operation_context
    from claudlobby.commands import orientation

    selected = _context(tmp_path)
    monkeypatch.setattr(operation_context, "resolve_operation_scope",
                        lambda **kwargs: (selected, None))
    monkeypatch.setattr(orientation, "_context", lambda _args:
                        pytest.fail("authored fleet context was consulted"))
    assert runtime_read._context(_args("bot.logs", "lead")) is selected
    with pytest.raises(CommandFailure) as missing:
        runtime_read._context(_args("bot.logs", "authored-only"))
    assert missing.value.error.code == "not_found"


def test_session_reports_selected_native_absence_and_unknown_without_pid_scan(monkeypatch, tmp_path):
    from claudlobby import fleet_operations

    context = _context(tmp_path)
    monkeypatch.setattr(runtime_read, "_context", lambda _args: context)
    rows = (
        FleetBotObservation("lead", True, True, "active", "ready", "healthy", "unit-lead"),
        FleetBotObservation("worker", True, False, "inactive", "absent", "unsupervised_down", "unit-worker"),
    )
    def observe(**kwargs):
        assert kwargs == {"root": context.paths.root, "fleet": "example", "bot": "worker"}
        return FleetReconcileResult("example", "selected-release", rows)
    monkeypatch.setattr(fleet_operations, "reconcile_fleet", observe)
    result = runtime_read.dispatch(_args("bot.session", "worker"))
    assert result.release_id == "selected-release"
    assert result.data["bot"]["session"] == "absent"
    assert result.data["bot"]["state"] == "unsupervised_down"
    unknown = (*rows[:1], FleetBotObservation("worker", True, True, "unknown", "unknown",
                                              "indeterminate", "unit-worker"))
    monkeypatch.setattr(fleet_operations, "reconcile_fleet", lambda **_kwargs:
                        FleetReconcileResult("example", "selected-release", unknown))
    assert runtime_read.dispatch(_args("bot.session", "worker")).data["bot"]["state"] == "indeterminate"


def test_logs_use_external_selected_fleet_and_disclose_missing_per_bot(monkeypatch, tmp_path, capsys):
    context = _context(tmp_path)
    monkeypatch.setattr(runtime_read, "_context", lambda _args: context)
    monkeypatch.setattr(runtime_read, "_log_release", lambda _context: "selected-release")
    bot_dir = context.paths.source_dir / "runtime/bots/lead/logs"
    bot_dir.mkdir(parents=True)
    (bot_dir / "startup.log").write_text("one\ntwo\nthree\n")
    args = _args("fleet.logs", lines=2)
    assert execute(args.public_command, lambda: runtime_read.dispatch(args), json_output=True) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1 and result["release_id"] == "selected-release"
    items = {item["bot"]: item for item in result["data"]["items"]}
    assert items["lead"]["status"] == "read" and "two\nthree" in items["lead"]["text"]
    assert "one" not in items["lead"]["text"]
    assert items["worker"] == {"bot": "worker", "status": "missing", "text": None}
    with pytest.raises(CommandFailure) as missing:
        runtime_read.dispatch(_args("bot.logs", "worker"))
    assert missing.value.error.code == "not_found"


def test_log_source_symlink_refuses_and_lines_are_bounded(monkeypatch, tmp_path):
    context = _context(tmp_path)
    monkeypatch.setattr(runtime_read, "_context", lambda _args: context)
    monkeypatch.setattr(runtime_read, "_log_release", lambda _context: "selected-release")
    bot_dir = context.paths.source_dir / "runtime/bots/lead/logs"
    bot_dir.mkdir(parents=True)
    secret = tmp_path / "secret.log"
    secret.write_text("must not be disclosed\n")
    (bot_dir / "startup.log").symlink_to(secret)
    with pytest.raises(CommandFailure) as unavailable:
        runtime_read.dispatch(_args("bot.logs", "lead"))
    assert unavailable.value.error.code == "unavailable"
    assert "must not be disclosed" not in str(unavailable.value.data)
    with pytest.raises(CommandFailure) as invalid:
        runtime_read.dispatch(_args("bot.logs", "lead", lines=201))
    assert invalid.value.error.code == "invalid_argument"
    (bot_dir / "startup.log").unlink()
    (bot_dir / "startup.log").write_text("")
    assert runtime_read.dispatch(_args("bot.logs", "lead")).data["items"][0]["status"] == "empty"
    (bot_dir / "startup.log").write_text("x" * 80 + "\n")
    monkeypatch.setattr(runtime_read, "_MAX_LOG_BYTES", 32)
    with pytest.raises(CommandFailure) as bounded:
        runtime_read.dispatch(_args("bot.logs", "lead"))
    assert bounded.value.error.code == "unavailable"


def test_large_line_refuses_without_returning_a_partial_tail(monkeypatch, tmp_path):
    context = _context(tmp_path)
    monkeypatch.setattr(runtime_read, "_context", lambda _args: context)
    monkeypatch.setattr(runtime_read, "_log_release", lambda _context: "selected-release")
    bot_dir = context.paths.source_dir / "runtime/bots/lead/logs"
    bot_dir.mkdir(parents=True)
    log = bot_dir / "startup.log"
    log.write_text("x" * (2 * 1024 * 1024) + "\n")
    with pytest.raises(CommandFailure) as refused:
        runtime_read.dispatch(_args("bot.logs", "lead", lines=1))
    assert refused.value.error.code == "unavailable"
    assert "x" * 100 not in str(refused.value.data)
    log.write_text("x" * (2 * 1024 * 1024) + "\nshort\n")
    assert runtime_read.dispatch(_args("bot.logs", "lead", lines=1)).data["items"][0]["text"].endswith("short")


def test_logs_refuse_a_different_executing_release_before_reading(monkeypatch, tmp_path):
    from claudlobby import activation_state, context as context_owner

    context = _context(tmp_path)
    monkeypatch.setattr(runtime_read, "_context", lambda _args: context)
    monkeypatch.setattr(activation_state, "read_selection", lambda _root:
                        {"release_id": "selected-release"})
    monkeypatch.setattr(context_owner, "native_environment", lambda _paths:
                        {"CLAUDLOBBY_RELEASE_ID": "other-release"})
    with pytest.raises(CommandFailure) as mismatch:
        runtime_read.dispatch(_args("bot.logs", "lead"))
    assert mismatch.value.error.code == "release_mismatch"


def test_incomplete_log_preserves_other_files_and_discloses_decode_replacement(monkeypatch, tmp_path):
    context = _context(tmp_path)
    monkeypatch.setattr(runtime_read, "_context", lambda _args: context)
    monkeypatch.setattr(runtime_read, "_log_release", lambda _context: "selected-release")
    logs = context.paths.source_dir / "runtime/bots/lead/logs"
    logs.mkdir(parents=True)
    (logs / "large.jsonl").write_text("x" * 170000 + "\n")
    good = logs / "bot.log"
    good.write_text("readable output\n")
    with pytest.raises(CommandFailure) as partial:
        runtime_read.dispatch(_args("bot.logs", "lead", lines=200))
    item = partial.value.data["items"][0]
    assert item["status"] == "partial"
    assert "readable output" in item["text"] and "truncated_line" in item["text"]
    (logs / "large.jsonl").unlink()
    good.write_bytes(b"readable \xff output\n")
    with pytest.raises(CommandFailure) as decoded:
        runtime_read.dispatch(_args("bot.logs", "lead"))
    item = decoded.value.data["items"][0]
    assert item["decode_replacements"] is True
    assert "readable" in item["text"]
