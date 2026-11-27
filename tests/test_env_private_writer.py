"""Fleet, bot and scaffolded .env files go through the one private writer."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby import dotenv
from claudlobby.command_result import CommandFailure
from claudlobby.commands import env_migrate
from claudlobby.commands.setup import _sync_parent, _write_config
from claudlobby.composer import EnvVar, _scaffold_env_merge


def _paths(tmp_path: Path):
    return SimpleNamespace(
        root=tmp_path,
        fleet_dir=tmp_path / "fleet",
        bot_runtime=lambda name: tmp_path / "runtime" / name,
    )


def _migrate(tmp_path: Path, fleet_vars=None, bot_vars=None):
    (tmp_path / "fleet").mkdir(exist_ok=True)
    return env_migrate._apply_env_migration(
        SimpleNamespace(name="example"), _paths(tmp_path), Path("/home/alice/old"),
        fleet_vars if fleet_vars is not None else {"API_TOKEN": "fleet-secret"},
        bot_vars if bot_vars is not None else {"worker": {"BOT_TOKEN": "bot-secret"}},
    )


def _spy_replace(monkeypatch, seen):
    real = os.replace

    def spy(src, dst, *a, **k):
        seen.append(stat.S_IMODE(os.stat(src).st_mode))
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", spy)


def test_old_names_still_importable_from_setup():
    assert _write_config is dotenv._write_config
    assert _sync_parent is dotenv._sync_parent


def test_migration_writes_private_files_atomically(tmp_path, monkeypatch):
    (tmp_path / "runtime" / "worker").mkdir(parents=True)
    seen: list[int] = []
    _spy_replace(monkeypatch, seen)
    assert _migrate(tmp_path) == 0
    assert seen == [0o600, 0o600]
    for env in (tmp_path / "fleet" / ".env", tmp_path / "runtime" / "worker" / ".env"):
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "fleet-secret" in (tmp_path / "fleet" / ".env").read_text()
    assert [p.name for p in (tmp_path / "fleet").iterdir()] == [".env"]


def test_migration_keeps_unrelated_keys_and_migrated_value_wins(tmp_path):
    bot = tmp_path / "runtime" / "worker"
    bot.mkdir(parents=True)
    (bot / ".env").write_text("export BOT_TOKEN=mine\nexport OTHER=keep\n")
    _migrate(tmp_path, fleet_vars={})
    values = dotenv.read(bot / ".env")
    assert values["BOT_TOKEN"] == "bot-secret" and values["OTHER"] == "keep"


def test_migration_skips_bot_without_runtime_folder(tmp_path):
    assert _migrate(tmp_path) == 0
    assert not (tmp_path / "runtime").exists()


def test_migration_refuses_symlink_and_writes_nothing(tmp_path):
    bot = tmp_path / "runtime" / "worker"
    bot.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    (bot / ".env").symlink_to(target)
    with pytest.raises(CommandFailure):
        _migrate(tmp_path)
    assert target.read_text() == "untouched"
    assert (bot / ".env").is_symlink()
    assert not (tmp_path / "fleet" / ".env").exists()


def test_migration_refuses_directory_at_destination(tmp_path):
    (tmp_path / "fleet").mkdir()
    (tmp_path / "fleet" / ".env").mkdir()
    with pytest.raises(CommandFailure):
        _migrate(tmp_path, bot_vars={})
    assert (tmp_path / "fleet" / ".env").is_dir()


def test_failed_migration_write_leaves_old_file_whole(tmp_path, monkeypatch):
    bot = tmp_path / "runtime" / "worker"
    bot.mkdir(parents=True)
    (bot / ".env").write_text("export KEEP=1\n")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        _migrate(tmp_path, fleet_vars={})
    assert (bot / ".env").read_text() == "export KEEP=1\n"
    assert [p.name for p in bot.iterdir()] == [".env"]


def _stub():
    return [EnvVar(name="NEW_VAR", description="a var", default_tier="bot", source="x")]


def test_scaffold_writes_private_from_first_byte(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    seen: list[int] = []
    _spy_replace(monkeypatch, seen)
    _scaffold_env_merge(env, "# header", _stub())
    assert seen == [0o600]
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "export NEW_VAR=" in env.read_text()


def test_scaffold_preserves_existing_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("export EXISTING=secret\n")
    _scaffold_env_merge(env, "# header", _stub())
    text = env.read_text()
    assert "export EXISTING=secret" in text and "export NEW_VAR=" in text
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_scaffold_refuses_symlink_and_directory(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    link = tmp_path / "link.env"
    link.symlink_to(target)
    with pytest.raises(CommandFailure):
        _scaffold_env_merge(link, "# header", _stub())
    assert target.read_text() == "untouched" and link.is_symlink()
    folder = tmp_path / "dir.env"
    folder.mkdir()
    with pytest.raises(CommandFailure):
        _scaffold_env_merge(folder, "# header", _stub())
    assert folder.is_dir()


def test_scaffold_failed_write_leaves_old_file_whole(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("export EXISTING=secret\n")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        _scaffold_env_merge(env, "# header", _stub())
    assert env.read_text() == "export EXISTING=secret\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"]
