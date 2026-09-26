"""Directory intentions are visible without inspecting bot-owned contents."""
from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

import pytest

from claudlobby import composer
from claudlobby.commands import core
from claudlobby.config import BotConfig, FleetConfig
from claudlobby.paths import Paths


BOT_DIRS = ("", ".claude", "memory", "projects", "data", "data/events", "logs")
SHARED_DIRS = ("planning/active", "planning/completed", "decisions", "knowledge", "runbooks")


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / "root"
    overlay = root / "local" / "example"
    overlay.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    monkeypatch.setenv("PLANE_EMIT_ENABLED", "0")
    paths = Paths(root=root, fleet_dir=overlay)
    fleet = FleetConfig(name="example", service_prefix="fixture", bots={
        name: BotConfig(bot_id=name, name=name, expertise=[])
        for name in ("first", "second")
    })
    monkeypatch.setattr(core, "_resolve_paths", lambda _: paths)
    monkeypatch.setattr(core, "_load_env", lambda _: None)
    monkeypatch.setattr(core, "_load_fleet_or_exit", lambda _: (fleet, {}))
    # Other output families have separate tests; no renderer/writer from them
    # should run in this public-command dispatch regression.
    monkeypatch.setattr(core, "diff_bot", lambda *_: "other bot artifacts\n")
    monkeypatch.setattr("claudlobby.diff.manifest_header", lambda *_: "manifest\n")
    monkeypatch.setattr("claudlobby.diff.diff_fleet_timers", lambda *_: "")
    monkeypatch.setattr("claudlobby.host_guard_lists.diff_host_guard_lists", lambda *_: "")
    return paths, fleet


def preview(capsys, bot=None):
    assert core.cmd_diff(argparse.Namespace(bot=bot)) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("selected", [None, "first"])
def test_public_directory_preview_lists_bot_creations(scene, capsys, selected):
    paths, _ = scene
    out = preview(capsys, selected)
    assert out.count("=== Directory creation intentions") == 1
    for relative in BOT_DIRS:
        target = "runtime/bots/first" + ("/" + relative if relative else "")
        assert target + ": missing" in out
    assert ("runtime/bots/second: missing" in out) == (selected is None)
    assert "metadata only" in out
    assert "earlier validation or writes may prevent later steps" in out
    assert not paths.runtime.exists()


@pytest.mark.parametrize("selected", [None, "first"])
def test_only_full_fleet_previews_shared_document_scaffolding(scene, capsys, selected):
    paths, _ = scene
    out = preview(capsys, selected)
    assert ("runtime/bots: missing" in out) == (selected is None)
    for relative in SHARED_DIRS:
        assert ("shared/" + relative + ": missing" in out) == (selected is None)
    assert not paths.shared_docs.exists()


def test_root_mode_has_no_shared_document_intentions(scene, capsys, monkeypatch):
    paths, _ = scene
    monkeypatch.setattr(core, "_resolve_paths", lambda _: Paths(root=paths.root))
    out = preview(capsys)
    assert "runtime/bots/first: missing" in out
    assert "shared/" not in out


def test_empty_fleet_still_previews_fleet_directories(scene, capsys):
    _, fleet = scene
    fleet.bots.clear()
    out = preview(capsys)
    assert "runtime/bots: missing" in out
    assert "shared/planning/active: missing" in out


def test_unknown_selected_bot_has_no_directory_intentions(scene, capsys):
    assert "Directory creation intentions" not in preview(capsys, "unknown")


@pytest.mark.parametrize("relative", ["runtime", "runtime/bots", "runtime/bots/first", "runtime/bots/first/memory", "runtime/bots/first/data", "shared/planning"])
def test_wrong_type_obstacles_are_named_without_opening_them(scene, capsys, monkeypatch, relative):
    paths, _ = scene
    node = paths.fleet_dir / relative
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text("PRIVATE_CONTENT")
    monkeypatch.setattr(Path, "read_text", lambda *_a, **_k: pytest.fail("content read"))
    out = preview(capsys)
    assert "blocked" in out and "not a directory" in out
    assert "PRIVATE_CONTENT" not in out


@pytest.mark.parametrize("relative", ["runtime", "runtime/bots/first", "runtime/bots/first/memory", "runtime/bots/first/data/events", "shared", "shared/planning"])
@pytest.mark.parametrize("dangling", [False, True])
def test_links_are_unavailable_and_never_followed(scene, capsys, monkeypatch, relative, dangling):
    paths, _ = scene
    target = paths.root.parent / "outside"
    if not dangling:
        target.mkdir()
    node = paths.fleet_dir / relative
    node.parent.mkdir(parents=True, exist_ok=True)
    node.symlink_to(target, target_is_directory=True)
    original = Path.lstat
    def no_follow(path, *a, **kw):
        assert not path.is_relative_to(node) or path == node, "followed a linked ancestor"
        return original(path, *a, **kw)
    monkeypatch.setattr(Path, "lstat", no_follow)
    out = preview(capsys)
    assert "preview unavailable" in out and "symlink" in out


@pytest.mark.parametrize("relative", ["runtime", "runtime/bots/first/memory", "shared/planning"])
def test_unreadable_metadata_is_not_absence(scene, capsys, monkeypatch, relative):
    paths, _ = scene
    node = paths.fleet_dir / relative
    node.parent.mkdir(parents=True, exist_ok=True)
    original = Path.lstat
    def denied(path, *a, **kw):
        if path == node:
            raise PermissionError("PRIVATE_ERROR")
        return original(path, *a, **kw)
    monkeypatch.setattr(Path, "lstat", denied)
    out = preview(capsys)
    assert "preview unavailable" in out and "unreadable metadata" in out
    assert "PRIVATE_ERROR" not in out


def test_fifo_obstacle_is_not_opened(scene, capsys):
    paths, _ = scene
    node = paths.bot_runtime("first") / "memory"
    node.parent.mkdir(parents=True)
    os.mkfifo(node)
    assert "runtime/bots/first/memory: blocked" in preview(capsys, "first")
    assert stat.S_ISFIFO(node.lstat().st_mode)


def test_linked_root_ancestor_is_unavailable_before_descendant_metadata(scene, capsys, monkeypatch):
    paths, _ = scene
    alias = paths.root.parent / "alias"
    alias.symlink_to(paths.root, target_is_directory=True)
    linked = Paths(root=alias, fleet_dir=alias / "local" / "example")
    monkeypatch.setattr(core, "_resolve_paths", lambda _: linked)
    original = Path.lstat
    def no_follow(path, *a, **kw):
        assert not path.is_relative_to(alias) or path == alias
        return original(path, *a, **kw)
    monkeypatch.setattr(Path, "lstat", no_follow)
    out = preview(capsys)
    assert "preview unavailable" in out and "symlink" in out


def test_existing_directories_preserve_contents_and_modes_without_traversal(scene, capsys, monkeypatch):
    paths, _ = scene
    bot = paths.bot_runtime("first")
    for relative in BOT_DIRS:
        (bot / relative).mkdir(parents=True, exist_ok=True)
    owned = [bot / name for name in ("memory", "projects", "data", "logs")]
    for directory in owned:
        (directory / "private").write_text("PRIVATE_CONTENT")
        directory.chmod(0o700)
    before = {p: (p.stat().st_mode, p.stat().st_mtime_ns) for p in owned}
    for method in ("iterdir", "rglob", "read_text", "read_bytes", "mkdir", "chmod", "write_text"):
        monkeypatch.setattr(Path, method, lambda *_a, **_k: pytest.fail("content or mutation operation"))
    out = preview(capsys, "first")
    assert "runtime/bots/first/memory: exists" in out
    assert "contents not inspected" in out
    assert "mode drift" not in out and "PRIVATE_CONTENT" not in out
    assert before == {p: (p.stat().st_mode, p.stat().st_mtime_ns) for p in owned}


class StopAfterDirectories(Exception):
    pass


def _stop_after_bot_directories(monkeypatch):
    monkeypatch.setattr(composer, "_load_bot_fragments", lambda *_: [])
    monkeypatch.setattr("claudlobby.path_audit.assert_bot_sources", lambda *_: None)
    def stop(*_a, **_k):
        raise StopAfterDirectories
    monkeypatch.setattr(composer, "compose_claude_md", stop)


@pytest.mark.parametrize("obstacle", [None, ".claude", "memory", "data", "data/events", "logs"])
def test_writer_retains_directory_order_and_partial_failure(scene, monkeypatch, obstacle):
    paths, fleet = scene
    bot_dir = paths.bot_runtime("first")
    if obstacle:
        path = bot_dir / obstacle
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("obstacle")
    _stop_after_bot_directories(monkeypatch)
    expected = FileExistsError if obstacle else StopAfterDirectories
    with pytest.raises(expected):
        composer.compose_bot(fleet.bots["first"], fleet, paths, boot_delay_s=0)
    if obstacle:
        assert (bot_dir / obstacle).read_text() == "obstacle"
    limit = BOT_DIRS.index(obstacle) if obstacle else len(BOT_DIRS)
    for relative in BOT_DIRS[:limit]:
        assert (bot_dir / relative).is_dir()
    for relative in BOT_DIRS[limit + 1:]:
        assert not (bot_dir / relative).exists()


def test_writer_source_audit_still_precedes_bot_directory_creation(scene, monkeypatch):
    paths, fleet = scene
    monkeypatch.setattr(composer, "_load_bot_fragments", lambda *_: [])
    def denied(*_a):
        raise ValueError("fixture source rejected")
    monkeypatch.setattr("claudlobby.path_audit.assert_bot_sources", denied)
    with pytest.raises(ValueError, match="fixture source rejected"):
        composer.compose_bot(fleet.bots["first"], fleet, paths, boot_delay_s=0)
    assert not paths.runtime.exists()


def test_writer_respects_umask_and_existing_directory_modes(scene, monkeypatch):
    paths, fleet = scene
    memory = paths.bot_runtime("first") / "memory"
    memory.mkdir(parents=True)
    memory.chmod(0o750)
    _stop_after_bot_directories(monkeypatch)
    old = os.umask(0o077)
    try:
        with pytest.raises(StopAfterDirectories):
            composer.compose_bot(fleet.bots["first"], fleet, paths, boot_delay_s=0)
    finally:
        os.umask(old)
    assert stat.S_IMODE(memory.stat().st_mode) == 0o750
    assert stat.S_IMODE((memory.parent / "data").stat().st_mode) == 0o700


@pytest.mark.parametrize("obstacle", [None, "planning/completed", "knowledge"])
def test_writer_fleet_scaffolding_precedes_resolver_and_preserves_failure(scene, monkeypatch, obstacle):
    paths, fleet = scene
    if obstacle:
        node = paths.shared_docs / obstacle
        node.parent.mkdir(parents=True)
        node.write_text("obstacle")
    def stop(*_a, **_k):
        raise StopAfterDirectories
    monkeypatch.setattr(composer, "_bot_conf_cascade", stop)
    with pytest.raises(FileExistsError if obstacle else StopAfterDirectories):
        composer.compose_fleet(fleet, paths)
    assert paths.runtime_bots.is_dir()
    limit = SHARED_DIRS.index(obstacle) if obstacle else len(SHARED_DIRS)
    for relative in SHARED_DIRS[:limit]:
        assert (paths.shared_docs / relative).is_dir()
    for relative in SHARED_DIRS[limit + 1:]:
        assert not (paths.shared_docs / relative).exists()
