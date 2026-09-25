"""#953 source-tree observations keep package and fleet repositories distinct."""
import json
import subprocess
from pathlib import Path

import pytest

from claudlobby import composer
from claudlobby.config import FleetConfig
from claudlobby.paths import Paths


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def init(root):
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.name', 'Fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'fixture')


@pytest.fixture
def source_scene(tmp_path, monkeypatch):
    package = tmp_path / 'package'
    for name in ('claudlobby', 'templates', 'library', 'voices', 'lib'):
        (package / name).mkdir(parents=True)
        (package / name / 'source.txt').write_text(name)
    (package / 'claudlobby/system.yaml').write_text('defaults: {}\n')
    fleet_dir = tmp_path / 'vault' / 'fleet'
    fleet_dir.mkdir(parents=True)
    (fleet_dir / 'fleet.yaml').write_text('fleet:\n  name: example\n')
    (fleet_dir / 'library').mkdir()
    (fleet_dir / 'library/overlay.md').write_text('overlay')
    (fleet_dir / 'runtime/bots/worker').mkdir(parents=True)
    init(package)
    init(fleet_dir)
    monkeypatch.setattr(composer, '__file__', str(package / 'claudlobby/composer.py'))
    monkeypatch.setattr(composer, '_resolve_system_yaml', lambda _: package / 'claudlobby/system.yaml')
    return FleetConfig(name='example', service_prefix='example'), Paths(root=package, fleet_dir=fleet_dir), package


def test_library_edit_is_source_drift_without_manifest_drift(source_scene):
    fleet, paths, package = source_scene
    before = composer.write_manifest_provenance(fleet, paths)
    assert before['schema'] == 2
    assert before['sources']['package_root'] == str(package / 'claudlobby')
    (package / 'library/source.txt').write_text('changed library')
    after = composer.manifest_provenance(fleet, paths)
    assert before['files'] == after['files']
    assert before['git'] == after['git']
    assert before['sources'] != after['sources']
    from claudlobby.diff import manifest_header
    assert 'core sources: CHANGED' in manifest_header(fleet, paths)
    from claudlobby.doctor import DoctorReport, check_manifest_provenance
    report = DoctorReport();check_manifest_provenance(fleet, paths, report)
    assert report.checks[-1].status == 'warn'
    assert 'core source' in report.checks[-1].detail


def test_package_and_overlay_revision_are_independent(source_scene):
    fleet, paths, package = source_scene
    prov = composer.manifest_provenance(fleet, paths)
    sources = prov['sources']
    repos = {r['path']: r for r in sources['repositories']}
    assert repos[str(package)]['commit'] == git(package, 'rev-parse', 'HEAD')
    assert repos[str(paths.fleet_dir)]['commit'] == git(paths.fleet_dir, 'rev-parse', 'HEAD')
    assert repos[str(package)]['commit'] != repos[str(paths.fleet_dir)]['commit']
    assert repos[str(package)]['dirty'] is False
    (paths.fleet_dir / 'library/overlay.md').write_text('changed overlay')
    now = composer.manifest_provenance(fleet, paths)
    roots = {r['path']: r for r in sources['roots']}
    updated = {r['path']: r for r in now['sources']['roots']}
    assert roots[str(paths.fleet_dir / 'library')]['sha256'] != updated[str(paths.fleet_dir / 'library')]['sha256']
    assert roots[str(package / 'library')] == updated[str(package / 'library')]


def test_cache_secret_and_mtime_changes_do_not_move_digest(source_scene):
    import os
    fleet, paths, package = source_scene
    before = composer.manifest_provenance(fleet, paths)['sources']
    for rel in ('library/nested/__pycache__/x.pyc', 'library/build/thing', 'library/.pytest_cache/state',
                'library/.DS_Store', 'library/source.pyc', 'library/.env', 'library/.env.secret'):
        p = package / rel;p.parent.mkdir(parents=True, exist_ok=True);p.write_text('DO_NOT_CAPTURE')
    os.utime(package / 'library/source.txt', (1, 1))
    after = composer.manifest_provenance(fleet, paths)['sources']
    assert before == after, "cache-only changes, even below new empty directories, must not drift"
    roots_before = {r['path']: r for r in before['roots']}
    roots_after = {r['path']: r for r in after['roots']}
    assert all('DO_NOT_CAPTURE' not in json.dumps(r) for r in after['roots'])
    assert roots_before[str(package / 'claudlobby')] == roots_after[str(package / 'claudlobby')]
    before2 = composer.manifest_provenance(fleet, paths)['sources']
    (package / 'library/nested/__pycache__/another.pyc').write_text('other')
    assert before2 == composer.manifest_provenance(fleet, paths)['sources']
    assert all(r.get('dirty') is False for r in after['repositories'])


def test_symlink_boundaries_no_outside_or_secret_reads(source_scene, tmp_path, monkeypatch):
    import os
    from claudlobby import source_provenance as src
    fleet, paths, package = source_scene
    outside = tmp_path / 'outside';outside.write_text('FORBIDDEN_SECRET')
    secret = package / 'library/.env';secret.write_text('FORBIDDEN_SECRET')
    library = package / 'library'
    (library / 'safe').symlink_to(library / 'source.txt')
    (library / 'escape').symlink_to(outside)
    (library / 'secret-link').symlink_to(secret)
    (library / 'dangling').symlink_to(library / 'absent')
    (library / 'cycle-a').symlink_to(library / 'cycle-b')
    (library / 'cycle-b').symlink_to(library / 'cycle-a')
    (library / 'dir-loop').symlink_to(library, target_is_directory=True)
    original = src._safe_open
    opened = []
    def guarded(path, **kwargs):
        assert path not in (outside, secret), f'forbidden read: {path}'
        opened.append(path)
        return original(path, **kwargs)
    monkeypatch.setattr(src, '_safe_open', guarded)
    observed = composer.manifest_provenance(fleet, paths)['sources']
    root = next(r for r in observed['roots'] if r['path'] == str(library))
    assert observed['complete'] is False and root['complete'] is False
    assert len(root['issues']) == 6
    assert any('outside-or-excluded' in issue for issue in root['issues'])
    assert any('cyclic' in issue for issue in root['issues'])
    assert library / 'source.txt' in opened
    assert 'FORBIDDEN_SECRET' not in json.dumps(observed)
    # Same target bytes, distinct link identity, is still a distinct observation.
    (library / 'safe').unlink();(library / 'safe').symlink_to('source.txt')
    changed = composer.manifest_provenance(fleet, paths)['sources']
    assert root['sha256'] != next(r['sha256'] for r in changed['roots'] if r['path'] == str(library))


def test_missing_unreadable_and_git_failures_are_unknown(source_scene, monkeypatch):
    from claudlobby import source_provenance as src
    fleet, paths, package = source_scene
    (package / 'voices/source.txt').unlink();(package / 'voices').rmdir()
    original = src._safe_open
    def fail(path, **kwargs):
        if path == package / 'library/source.txt':raise PermissionError('synthetic')
        return original(path, **kwargs)
    monkeypatch.setattr(src, '_safe_open', fail)
    monkeypatch.setattr(src, '_git', lambda *args: (None, 'unavailable'))
    prov = composer.manifest_provenance(fleet, paths)
    assert prov['sources']['complete'] is False
    assert any('coverage incomplete' in s for s in composer.manifest_warnings(prov))
    assert any('git state unavailable' in s for s in composer.manifest_warnings(prov))


def test_detached_and_scoped_dirty_are_recorded(source_scene):
    fleet, paths, package = source_scene
    (package / 'unrelated.txt').write_text('outside source inventory')
    git(package, 'checkout', '-q', '--detach')
    prov = composer.manifest_provenance(fleet, paths)
    repo = next(r for r in prov['sources']['repositories'] if r['path'] == str(package))
    assert repo['detached'] and repo['dirty'] is False
    assert len(repo['commit']) == 40
    assert any('detached' in s for s in composer.manifest_warnings(prov))
    (package / '.git/rebase-merge').mkdir()
    assert any('interrupted' in s for s in composer.manifest_warnings(composer.manifest_provenance(fleet, paths)))


def test_legacy_and_corrupt_snapshots_are_not_clean(source_scene):
    from claudlobby.diff import manifest_header
    from claudlobby.doctor import DoctorReport, check_manifest_provenance
    fleet, paths, _ = source_scene
    prov = composer.write_manifest_provenance(fleet, paths)
    prov['schema'] = 1;prov.pop('sources')
    sidecar = paths.runtime / 'composed.json';sidecar.write_text(json.dumps(prov))
    assert composer.read_manifest_provenance(paths)['files'] == prov['files']
    assert 'core source provenance unavailable' in manifest_header(fleet, paths)
    report = DoctorReport();check_manifest_provenance(fleet, paths, report)
    assert report.checks[-1].status == 'warn'
    for value in ([], {'schema': 2, 'files': [], 'git': {}}, {'schema': 2, 'files': {}, 'git': {}, 'sources': {}}):
        sidecar.write_text(json.dumps(value))
        assert composer.read_manifest_provenance(paths) is None
        assert 'NO PROVENANCE' in manifest_header(fleet, paths)


def test_schema_two_receipt_keeps_sources_and_old_receipts_remain_valid(source_scene, tmp_path):
    from claudlobby.plane.contracts import validate_request, ContractViolation
    from claudlobby.plane.emit_api import emit_batch
    from claudlobby.plane.db import connect_ro, db_file
    from claudlobby.plane.composition_history import read_compositions
    from tests.test_composition_history import completion, observation
    fleet, paths, _ = source_scene
    prov = composer.manifest_provenance(fleet, paths);prov['bot_ids'] = ['worker']
    old = completion('old', observation(fleet.name), fleet=fleet.name)
    new = completion('new', prov, fleet=fleet.name)
    validate_request(old);validate_request(new)
    root = tmp_path / 'receipt-root'
    assert [r.status for r in emit_batch(root, [old, new])] == ['committed', 'committed']
    with connect_ro(db_file(root)) as conn:
        rows = read_compositions(conn, fleet.name)['observations']
    assert rows[0]['composition']['schema'] == 2
    assert rows[0]['composition']['sources']['package_root'] == prov['sources']['package_root']
    assert rows[1]['composition']['schema'] == 1
    del new['payload']['composition']['sources']
    with pytest.raises(ContractViolation, match='requires core source'):
        validate_request(new)


def test_no_git_installation_keeps_revision_explicitly_unavailable(source_scene, monkeypatch):
    from claudlobby import source_provenance as src
    fleet, paths, _ = source_scene
    monkeypatch.setattr(src, '_git', lambda *args: (None, 'no_git'))
    sources = composer.manifest_provenance(fleet, paths)['sources']
    assert sources['complete'] is True
    assert all(r['state'] == 'no_git' and r.get('commit') is None for r in sources['repositories'])
    assert all(len(r['sha256']) == 64 for r in sources['roots'])


def test_shared_file_is_hashed_once_and_overlay_templates_are_observed(source_scene, monkeypatch):
    from claudlobby import source_provenance as src
    fleet, paths, package = source_scene
    (paths.fleet_dir / 'templates').mkdir()
    (paths.fleet_dir / 'templates/claude.md.j2').write_text('private overlay')
    original = src._safe_open
    opens = []
    def record(path, **kwargs):
        if not kwargs.get('directory'):opens.append(path)
        return original(path, **kwargs)
    monkeypatch.setattr(src, '_safe_open', record)
    prov = composer.manifest_provenance(fleet, paths)
    assert opens.count(package / 'claudlobby/system.yaml') == 1
    assert any('overlay/templates' in r['labels'] for r in prov['sources']['roots'])


def test_declared_symlink_root_does_not_authorize_its_external_target(source_scene, tmp_path, monkeypatch):
    from claudlobby import source_provenance as src
    fleet, paths, package = source_scene
    outside = tmp_path / 'external-sources';outside.mkdir()
    (outside / 'secret').write_text('FORBIDDEN_ROOT_BODY')
    (package / 'voices/source.txt').unlink();(package / 'voices').rmdir()
    (package / 'voices').symlink_to(outside, target_is_directory=True)
    original = src._safe_open
    def guarded(path, **kwargs):
        assert outside not in (path, *path.parents)
        return original(path, **kwargs)
    monkeypatch.setattr(src, '_safe_open', guarded)
    sources = composer.manifest_provenance(fleet, paths)['sources']
    voice = next(r for r in sources['roots'] if 'base/voices' in r['labels'])
    assert not voice['complete']
    assert 'outside-or-excluded' in voice['issues'][0]
    assert 'FORBIDDEN_ROOT_BODY' not in json.dumps(sources)


def test_system_default_symlink_cannot_bypass_inventory_via_manifest_hash(source_scene, tmp_path, monkeypatch):
    import builtins
    from claudlobby.diff import manifest_header
    fleet, paths, package = source_scene
    outside = tmp_path / '.env';outside.write_text('FORBIDDEN_CREDENTIAL')
    system = package / 'claudlobby/system.yaml'
    system.unlink();system.symlink_to(outside)
    original = builtins.open
    def guarded(file, *args, **kwargs):
        if isinstance(file, (str, Path)):
            assert Path(file).resolve() != outside, 'manifest hash bypassed source boundary'
        return original(file, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', guarded)
    prov = composer.write_manifest_provenance(fleet, paths)
    assert prov['files']['system.yaml']['sha256'] is None
    assert prov['sources']['complete'] is False
    assert 'INCOMPLETE' in manifest_header(fleet, paths)
    assert composer.changed_manifest_inputs(fleet, paths) == []
    assert 'FORBIDDEN_CREDENTIAL' not in json.dumps(prov)


def test_failed_branch_read_is_unknown_not_detached(source_scene, monkeypatch):
    from claudlobby import source_provenance as src
    fleet, paths, package = source_scene
    original = src._git
    def fail(args, cwd):
        if args == ['symbolic-ref', '--quiet', '--short', 'HEAD']:
            return None, 'unavailable'
        return original(args, cwd)
    monkeypatch.setattr(src, '_git', fail)
    prov = composer.manifest_provenance(fleet, paths)
    repo = next(r for r in prov['sources']['repositories'] if r['path'] == str(package))
    assert repo['state'] == 'unavailable' and repo['detached'] is None
    assert any('git state unavailable' in s for s in composer.manifest_warnings(prov))
