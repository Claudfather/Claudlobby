"""Generated tools have owned bytes and mode; preview never repairs either."""
import os
import shutil
import stat
from pathlib import Path

import pytest

from claudlobby.composer import compose_bot, compose_tool_outputs, compose_tools
from claudlobby.config import GithubAppConfig, ToolEntry, load_fleet
from claudlobby.diff import diff_bot
from claudlobby.paths import Paths


@pytest.fixture
def tool_scene(fleet_dir, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('PLANE_EMIT_DISABLED', '1')
    lib = fleet_dir / 'lib'
    lib.mkdir()
    source = Path(__file__).resolve().parents[1] / 'lib'
    for name in ('env-tiers.sh', 'lib-common.sh', 'supervisor.sh'):
        shutil.copy(source / name, lib / name)
    paths = Paths(root=fleet_dir)
    fleet, _ = load_fleet(paths.fleet_yaml)
    bot = fleet.bots['lead']
    tool = fleet_dir / 'library/tools/hello'
    tool.mkdir(parents=True)
    (tool / 'tool.yaml').write_text('type: script\n')
    template = tool / 'hello.sh.j2'
    template.write_text('#!/bin/sh\nprintf "%s\\n" "{{ bot_id }}"\n')
    bot.tools = [ToolEntry('hello')]
    compose_bot(bot, fleet, paths)
    target = paths.bot_runtime('lead') / 'tools/hello.sh'
    assert target.read_text() == compose_tool_outputs(bot, fleet, paths, target.parent.parent)['hello.sh']
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    return fleet, paths, target, template, home


def preview(scene):
    return diff_bot('lead', scene[0], scene[1])


def snapshot(*roots):
    result = {}
    for root in roots:
        for path in [root, *sorted(root.rglob('*'))]:
            node = path.lstat()
            value = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
            result[str(path)] = node.st_mode, node.st_size, node.st_mtime_ns, value
    return result


@pytest.mark.parametrize('mode', [0o644, 0o700, 0o775, 0o777, 0o4755, 0o2755, 0o1755])
def test_mode_only_drift_matches_writer_repair_and_is_read_only(tool_scene, mode):
    fleet, paths, target, _, home = tool_scene
    original = target.read_bytes()
    # macOS clears setgid for an inherited group the user does not belong to.
    os.chown(target, -1, os.getgid())
    target.chmod(mode)
    assert stat.S_IMODE(target.stat().st_mode) == mode
    before = snapshot(paths.root, home)
    output = preview(tool_scene)
    assert 'tools/hello.sh mode drift in lead' in output
    assert 'expected 0755' in output and f'current {mode:04o}' in output
    assert 'no drift in lead' not in output
    assert before == snapshot(paths.root, home)
    # The unchanged real writer, not a test chmod, supplies the parity oracle.
    compose_tools(fleet.bots['lead'], fleet, paths, target.parent.parent)
    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert 'no drift in lead' in preview(tool_scene)


def test_clean_0755_and_mutable_data_modes_are_not_drift(tool_scene):
    _, paths, target, _, home = tool_scene
    mutable = target.parent.parent / 'data/owned.txt'
    mutable.write_text('bot-owned data')
    mutable.chmod(0o600)
    before = snapshot(paths.root, home)
    assert 'no drift in lead' in preview(tool_scene)
    assert before == snapshot(paths.root, home)


def test_empty_rendered_file_still_requires_writer_mode(tool_scene):
    fleet, paths, target, template, _ = tool_scene
    template.write_text('')
    compose_tools(fleet.bots['lead'], fleet, paths, target.parent.parent)
    target.chmod(0o644)
    assert target.read_bytes() == b''
    assert 'tools/hello.sh mode drift in lead' in preview(tool_scene)


def test_content_and_mode_drift_are_both_visible(tool_scene):
    target = tool_scene[2]
    target.write_text('# runtime edit\n')
    target.chmod(0o644)
    output = preview(tool_scene)
    assert 'tools/hello.sh drift in lead' in output and 'runtime edit' in output
    assert 'tools/hello.sh mode drift in lead' in output


def test_extra_file_has_no_expected_0755_mode(tool_scene):
    extra = tool_scene[2].parent / 'stray.sh'
    extra.write_text('stray script\n')
    extra.chmod(0o600)
    output = preview(tool_scene)
    assert 'tools/stray.sh drift in lead' in output
    assert 'tools/stray.sh mode drift' not in output


def test_symlink_uses_target_mode_like_existing_writer(tool_scene):
    _, paths, target, _, home = tool_scene
    backing = paths.root / 'backing.sh'
    target.rename(backing)
    target.symlink_to(backing)
    backing.chmod(0o644)
    before = snapshot(paths.root, home)
    assert 'tools/hello.sh mode drift in lead' in preview(tool_scene)
    assert before == snapshot(paths.root, home)


@pytest.mark.parametrize('failure', ['metadata', 'contents', 'directory'])
def test_unreadable_tool_is_named_without_raw_exception(tool_scene, monkeypatch, failure):
    target = tool_scene[2]
    original = Path.stat if failure == 'metadata' else Path.read_text if failure == 'contents' else Path.iterdir
    denied_path = target.parent if failure == 'directory' else target
    def denied(self, *args, **kwargs):
        if self == denied_path:
            raise PermissionError('private-secret-diagnostic')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat' if failure == 'metadata' else 'read_text' if failure == 'contents' else 'iterdir', denied)
    output = preview(tool_scene)
    assert 'tools' in output and 'unavailable' in output and 'PermissionError' in output
    assert 'private-secret-diagnostic' not in output and 'no drift in lead' not in output


def test_missing_empty_output_is_not_a_clean_mode_check(tool_scene):
    fleet, paths, target, template, _ = tool_scene
    template.write_text('')
    compose_tools(fleet.bots['lead'], fleet, paths, target.parent.parent)
    target.unlink()
    output = preview(tool_scene)
    assert 'tools/hello.sh' in output and 'missing' in output
    assert 'no drift in lead' not in output


def test_nonregular_declared_tool_is_unavailable(tool_scene):
    target = tool_scene[2]
    target.unlink()
    target.mkdir()
    output = preview(tool_scene)
    assert 'tools/hello.sh' in output and 'unavailable' in output
    assert 'no drift in lead' not in output


def test_empty_extra_file_is_reported_for_removal(tool_scene):
    extra = tool_scene[2].parent / 'empty-stray.sh'
    extra.touch()
    output = preview(tool_scene)
    assert 'tools/empty-stray.sh drift in lead' in output
    assert 'tools/empty-stray.sh mode drift' not in output


def test_undeclared_nonregular_entry_keeps_existing_writer_semantics(tool_scene):
    _, paths, target, _, home = tool_scene
    extra = target.parent / 'preserved-directory'
    extra.mkdir()
    (extra / 'unowned-data').write_text('not a generated tool')
    before = snapshot(paths.root, home)
    assert 'no drift in lead' in preview(tool_scene)
    assert before == snapshot(paths.root, home)


def test_symlink_writer_repairs_target_mode_without_replacing_link(tool_scene):
    fleet, paths, target, _, _ = tool_scene
    backing = paths.root / 'writer-target.sh'
    target.rename(backing)
    target.symlink_to(backing)
    backing.chmod(0o644)
    assert 'tools/hello.sh mode drift in lead' in preview(tool_scene)
    compose_tools(fleet.bots['lead'], fleet, paths, target.parent.parent)
    assert target.is_symlink()
    assert stat.S_IMODE(backing.stat().st_mode) == 0o755
    assert 'no drift in lead' in preview(tool_scene)


def test_implicit_github_app_shim_uses_the_same_mode_contract(tool_scene):
    fleet, paths, target, _, _ = tool_scene
    bot = fleet.bots['lead']
    bot.github_app = GithubAppConfig()
    compose_bot(bot, fleet, paths)
    shim = target.parent / 'gh'
    assert shim.is_file() and stat.S_IMODE(shim.stat().st_mode) == 0o755
    assert all(tool.name != 'gh' for tool in bot.tools)
    shim.chmod(0o644)
    assert 'tools/gh mode drift in lead' in preview(tool_scene)


def test_undeclared_fifo_is_preserved_and_never_opened(tool_scene):
    _, paths, target, _, home = tool_scene
    os.mkfifo(target.parent / 'preserved-pipe')
    before = snapshot(paths.root, home)
    assert 'no drift in lead' in preview(tool_scene)
    assert before == snapshot(paths.root, home)
