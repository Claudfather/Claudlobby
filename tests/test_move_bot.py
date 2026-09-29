"""Focused contracts for selected-release bot relocation."""

from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.command_result import CommandFailure
from claudlobby.commands import move_bot


def _move(tmp_path: Path, *, channel: bool = False) -> move_bot.Move:
    source_dir = tmp_path / "source" / "runtime" / "bots" / "worker"
    target_dir = tmp_path / "target" / "runtime" / "bots" / "worker"
    source_dir.mkdir(parents=True)
    bot = SimpleNamespace(bot_id="worker", telegram=SimpleNamespace(handle="worker" if channel else None),
                          channels=[])
    source = SimpleNamespace(fleet=SimpleNamespace(name="source", bots={"worker": bot},
                                           telegram_group_chat_id="-1001"),
                             paths=SimpleNamespace(package=object()))
    target = SimpleNamespace(fleet=SimpleNamespace(name="target", bots={"worker": bot},
                                           telegram_group_chat_id="-1002"),
                             paths=SimpleNamespace(package=source.paths.package))
    return move_bot.Move(tmp_path, source, target, source_dir, target_dir,
                         "r-selected", tmp_path / "units")


def test_retained_copy_preserves_durable_paths_and_keeps_source(tmp_path):
    move = _move(tmp_path)
    (move.source_dir / ".env").write_text("TOKEN=private\n")
    (move.source_dir / "memory").mkdir()
    (move.source_dir / "memory" / "fact.md").write_text("remember")
    (move.source_dir / "data").mkdir()
    (move.source_dir / "data" / "record.json").write_text('{"kept":true}')
    (move.source_dir / "projects" / "repo").mkdir(parents=True)
    (move.source_dir / "projects" / "repo" / "README").write_text("project")
    (move.source_dir / "projects" / "repo" / "linked").symlink_to("README")
    (move.source_dir / ".claude").mkdir()
    (move.source_dir / ".claude" / "session.md").write_text("handoff")
    (move.target_dir / "memory").mkdir(parents=True)
    (move.target_dir / "data" / "events").mkdir(parents=True)

    move_bot.check_copy_destinations(move.source_dir, move.target_dir)
    copied = move_bot.copy_retained(move)

    assert copied == [str(move.target_dir / name) for name in move_bot._RETAINED]
    assert (move.target_dir / ".env").read_text() == "TOKEN=private\n"
    assert (move.target_dir / ".env").stat().st_mode & 0o777 == 0o600
    assert (move.target_dir / "memory" / "fact.md").read_text() == "remember"
    assert (move.target_dir / "data" / "record.json").read_text() == '{"kept":true}'
    assert (move.target_dir / "data" / "events").is_dir()
    assert (move.target_dir / "projects" / "repo" / "README").read_text() == "project"
    assert (move.target_dir / "projects" / "repo" / "linked").is_symlink()
    assert (move.target_dir / "projects" / "repo" / "linked").readlink() == Path("README")
    assert (move.target_dir / ".claude" / "session.md").read_text() == "handoff"
    assert (move.source_dir / "memory" / "fact.md").read_text() == "remember"


def test_retained_copy_refuses_redirect_before_mutation(tmp_path):
    move = _move(tmp_path)
    move.target_dir.mkdir(parents=True)
    (move.target_dir / ".env").symlink_to(tmp_path / "outside")
    with pytest.raises(CommandFailure, match="redirected"):
        move_bot.check_copy_destinations(move.source_dir, move.target_dir)
    assert not (tmp_path / "outside").exists()


def test_nonempty_target_retained_path_refuses_before_source_stop(tmp_path, monkeypatch):
    from claudlobby import bot_operations

    move = _move(tmp_path)
    (move.source_dir / "data").mkdir()
    (move.source_dir / "data" / "fact.md").write_text("source")
    (move.target_dir / "data").mkdir(parents=True)
    (move.target_dir / "data" / "fact.md").write_text("target")
    monkeypatch.setattr(move_bot, "no_active_assignment", lambda *_: None)
    monkeypatch.setattr(move_bot, "source_session", lambda *_, **__: None)
    monkeypatch.setattr(bot_operations, "set_bot_running",
                        lambda **_: pytest.fail("source stopped before retained conflict refusal"))

    with pytest.raises(CommandFailure, match="target retained path is not empty"):
        move_bot.apply_move(move, "worker", force=False, cleanup=True)
    assert (move.target_dir / "data" / "fact.md").read_text() == "target"
    assert (move.source_dir / "data" / "fact.md").read_text() == "source"


def test_access_replaces_only_owned_source_group(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    move = _move(tmp_path, channel=True)
    path = tmp_path / ".claude/channels/telegram-worker/access.json"
    path.parent.mkdir(parents=True)
    original = {"groups": {"-1001": {"allowFrom": ["42"], "requireMention": False}},
                "other": "retained"}
    path.write_text(json.dumps(original))
    assert move_bot.update_access(move, "worker", dry_run=True) == str(path)
    assert json.loads(path.read_text()) == original
    move_bot.update_access(move, "worker")
    assert json.loads(path.read_text()) == {
        "groups": {"-1002": {"allowFrom": ["42"], "requireMention": False}},
        "other": "retained",
    }
    path.write_text(json.dumps({"groups": {"-1003": {"allowFrom": []}}}))
    with pytest.raises(CommandFailure, match="another group"):
        move_bot.update_access(move, "worker", dry_run=True)


def test_apply_stages_after_copy_and_reports_failed_activation(tmp_path, monkeypatch):
    from claudlobby import activation, activation_state, bot_operations, config_staging, releases

    move = _move(tmp_path)
    (move.source_dir / ".env").write_text("TOKEN=private\n")
    events = []
    monkeypatch.setattr(move_bot, "no_active_assignment", lambda *_: events.append("assignment"))
    monkeypatch.setattr(move_bot, "source_session", lambda *_, **__: events.append("session"))
    monkeypatch.setattr(bot_operations, "set_bot_running", lambda **_: (events.append("stop") or SimpleNamespace(state="stopped")))
    monkeypatch.setattr(releases, "read_release", lambda *_: SimpleNamespace(release_id="r-selected"))

    def stage(*_):
        assert (move.target_dir / ".env").read_text() == "TOKEN=private\n"
        events.append("stage")
        return SimpleNamespace(plan_id="p-staged")

    monkeypatch.setattr(config_staging, "stage_configuration", stage)
    monkeypatch.setattr(move_bot, "declared_paths", lambda *_, **__: [object()])
    monkeypatch.setattr(activation, "upgrade_activation", lambda *_: (events.append("activate") or (_ for _ in ()).throw(RuntimeError("failed"))))
    monkeypatch.setattr(activation_state, "read_selection", lambda *_: None)
    with pytest.raises(CommandFailure, match="incomplete") as caught:
        move_bot.apply_move(move, "worker", force=False, cleanup=True)
    assert events == ["assignment", "session", "stop", "stage", "activate"]
    assert caught.value.data["plan_id"] == "p-staged"
    assert caught.value.data["source_stopped"] is True
    assert caught.value.data["source_retained"] is True
    assert (move.source_dir / ".env").exists()


def test_preview_discloses_host_scope_without_effect(tmp_path, monkeypatch):
    move = _move(tmp_path)
    monkeypatch.setattr(move_bot, "preflight", lambda *_: move)
    monkeypatch.setattr(move_bot, "apply_move", lambda *_args, **_kwargs: pytest.fail("preview mutated"))
    args = Namespace(root=str(tmp_path), bot="worker", to="target", from_fleet="source",
                     apply=False, cleanup_source=False, force=False)
    result = move_bot.dispatch(args)
    assert result.data["state"] == "preview"
    assert result.data["host_restart_scope"] == "all_declared_fleets"
    assert result.data["changed"] is False
