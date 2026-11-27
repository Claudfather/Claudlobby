"""Fleet, bot and scaffolded .env files go through the one private writer."""

from __future__ import annotations

import errno
import logging
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby import dotenv
from claudlobby.command_result import CommandFailure
from claudlobby.commands import env_migrate
from claudlobby.commands.setup import _sync_parent, _write_config
from claudlobby import composer
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


def _spy_writes(monkeypatch):
    """Record each fsync of a regular file, each chmod and each replace, in order.

    A file is named by (st_dev, st_ino), so the temp file can be followed through
    its rename to the file it became: a rename keeps the inode.
    """
    events: list[tuple] = []
    real_fsync, real_chmod, real_replace = os.fsync, os.chmod, os.replace

    def fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            events.append(("fsync", (info.st_dev, info.st_ino), stat.S_IMODE(info.st_mode)))
        return real_fsync(fd)

    def chmod(path, mode, *a, **k):
        info = os.stat(path)
        events.append(("chmod", (info.st_dev, info.st_ino), mode))
        return real_chmod(path, mode, *a, **k)

    def replace(src, dst, *a, **k):
        info = os.stat(src)
        events.append(("replace", (info.st_dev, info.st_ino), stat.S_IMODE(info.st_mode), Path(src), Path(dst)))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "chmod", chmod)
    monkeypatch.setattr(os, "replace", replace)
    return events


def _replaced_modes(events):
    return [event[2] for event in events if event[0] == "replace"]


def _assert_private_temp(events, destination: Path):
    """The temp file that became ``destination`` had no group or other bits at its fsync,
    before its chmod, and was renamed from the destination's own folder as .env.tmp-*."""
    info = destination.stat()
    own = [event for event in events if event[1] == (info.st_dev, info.st_ino)]
    assert [event[0] for event in own][:3] == ["fsync", "chmod", "replace"]
    assert own[0][2] & 0o077 == 0
    source, target = own[2][3], own[2][4]
    assert source.parent == target.parent == destination.parent
    assert source.name.startswith(".env.tmp-")


def test_old_names_still_importable_from_setup():
    assert _write_config is dotenv._write_config
    assert _sync_parent is dotenv._sync_parent


@pytest.mark.parametrize(
    ("before", "after"),
    [(0o640, 0o600), (0o644, 0o600), (0o400, 0o400), (None, 0o600)],
    ids=["0640-becomes-0600", "0644-becomes-0600", "0400-stays-0400", "new-file-0600"],
)
def test_writer_keeps_owner_bits_of_existing_file(tmp_path, monkeypatch, before, after):
    env = tmp_path / ".env"
    if before is not None:
        env.write_text("export OLD=1\n")
        env.chmod(before)
    events = _spy_writes(monkeypatch)
    dotenv._write_config(env, b"export NEW=1\n", replace=True, prefix=".env.tmp-")
    assert stat.S_IMODE(env.stat().st_mode) == after
    assert _replaced_modes(events) == [after]
    _assert_private_temp(events, env)
    assert env.read_bytes() == b"export NEW=1\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_folder_sync_failure_after_rename_warns_and_keeps_new_file(tmp_path, monkeypatch, caplog):
    env = tmp_path / ".env"
    env.write_text("export OLD=1\n")
    env.chmod(0o600)
    real_fsync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "folder sync failed")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with caplog.at_level(logging.WARNING, logger="claudlobby.dotenv"):
        dotenv._write_config(env, b"export NEW=1\n", replace=True, prefix=".env.tmp-")
    assert env.read_bytes() == b"export NEW=1\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [".env"]
    warnings = [r for r in caplog.records if r.name == "claudlobby.dotenv" and r.levelno == logging.WARNING]
    assert len(warnings) == 1 and str(env) in warnings[0].getMessage()


def test_migration_writes_private_files_atomically(tmp_path, monkeypatch):
    (tmp_path / "runtime" / "worker").mkdir(parents=True)
    events = _spy_writes(monkeypatch)
    assert _migrate(tmp_path) == 0
    assert _replaced_modes(events) == [0o600, 0o600]
    for env in (tmp_path / "fleet" / ".env", tmp_path / "runtime" / "worker" / ".env"):
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        _assert_private_temp(events, env)
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
    events = _spy_writes(monkeypatch)
    _scaffold_env_merge(env, "# header", _stub())
    assert _replaced_modes(events) == [0o600]
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    _assert_private_temp(events, env)
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


def test_scaffold_files_refuse_before_any_write(tmp_path, monkeypatch):
    """A symlinked bot .env stops scaffolding before the fleet .env is touched."""
    paths = _paths(tmp_path)
    paths.fleet_dir.mkdir()
    fleet_env = paths.fleet_dir / ".env"
    fleet_env.write_text("export KEEP=1\n")
    before = fleet_env.read_bytes()
    runtime = paths.bot_runtime("worker")
    runtime.mkdir(parents=True)
    (tmp_path / "elsewhere").write_text("export OTHER=1\n")
    (runtime / ".env").symlink_to(tmp_path / "elsewhere")
    contract = [
        EnvVar(name="FLEET_VAR", description="f", default_tier="fleet", source="x"),
        EnvVar(name="BOT_VAR", description="b", default_tier="bot", source="x"),
    ]
    monkeypatch.setattr(composer, "collect_env_contracts", lambda fleet, paths: contract)
    monkeypatch.setattr(composer, "_upstream_env_names", lambda *a, **k: frozenset())
    fleet = SimpleNamespace(name="example", bots=["worker"])
    with pytest.raises(CommandFailure) as caught:
        composer.scaffold_env_files(fleet, paths)
    assert "symlink" in caught.value.error.message
    assert fleet_env.read_bytes() == before
    assert (tmp_path / "elsewhere").read_text() == "export OTHER=1\n"
    assert [p.name for p in paths.fleet_dir.iterdir()] == [".env"]
