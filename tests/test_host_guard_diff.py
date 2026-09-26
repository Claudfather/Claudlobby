"""Read-only host guard-list preview through the public diff command (#909)."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from claudlobby import composer
from claudlobby.commands import core
from claudlobby.config import BotConfig, FleetConfig
from claudlobby.paths import Paths


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    monkeypatch.setenv("PLANE_EMIT_ENABLED", "0")
    paths = Paths(root=root)
    for relative, names, allowed in [
        ("alpha", ["zulu", "worker-1"], ["reviewer", "owner"]),
        ("group/beta", ["beta", "worker-1"], ["owner", "maintainer"]),
    ]:
        manifest = root / "local" / relative / "fleet.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(yaml.safe_dump({"fleet": {
            "name": relative.rsplit("/", 1)[-1], "bots": dict.fromkeys(names, {}),
            "github": {"mention_allowlist": allowed},
        }}))
    fleet = FleetConfig(name="alpha", service_prefix="test.alpha", bots={"first": BotConfig(name="first", bot_id="first", expertise=[]),
                                          "second": BotConfig(name="second", bot_id="second", expertise=[])})
    monkeypatch.setattr(core, "_resolve_paths", lambda _: paths)
    monkeypatch.setattr(core, "_load_env", lambda _: None)
    monkeypatch.setattr(core, "_load_fleet_or_exit", lambda _: (fleet, {}))
    # Other owned diff families have their own tests. For this public dispatch
    # regression, no renderer or external door outside this slice may run.
    monkeypatch.setattr(core, "diff_bot", lambda name, *_: f"bot {name}\n")
    monkeypatch.setattr("claudlobby.diff.manifest_header", lambda *_: "manifest\n")
    monkeypatch.setattr("claudlobby.diff.diff_fleet_timers", lambda *_: "timers\n")
    return paths


def run_diff(capsys, *, bot=None):
    assert core.cmd_diff(argparse.Namespace(bot=bot)) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("bot", [None, "first"])
def test_public_diff_reports_host_lists_once_including_single_bot(scene, capsys, bot):
    out = run_diff(capsys, bot=bot)
    assert out.count("=== Host guard lists") == 1
    assert "host-wide" in out and "on demand" in out
    assert "bot-handles: missing" in out
    assert "mention-allowlist: missing" in out
    assert "beta" in out and "maintainer" in out
    assert not (scene.root / "runtime").exists()


def test_public_diff_reports_runtime_removal_and_source_addition(scene, capsys):
    base = scene.root / "runtime" / "_host"
    base.mkdir(parents=True)
    (base / "bot-handles").write_text("retired\nworker-1\n")
    (base / "mention-allowlist").write_text("former-human\n")
    out = run_diff(capsys)
    assert "bot-handles: drift" in out and "mention-allowlist: drift" in out
    assert "+retired" in out and "-beta" in out
    assert "+former-human" in out and "-maintainer" in out


def test_writer_round_trip_and_unchanged_preview(scene, capsys):
    assert composer.compose_host_bot_handles(scene).read_text() == "beta\nworker-1\nzulu\n"
    assert composer.compose_host_mention_allowlist(scene).read_text() == "maintainer\nowner\nreviewer\n"
    out = run_diff(capsys)
    assert "bot-handles: unchanged" in out and "mention-allowlist: unchanged" in out
    assert "on demand" in out


def test_partial_manifest_is_unavailable_not_clean(scene, capsys):
    (scene.root / "local/alpha/fleet.yaml").write_text("fleet: [PRIVATE_SENTINEL")
    composer.compose_host_bot_handles(scene)
    composer.compose_host_mention_allowlist(scene)
    out = run_diff(capsys)
    assert "preview unavailable" in out
    assert "local/alpha/fleet.yaml" in out
    assert "PRIVATE_SENTINEL" not in out
    assert "unchanged" not in out


@pytest.mark.parametrize("body", ["[]", "false", "fleet: []", "fleet: false",
                                   "fleet:\n  bots: [one]", "fleet:\n  bots: {1: {}}",
                                   "fleet:\n  github: []", "fleet:\n  github: {mention_allowlist: human}",
                                   "fleet:\n  github: {mention_allowlist: [1]}"])
def test_invalid_shape_is_disclosed_without_raw_values(scene, capsys, body):
    (scene.root / "local/alpha/fleet.yaml").write_text(body)
    assert "preview unavailable" in run_diff(capsys)


@pytest.mark.parametrize("relative", ["local", "local/alpha", "local/alpha/fleet.yaml",
                                      "local/group", "local/group/beta", "local/group/beta/fleet.yaml",
                                      "runtime", "runtime/_host", "runtime/_host/bot-handles"])
def test_preview_never_follows_source_or_runtime_links(scene, capsys, monkeypatch, relative):
    import shutil
    path = scene.root / relative
    outside = scene.root.parent / "outside"
    outside.mkdir()
    sentinel = outside / "fleet.yaml"
    sentinel.write_text("PRIVATE_LINK_TARGET")
    if path.exists():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(sentinel if relative.endswith(("fleet.yaml", "bot-handles")) else outside)
    original = Path.read_text
    def bounded_read(path, *args, **kwargs):
        assert not path.resolve().is_relative_to(outside), "preview followed a link"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", bounded_read)
    out = run_diff(capsys)
    assert "preview unavailable" in out and "symlink" in out
    assert "PRIVATE_LINK_TARGET" not in out


@pytest.mark.parametrize("operation", ["read_text", "iterdir", "lstat"])
def test_unreadable_sources_never_report_clean(scene, capsys, monkeypatch, operation):
    original = getattr(Path, operation)
    target = scene.root / ("local/alpha/fleet.yaml" if operation == "read_text" else "local")
    def unreadable(path, *args, **kwargs):
        if path == target:
            raise PermissionError("PRIVATE_READ_ERROR")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, operation, unreadable)
    out = run_diff(capsys)
    assert out.count("preview unavailable") == 2
    assert "PRIVATE_READ_ERROR" not in out and "unchanged" not in out


@pytest.mark.parametrize("kind", ["directory", "fifo", "invalid-text", "unreadable"])
def test_unreadable_or_nonregular_runtime_is_disclosed(scene, capsys, monkeypatch, kind):
    import os
    target = scene.root / "runtime/_host/bot-handles"
    target.parent.mkdir(parents=True)
    if kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(b"\xff" if kind == "invalid-text" else b"data")
    if kind == "unreadable":
        original = Path.read_text
        def unreadable(path, *args, **kwargs):
            if path == target:
                raise PermissionError("PRIVATE_RUNTIME_ERROR")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", unreadable)
    out = run_diff(capsys)
    assert "bot-handles: preview unavailable" in out
    assert "PRIVATE_RUNTIME_ERROR" not in out


def test_preview_does_not_call_writers_or_change_files(scene, capsys, monkeypatch):
    import hashlib
    import os
    import stat
    composer.compose_host_bot_handles(scene)
    composer.compose_host_mention_allowlist(scene)
    def snapshot():
        result = {}
        for path in [scene.root, *sorted(scene.root.rglob("*"))]:
            meta = path.lstat()
            result[str(path)] = (meta.st_mode, meta.st_size, meta.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(meta.st_mode) else None)
        return result
    def forbidden(*args, **kwargs):
        raise AssertionError("preview called a writer")
    monkeypatch.setattr(composer, "compose_host_bot_handles", forbidden)
    monkeypatch.setattr(composer, "compose_host_mention_allowlist", forbidden)
    target = scene.root / "runtime/_host/bot-handles"
    target.write_text("drifted\n")
    os.chmod(target, 0o640)
    before = snapshot()
    assert "bot-handles: drift" in run_diff(capsys, bot="first")
    assert snapshot() == before  # atime deliberately excluded: reading can change it.


def test_discovery_stops_below_a_manifest_and_at_depth_two(scene, capsys):
    hidden = scene.root / "local/alpha/hidden/fleet.yaml"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("fleet: [PRIVATE_HIDDEN")
    too_deep = scene.root / "local/group/beta/deeper/fleet.yaml"
    too_deep.parent.mkdir(parents=True)
    too_deep.write_text("fleet: [PRIVATE_DEEP")
    composer.compose_host_bot_handles(scene)
    composer.compose_host_mention_allowlist(scene)
    out = run_diff(capsys)
    assert out.count("unchanged") == 2 and "PRIVATE" not in out


def test_present_empty_is_distinct_from_missing(scene, capsys):
    import shutil
    shutil.rmtree(scene.root / "local")
    # A root fleet is not currently included by either host writer. Preview
    # preserves that contract and names its local/ discovery boundary.
    (scene.root / "fleet.yaml").write_text("fleet:\n  bots: {rootbot: {}}\n")
    assert "bot-handles: missing" in run_diff(capsys)
    composer.compose_host_bot_handles(scene)
    composer.compose_host_mention_allowlist(scene)
    out = run_diff(capsys)
    assert out.count("unchanged") == 2
    assert "local/" in out and "rootbot" not in out


@pytest.mark.parametrize("filename,writer", [
    ("bot-handles", composer.compose_host_bot_handles),
    ("mention-allowlist", composer.compose_host_mention_allowlist),
])
def test_writer_skip_unreadable_sibling_is_preserved(scene, monkeypatch, filename, writer):
    original = Path.read_text
    target = scene.root / "local/alpha/fleet.yaml"
    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("hidden")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", denied)
    assert writer(scene).read_text() == ("beta\nworker-1\n" if filename == "bot-handles" else "maintainer\nowner\n")


def test_writer_collection_failure_preserves_no_output_directory(scene):
    (scene.root / "local/alpha/fleet.yaml").write_text("fleet: [truthy]\n")
    with pytest.raises(AttributeError):
        composer.compose_host_bot_handles(scene)
    assert not (scene.root / "runtime").exists()
