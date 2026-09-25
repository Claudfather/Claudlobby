"""Compare link topology through the real preview without touching live targets."""
from pathlib import Path
import os

import pytest

from claudlobby.composer import compose_bot, link_skills, link_mounts
from claudlobby.config import load_fleet
from claudlobby.diff import diff_bot
from claudlobby.paths import Paths


def snapshot(root):
    out = {}
    for p in [root, *sorted(root.rglob('*'))]:
        s = p.lstat()
        out[str(p)] = (s.st_mode, s.st_mtime_ns, s.st_size,
                      os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
    return out


@pytest.fixture
def scene(fleet_dir, tmp_path, monkeypatch):
    home = tmp_path / 'home'; home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    paths = Paths(root=fleet_dir)
    fleet, _ = load_fleet(paths.fleet_yaml)
    bot = fleet.bots['lead']
    bot.skills = ['one']
    skill = fleet_dir / 'library/skills/one'; skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: one\n---\nOriginal body.\n')
    target = home / 'mounted'; target.mkdir()
    (target / 'PRIVATE').write_text('DO_NOT_READ_TARGET')
    bot.mounts = {'docs': str(target)}
    compose_bot(bot, fleet, paths)
    return bot, fleet, paths, home, target


def preview(scene):
    bot, fleet, paths, _, _ = scene
    return diff_bot(bot.bot_id, fleet, paths)


def skill_link(scene):
    return scene[2].bot_runtime('lead') / '.claude/skills/one'


def mount_link(scene):
    return scene[2].bot_runtime('lead') / 'mounts/docs'


def test_clean_link_preview_discloses_coverage_and_live_recreation(scene):
    text = preview(scene)
    assert 'no drift in lead' in text
    assert 'skills: expected 1, matching 1' in text
    assert '1 skill links recreated on generate' in text
    assert 'next use, no restart gate' in text
    assert 'mounts: expected 1, matching 1' in text
    assert 'target content not compared' in text
    assert 'not compared: skill/command/agent symlinks' not in text


@pytest.mark.parametrize('kind', ['skill', 'mount'])
@pytest.mark.parametrize('change', ['missing', 'retargeted', 'directory', 'file'])
def test_public_diff_detects_link_changes_and_obstacles(scene, kind, change):
    link = skill_link(scene) if kind == 'skill' else mount_link(scene)
    link.unlink()
    if change == 'retargeted': link.symlink_to(scene[3] / 'absent')
    elif change == 'directory': link.mkdir()
    elif change == 'file': link.write_text('local obstacle')
    text = preview(scene)
    assert f'{kind} topology' in text and link.name in text
    assert {'missing': 'create', 'retargeted': 'retarget', 'directory': 'directory', 'file': 'obstacle'}[change] in text
    assert 'no drift in lead' not in text
    if kind == 'mount' and change in ('directory', 'file'):
        assert 'preserved' in text


def test_extra_links_and_skill_directories_are_removals_but_files_are_preserved(scene):
    skills = skill_link(scene).parent; mounts = mount_link(scene).parent
    (skills / 'stale').symlink_to(scene[3] / 'absent')
    (skills / 'local-copy').mkdir()
    (skills / 'keep-file').write_text('retained')
    (mounts / 'stale').symlink_to(scene[3] / 'absent')
    (mounts / 'keep-dir').mkdir()
    text = preview(scene)
    assert 'remove skill link stale' in text
    assert 'remove skill directory local-copy' in text
    assert 'remove mount link stale' in text
    assert 'preserved extra skill entry keep-file' in text
    assert 'preserved extra mount entry keep-dir' in text


def test_plugin_reference_and_empty_local_set_are_not_phantom_missing_links(scene):
    bot, fleet, paths, _, _ = scene
    bot.skills = ['plugin:provided']; bot.mounts = {}
    compose_bot(bot, fleet, paths)
    text = preview(scene)
    assert 'skills: expected 0, matching 0' in text
    assert 'no local link expected' in text and 'plugin availability not assessed' in text
    assert 'no drift in lead' in text


def test_diff_never_calls_link_writers_or_reads_mount_contents(scene, monkeypatch):
    import claudlobby.composer as composer
    before = snapshot(scene[2].root), snapshot(scene[3])
    def forbidden(*_a, **_k): raise AssertionError('writer called by diff')
    monkeypatch.setattr(composer, 'link_skills', forbidden)
    monkeypatch.setattr(composer, 'link_mounts', forbidden)
    original = Path.open
    target = scene[4]
    def safe_open(self, *a, **kw):
        assert not self.is_relative_to(target), 'target content opened'
        return original(self, *a, **kw)
    monkeypatch.setattr(Path, 'open', safe_open)
    text = preview(scene)
    assert 'skills: expected 1' in text
    monkeypatch.setattr(Path, 'open', original)
    assert before == (snapshot(scene[2].root), snapshot(scene[3]))


def test_same_target_content_change_is_not_fabricated_as_topology_drift(scene):
    skill = scene[2].root / 'library/skills/one/SKILL.md'
    skill.write_text('---\nname: one\n---\nChanged body already live.\n')
    (scene[4] / 'PRIVATE').write_text('changed mounted content')
    text = preview(scene)
    assert 'no drift in lead' in text
    assert 'target content not compared' in text


def test_regular_skill_obstacle_still_blocks_writer_and_mount_obstacle_survives(scene):
    bot, _, paths, _, _ = scene
    skill = skill_link(scene); skill.unlink(); skill.write_text('local')
    with pytest.raises(FileExistsError): link_skills(bot, paths, lambda _: None, skills=['one'])
    mount = mount_link(scene); mount.unlink(); mount.write_text('local mount')
    notes = []
    link_mounts(bot, paths.bot_runtime('lead'), notes.append)
    assert mount.read_text() == 'local mount' and any('non-symlink' in n for n in notes)


def test_unsafe_mount_and_dangling_safe_mount_are_disclosed(scene):
    bot, _, paths, home, _ = scene
    bot.mounts = {'unsafe': '/outside-home/audit-target', 'future': str(home / 'future')}
    # Actual behavior preserves the skipped name if it already exists.
    (mount_link(scene).parent / 'unsafe').symlink_to(home / 'prior')
    text = preview(scene)
    assert "mount 'unsafe'" in text and 'skipping' in text
    assert 'create mount link future' in text and 'dangling' in text


def test_link_namespace_symlink_is_explicitly_uninspected(scene):
    bot, _, paths, home, _ = scene
    link = skill_link(scene); link.unlink(); link.parent.rmdir()
    target = home / 'external-skills'; target.mkdir()
    (target / 'PRIVATE').write_text('not a skill')
    link.parent.symlink_to(target)
    text = preview(scene)
    assert 'skill topology unavailable: namespace is a symlink' in text
    assert 'no drift in lead' not in text


def test_overlay_folder_expansion_and_first_leaf_win_match_writer(scene):
    bot, fleet, paths, _, _ = scene
    overlay = paths.root / 'local/demo'
    for root, rel in [(paths.root, 'pack/a'), (paths.root, 'pack/b'),
                      (overlay, 'pack/a'), (paths.root, 'other/a')]:
        p = root / 'library/skills' / rel; p.mkdir(parents=True)
        (p / 'SKILL.md').write_text('---\nname: fixture\n---\nBody.\n')
    bot.skills = ['pack/', 'other/a']
    paths = Paths(root=paths.root, fleet_dir=overlay)
    notes = []
    compose_bot(bot, fleet, paths, log=notes.append, boot_delay_s=0)
    directory = paths.bot_runtime('lead') / '.claude/skills'
    assert os.readlink(directory / 'a') == str((overlay / 'library/skills/pack/a').resolve())
    assert os.readlink(directory / 'b') == str((paths.root / 'library/skills/pack/b').resolve())
    assert any("skill 'a' already linked" in n for n in notes)
    text = diff_bot('lead', fleet, paths)
    assert 'skills: expected 2, matching 2' in text and 'no drift in lead' in text
    (directory / 'a').unlink()
    assert 'create skill link a' in diff_bot('lead', fleet, paths)


@pytest.mark.parametrize('defaulted', [False, True])
def test_protocol_required_and_manager_default_skills_use_effective_authority(scene, defaulted):
    bot, fleet, paths, _, _ = scene
    name = 'checkin' if defaulted else 'require-one'
    protocol = paths.root / f'library/protocols/{name}.md'
    protocol.write_text('---\ntitle: Link requirement\nrequires:\n  skills: [one]\n---\nProtocol.\n')
    bot.protocols = [] if defaulted else [name]
    bot.skills = []
    if defaulted: assert bot.bot_id in fleet.leaf_manager_bots()
    compose_bot(bot, fleet, paths)
    assert skill_link(scene).is_symlink()
    text = preview(scene)
    assert 'skills: expected 1, matching 1' in text and 'no drift in lead' in text
    skill_link(scene).unlink()
    assert 'create skill link one' in preview(scene)


def test_topology_helper_opens_no_file_contents(scene, monkeypatch):
    from claudlobby.link_diff import link_preview
    def forbidden(*_a, **_k): raise AssertionError('topology opened content')
    monkeypatch.setattr(Path, 'open', forbidden)
    changes, notes = link_preview(scene[0], scene[2], skills=['one'])
    assert changes == [] and any('matching 1' in n for n in notes)


def test_unreadable_namespace_is_unknown_not_empty(scene, monkeypatch):
    directory = skill_link(scene).parent
    original = Path.iterdir
    def denied(self):
        if self == directory: raise PermissionError('do not print this value')
        return original(self)
    monkeypatch.setattr(Path, 'iterdir', denied)
    text = preview(scene)
    assert 'skill topology unavailable (PermissionError)' in text
    assert 'do not print this value' not in text and 'no drift in lead' not in text


def test_relative_equivalent_link_is_matching_and_mount_writer_preserves_it(scene):
    skill = skill_link(scene); target = os.readlink(skill)
    skill.unlink(); skill.symlink_to(os.path.relpath(target, skill.parent))
    mount = mount_link(scene); mount.unlink()
    mount.symlink_to(os.path.relpath(scene[4], mount.parent))
    before = mount.lstat().st_mtime_ns, os.readlink(mount)
    text = preview(scene)
    assert 'skills: expected 1, matching 1' in text
    assert 'mounts: expected 1, matching 1' in text
    link_mounts(scene[0], scene[2].bot_runtime('lead'), lambda _: None)
    assert before == (mount.lstat().st_mtime_ns, os.readlink(mount))


def test_discovery_remains_lazy_after_skill_cleanup(scene):
    """Pin existing partial-write order: invalid source refuses after cleanup."""
    with pytest.raises(ValueError, match='path traversal'):
        link_skills(scene[0], scene[2], lambda _: None, skills=['../bad'])
    assert not skill_link(scene).exists()


@pytest.mark.parametrize('segment', ['runtime', 'bots', 'lead', '.claude', 'skills'])
def test_namespace_ancestors_are_guarded_before_traversal(scene, monkeypatch, segment):
    from claudlobby.link_diff import link_preview
    bot, _, paths, home, _ = scene
    namespaces = [paths.runtime, paths.runtime_bots, paths.bot_runtime('lead'),
                  skill_link(scene).parent.parent, skill_link(scene).parent]
    original = next(path for path in namespaces if path.name == segment)
    outside = home / f'external-{segment}'
    original.rename(outside)
    original.symlink_to(outside, target_is_directory=True)
    iterdir = Path.iterdir
    def guarded(self):
        assert not self.is_relative_to(original), 'traversed linked namespace'
        return iterdir(self)
    monkeypatch.setattr(Path, 'iterdir', guarded)
    changes, _ = link_preview(bot, paths, skills=['one'])
    assert any('skill topology unavailable' in change for change in changes)


def test_namespace_metadata_denial_is_unavailable(scene, monkeypatch):
    from claudlobby.link_diff import link_preview
    directory = skill_link(scene).parent
    original = Path.lstat
    def denied(self, *args, **kwargs):
        if self == directory:
            raise PermissionError('PRIVATE_METADATA_VALUE')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', denied)
    changes, _ = link_preview(scene[0], scene[2], skills=['one'])
    assert 'skill topology unavailable (PermissionError)' in changes
    assert 'PRIVATE_METADATA_VALUE' not in str(changes)


def test_explicit_root_symlink_alias_remains_supported(scene):
    from claudlobby.link_diff import link_preview
    bot, _, paths, home, _ = scene
    alias = home / 'root-alias'
    alias.symlink_to(paths.root, target_is_directory=True)
    changes, notes = link_preview(bot, Paths(root=alias), skills=['one'])
    assert changes == []
    assert 'skills: expected 1, matching 1' in notes


def test_nested_mount_names_keep_full_identity_through_public_diff(scene):
    import yaml
    bot, fleet, paths, home, _ = scene
    directory = mount_link(scene).parent
    for name in ('first', 'second'):
        (directory / name).mkdir()
        (home / name).mkdir()
    bot.mounts = {f'{name}/doc': str(home / name) for name in ('first', 'second')}
    raw = yaml.safe_load(paths.fleet_yaml.read_text())
    raw['fleet']['bots']['lead']['mounts'] = bot.mounts
    paths.fleet_yaml.write_text(yaml.safe_dump(raw))
    assert load_fleet(paths.fleet_yaml)[0].bots['lead'].mounts == bot.mounts
    compose_bot(bot, fleet, paths)
    (directory / 'first/doc').unlink()
    text = preview(scene)
    assert 'create mount link first/doc' in text
    assert 'mounts: expected 2, matching 1' in text
    link_mounts(bot, paths.bot_runtime('lead'), lambda _: None)
    assert (directory / 'first/doc').is_symlink()


def test_selected_mount_ancestor_is_guarded_before_plan_reads(scene, monkeypatch):
    from claudlobby.link_diff import link_preview
    bot, _, paths, home, target = scene
    outside = home / 'outside-mounts'; outside.mkdir()
    (outside / 'doc').symlink_to(target)
    intermediate = mount_link(scene).parent / 'first'
    intermediate.symlink_to(outside)
    bot.mounts = {'first/doc': str(target)}
    original = Path.lstat
    def guarded(self, *args, **kwargs):
        assert self != intermediate / 'doc', 'read through selected path ancestor'
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', guarded)
    changes, _ = link_preview(bot, paths, skills=['one'])
    assert any('mount topology unavailable' in change for change in changes)


@pytest.mark.parametrize('via_runtime_link', [False, True])
def test_cleanup_dependent_skill_selection_is_explicitly_unavailable(scene, via_runtime_link):
    from claudlobby.link_diff import link_preview
    bot, _, paths, home, _ = scene
    source = paths.root / 'library/skills/one'
    (source / 'SKILL.md').unlink(); source.rmdir()
    link = skill_link(scene); link.unlink()
    if via_runtime_link:
        target = home / 'external-source'; target.mkdir()
        (target / 'SKILL.md').write_text('body')
        link.symlink_to(target)
    else:
        link.mkdir(); (link / 'SKILL.md').write_text('body')
    source.symlink_to(link)
    before = snapshot(paths.root), snapshot(home)
    changes, _ = link_preview(bot, paths, skills=['one'])
    assert any('skill topology unavailable' in change and 'cleanup' in change for change in changes)
    assert before == (snapshot(paths.root), snapshot(home))
    logs = []
    link_skills(bot, paths, logs.append, skills=['one'])
    assert not link.is_symlink() and any("skill 'one' missing" in line for line in logs)


def test_external_skill_source_symlink_remains_supported(scene):
    from claudlobby.link_diff import link_preview
    bot, _, paths, home, _ = scene
    source = paths.root / 'library/skills/one'
    (source / 'SKILL.md').unlink(); source.rmdir()
    target = home / 'external-source'; target.mkdir()
    (target / 'SKILL.md').write_text('body')
    source.symlink_to(target)
    link_skills(bot, paths, lambda _: None, skills=['one'])
    changes, notes = link_preview(bot, paths, skills=['one'])
    assert changes == [] and 'skills: expected 1, matching 1' in notes


def test_absent_nested_mount_parent_is_unavailable_and_writer_still_refuses(scene):
    from claudlobby.link_diff import link_preview
    bot, _, paths, _, target = scene
    bot.mounts = {'absent/doc': str(target)}
    changes, _ = link_preview(bot, paths, skills=['one'])
    assert 'mount topology unavailable: selected path parent is absent' in changes
    with pytest.raises(FileNotFoundError):
        link_mounts(bot, paths.bot_runtime('lead'), lambda _: None)


def test_matching_dangling_mount_has_note_and_remains_unchanged(scene):
    target = scene[4]
    (target / 'PRIVATE').unlink(); target.rmdir()
    before = os.readlink(mount_link(scene)), mount_link(scene).lstat().st_mtime_ns
    text = preview(scene)
    assert 'matching mount link docs is dangling' in text
    assert 'no drift in lead' in text
    link_mounts(scene[0], scene[2].bot_runtime('lead'), lambda _: None)
    assert before == (os.readlink(mount_link(scene)), mount_link(scene).lstat().st_mtime_ns)


@pytest.mark.parametrize('folder', [False, True])
def test_missing_skill_source_can_appear_after_an_earlier_link(scene, folder):
    from claudlobby.link_diff import link_preview
    bot, _, paths, _, _ = scene
    first = skill_link(scene); first.unlink()
    if folder:
        pack = paths.root / 'library/skills/pack'; pack.mkdir()
        source = pack / 'two'
        skills = ['one', 'pack/']
    else:
        source = paths.root / 'library/skills/two'
        skills = ['one', 'two']
    source.symlink_to(first)
    before = snapshot(paths.root)
    changes, _ = link_preview(bot, paths, skills=skills)
    assert any('skill topology unavailable' in change for change in changes)
    assert before == snapshot(paths.root)
    link_skills(bot, paths, lambda _: None, skills=skills)
    assert first.is_symlink()
    assert (first.parent / 'two').is_symlink()


@pytest.mark.parametrize('via_alias', [False, True])
def test_mount_target_resolution_can_change_after_stale_cleanup(scene, tmp_path, via_alias):
    from claudlobby.link_diff import link_preview
    bot, _, paths, home, _ = scene
    stale = mount_link(scene).parent / 'stale'
    stale.symlink_to(tmp_path / 'outside-home')
    target = stale / 'child'
    if via_alias:
        alias = home / 'target-alias'
        alias.symlink_to(stale)
        target = alias / 'child'
    bot.mounts = {'docs': str(target)}
    before = snapshot(paths.root), snapshot(home)
    changes, _ = link_preview(bot, paths, skills=['one'])
    assert any('mount topology unavailable' in change for change in changes)
    assert not any('escapes home' in change for change in changes)
    assert before == (snapshot(paths.root), snapshot(home))
    link_mounts(bot, paths.bot_runtime('lead'), lambda _: None)
    assert os.readlink(mount_link(scene)) == str(target)
    assert not mount_link(scene).exists()


def test_relative_mount_declaration_is_unavailable_without_changing_writer(scene, monkeypatch):
    from claudlobby.link_diff import link_preview
    bot, _, paths, home, _ = scene
    monkeypatch.chdir(home)
    bot.mounts = {'docs': 'mounted'}
    # The existing writer preserves an absolute link resolving under cwd.
    before = os.readlink(mount_link(scene)), mount_link(scene).lstat().st_mtime_ns
    changes, _ = link_preview(bot, paths, skills=['one'])
    assert any('mount topology unavailable' in change and 'relative' in change for change in changes)
    link_mounts(bot, paths.bot_runtime('lead'), lambda _: None)
    assert before == (os.readlink(mount_link(scene)), mount_link(scene).lstat().st_mtime_ns)
    # Creation stores the raw relative value, which resolves under the link parent.
    mount_link(scene).unlink()
    link_mounts(bot, paths.bot_runtime('lead'), lambda _: None)
    assert os.readlink(mount_link(scene)) == 'mounted'
    assert not mount_link(scene).exists()
    changes, _ = link_preview(bot, paths, skills=['one'])
    assert any('mount topology unavailable' in change and 'relative' in change for change in changes)
    assert not any('retarget' in change for change in changes)
    link_mounts(bot, paths.bot_runtime('lead'), lambda _: None)
    assert os.readlink(mount_link(scene)) == 'mounted'


def test_absolute_mount_declaration_and_home_expansion_remain_supported(scene):
    from claudlobby.link_diff import link_preview
    scene[0].mounts = {'docs': '~/mounted'}
    changes, notes = link_preview(scene[0], scene[2], skills=['one'])
    assert changes == [] and 'mounts: expected 1, matching 1' in notes
