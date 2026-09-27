"""Value-free env preview shares the writer's decisions without writing."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from claudlobby import composer
from claudlobby.commands import core
from claudlobby.config import BotConfig, FleetConfig
from claudlobby.paths import Paths


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path.resolve() / 'root'
    overlay = root / 'local/example'
    overlay.mkdir(parents=True)
    home = tmp_path.resolve() / 'home'; home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    paths = Paths(root=root, fleet_dir=overlay)
    fleet = FleetConfig(name='example', service_prefix='fixture', bots={
        name: BotConfig(bot_id=name, name=name, expertise=[]) for name in ('first', 'second')})
    requirements = [composer.EnvVar('FLEET_KEY', 'PRIVATE_DESCRIPTION', 'fleet', 'PRIVATE_SOURCE'),
                    composer.EnvVar('BOT_KEY', 'PRIVATE_DESCRIPTION', 'bot', 'PRIVATE_SOURCE')]
    monkeypatch.setattr(composer, 'collect_env_contracts', lambda *_: requirements)
    monkeypatch.setattr(core, '_resolve_paths', lambda _: paths)
    monkeypatch.setattr(core, '_load_env', lambda _: None)
    monkeypatch.setattr(core, '_load_fleet_or_exit', lambda _: (fleet, {}))
    monkeypatch.setattr(core, 'diff_bot', lambda *_: '')
    monkeypatch.setattr('claudlobby.diff.manifest_header', lambda *_: '')
    monkeypatch.setattr('claudlobby.diff.diff_fleet_timers', lambda *_: '')
    monkeypatch.setattr('claudlobby.host_guard_lists.diff_host_guard_lists', lambda *_: '')
    monkeypatch.setattr('claudlobby.directory_plan.diff_directory_creations', lambda *_a, **_kw: '')
    return paths, fleet, requirements, home


def preview(capsys, bot=None):
    assert core.cmd_diff(argparse.Namespace(bot=bot)) == 0
    return capsys.readouterr().out


def test_public_preview_discloses_fleet_write_and_conditional_bot_plans(scene, capsys):
    paths, _, _, _ = scene
    out = preview(capsys)
    assert 'Environment scaffold intentions' in out
    assert 'FLEET_KEY' in out and 'content write' in out
    assert 'conditional on earlier fleet .env' in out
    assert 'runtime/bots/first/.env' in out and 'runtime/bots/second/.env' in out
    assert 'PRIVATE_' not in out
    assert not (paths.fleet_dir / '.env').exists()
    assert not paths.runtime.exists()


def test_public_selected_bot_requests_no_env_scaffolding(scene, capsys, monkeypatch):
    monkeypatch.setattr(composer, 'collect_env_contracts', lambda *_: pytest.fail('contract read for --bot'))
    out = preview(capsys, 'first')
    assert 'no environment scaffolding requested' in out


def test_public_stable_fleet_allows_value_free_bot_plan(scene, capsys):
    paths, _, _, _ = scene
    (paths.fleet_dir / '.env').write_text('export FLEET_KEY=PRIVATE_VALUE\n')
    out = preview(capsys)
    assert 'BOT_KEY' in out and 'additions' in out and '0600' in out
    assert 'conditional' not in out
    assert 'PRIVATE_' not in out


def test_public_unreadable_values_are_unavailable_without_exception_leak(scene, capsys, monkeypatch):
    paths, _, _, _ = scene
    target = paths.fleet_dir / '.env'; target.write_text('PRIVATE_VALUE')
    read = Path.read_text
    def denied(path, *a, **kw):
        if path == target:
            raise PermissionError('PRIVATE_EXCEPTION')
        return read(path, *a, **kw)
    monkeypatch.setattr(Path, 'read_text', denied)
    out = preview(capsys)
    assert 'preview unavailable' in out and 'conditional on earlier fleet .env' in out
    assert 'PRIVATE_' not in out


def test_writer_retains_two_reads_and_original_key_content_disagreement(tmp_path, monkeypatch):
    target = tmp_path / '.env'; target.write_text('on disk')
    calls = []
    def read(path, *a, **kw):
        assert path == target
        calls.append(path)
        return 'A=first-read\n' if len(calls) == 1 else 'B=second-read\n'
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', read)
        composer._scaffold_env_merge(target, '# header', [composer.EnvVar('A', 'desc', 'fleet', '')])
    assert len(calls) == 2
    assert target.read_text() == 'A=first-read\n\n# desc\nexport A=\n'
    assert target.stat().st_mode & 0o777 == 0o600


def test_bot_upstream_is_read_once_after_fleet_write_even_for_empty_attachment(scene, monkeypatch):
    paths, fleet, requirements, _ = scene
    requirements[1].bot_attached = frozenset({'not-in-fleet'})
    order = []
    def upstream(p, *, for_tier='bot'):
        order.append(('upstream', for_tier))
        return frozenset()
    monkeypatch.setattr(composer, '_upstream_env_names', upstream)
    monkeypatch.setattr(composer, '_scaffold_env_merge', lambda p, *_a, **_kw: order.append(('write', p)))
    composer.scaffold_env_files(fleet, paths)
    assert order == [('upstream', 'fleet'), ('write', paths.fleet_dir / '.env'), ('upstream', 'bot')]


def test_writer_failure_prevents_later_reads_and_bot_writes(scene, monkeypatch):
    paths, fleet, _, _ = scene
    tiers = []
    monkeypatch.setattr(composer, '_upstream_env_names', lambda _p, **kw: tiers.append(kw.get('for_tier', 'bot')) or frozenset())
    def broken(*_a, **_kw): raise PermissionError('original error preserved')
    monkeypatch.setattr(composer, '_scaffold_env_merge', broken)
    with pytest.raises(PermissionError, match='original error preserved'):
        composer.scaffold_env_files(fleet, paths)
    assert tiers == ['fleet']


def test_duplicate_rewrite_requires_conditional_bot_preview(scene, capsys):
    paths, fleet, requirements, home = scene
    requirements[0].name = 'SHARED'
    requirements[1].name = 'SHARED'
    (home / '.env').write_text('export SHARED=PRIVATE_HOST\n')
    (paths.fleet_dir / '.env').write_text('SHARED=PRIVATE_OLDER\nexport SHARED=\n')
    before = composer._upstream_env_names(paths)
    assert 'SHARED' in before  # existing union policy includes the host value
    out = preview(capsys)
    assert 'pristine stubs commented 1' in out
    assert 'conditional on earlier fleet .env' in out
    assert 'PRIVATE_' not in out


@pytest.mark.parametrize('kind', ['link', 'dangling', 'directory', 'fifo', 'ancestor-link'])
def test_unsupported_topology_never_reads_target(scene, capsys, monkeypatch, kind):
    import os
    paths, _, _, _ = scene
    target = paths.fleet_dir / '.env'
    outside = paths.root.parent / 'outside'; outside.write_text('PRIVATE_VALUE')
    if kind in ('link', 'dangling'):
        target.symlink_to(outside if kind == 'link' else outside.with_name('absent'))
    elif kind == 'directory': target.mkdir()
    elif kind == 'fifo': os.mkfifo(target)
    else:
        alias = paths.root.parent / 'alias'; alias.symlink_to(paths.root, target_is_directory=True)
        monkeypatch.setattr(core, '_resolve_paths', lambda _: Paths(root=alias, fleet_dir=alias/'local/example'))
    monkeypatch.setattr(Path, 'read_text', lambda *_a, **_kw: pytest.fail('unsupported content read'))
    out = preview(capsys)
    assert 'preview unavailable' in out
    assert 'PRIVATE_' not in out


def test_preview_preserves_namespace_bytes_mode_mtime_and_hides_payloads(scene, capsys, monkeypatch):
    paths, _, _, _ = scene
    target = paths.fleet_dir / '.env'; target.write_text('export FLEET_KEY=PRIVATE_VALUE\n'); target.chmod(0o644)
    before = (target.read_bytes(), target.stat().st_mode, target.stat().st_mtime_ns)
    for method in ('write_text', 'write_bytes', 'mkdir', 'chmod', 'unlink', 'rename', 'symlink_to'):
        monkeypatch.setattr(Path, method, lambda *_a, **_kw: pytest.fail('preview mutation'))
    out = preview(capsys)
    assert 'mode 0600 (change)' in out and 'PRIVATE_' not in out
    assert before == (target.read_bytes(), target.stat().st_mode, target.stat().st_mtime_ns)
    assert not paths.runtime.exists()


def test_plan_repr_and_preview_hide_invalid_contract_names(scene, capsys):
    from claudlobby.env_scaffold import plan_env_merge
    _, _, requirements, _ = scene
    requirements[0].name = 'BAD-PRIVATE_NAME'
    plan = plan_env_merge('SECRET=PRIVATE_VALUE\n', {'SECRET'}, 'PRIVATE_HEADER', requirements, frozenset())
    assert 'PRIVATE' not in repr(plan)
    out = preview(capsys)
    assert 'invalid name(s) hidden' in out
    assert 'PRIVATE' not in out


@pytest.mark.parametrize('tier', ['host', 'root', 'fleet'])
def test_bad_utf8_and_metadata_errors_never_look_clean(scene, capsys, monkeypatch, tier):
    paths, _, _, home = scene
    target = {'host': home / '.env', 'root': paths.root / '.env', 'fleet': paths.fleet_dir / '.env'}[tier]
    target.write_bytes(b'SECRET=\xff\n')
    out = preview(capsys)
    assert 'preview unavailable' in out and 'conditional on earlier fleet .env' in out
    assert 'SECRET' not in out and '\\xff' not in out
    old = Path.lstat
    def denied(path, *a, **kw):
        if path == target: raise PermissionError('PRIVATE_METADATA')
        return old(path, *a, **kw)
    monkeypatch.setattr(Path, 'lstat', denied)
    out = preview(capsys)
    assert 'unreadable metadata' in out and 'PRIVATE_METADATA' not in out


def test_linked_upstream_is_unavailable_before_any_value_read(scene, capsys, monkeypatch):
    paths, _, _, home = scene
    outside = home / 'outside'; outside.write_text('SECRET=PRIVATE_VALUE\n')
    (home / '.env').symlink_to(outside)
    monkeypatch.setattr(Path, 'read_text', lambda *_a, **_kw: pytest.fail('linked upstream read'))
    out = preview(capsys)
    assert 'symlink topology is unsupported' in out and 'PRIVATE' not in out


def test_contract_error_is_disclosed_without_tokens(scene, capsys, monkeypatch):
    def broken(*_): raise ValueError('PRIVATE_CONTRACT_TOKEN')
    monkeypatch.setattr(composer, 'collect_env_contracts', broken)
    out = preview(capsys)
    assert 'requirements could not be inspected' in out and 'PRIVATE' not in out
