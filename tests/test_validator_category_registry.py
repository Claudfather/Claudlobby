"""Shared markdown registration reaches validation without another category list."""
from copy import deepcopy

import pytest

from claudlobby import composer, validator
from claudlobby.commands import core
from claudlobby.known_values import MARKDOWN_BOT_REFERENCE_FIELDS
from types import SimpleNamespace
from tests.test_library_category_contract import scene, _put


@pytest.mark.parametrize('kind', MARKDOWN_BOT_REFERENCE_FIELDS)
@pytest.mark.parametrize('ref', ['missing', 'nested/missing', 'folder/'])
def test_every_markdown_category_warns_once_and_fails_strict(scene, monkeypatch, kind, ref):
    bot, fleet, paths = scene
    setattr(bot, kind, [ref])
    report = validator.validate(fleet, paths)
    assert report.errors == []
    assert len(report.warnings) == 1
    suffix = 'skipped' if kind == 'integrations' else (
        'no items will be loaded' if ref.endswith('/') else 'section will be skipped')
    detail = (f"folder '{ref}' empty or missing" if ref.endswith('/')
              else f"'{ref}' not in any")
    location = f" in any library/{kind}/" if ref.endswith('/') else f" library/{kind}/"
    assert report.warnings == [f"bot 'sample': {kind[:-1]} {detail}{location} — {suffix}"]
    monkeypatch.setattr(core, '_resolve_paths', lambda _args: paths)
    monkeypatch.setattr(core, '_load_env', lambda _paths: None)
    monkeypatch.setattr(core, '_load_fleet_or_exit', lambda _paths: (fleet, {}))
    assert core.cmd_validate(SimpleNamespace(strict=False)) == 0
    assert core.cmd_validate(SimpleNamespace(strict=True)) == 1


@pytest.mark.parametrize('kind', MARKDOWN_BOT_REFERENCE_FIELDS)
def test_every_category_resolves_overlay_nested_and_folder_refs(scene, kind):
    bot, fleet, original = scene
    paths = type(original)(root=original.root, fleet_dir=original.root / 'local/fixture')
    _put(paths, kind, 'nested/single', 'FIRST', overlay=True)
    _put(paths, kind, 'bundle/a', 'BASE')
    _put(paths, kind, 'bundle/a', 'OVERLAY {{BOT_NAME}}', overlay=True)
    _put(paths, kind, 'bundle/z', 'LAST', overlay=True)
    _put(paths, kind, 'bundle/README', 'NOT COMPOSED', overlay=True)
    setattr(bot, kind, ['nested/single', 'bundle/', 'bundle/a'])
    (paths.root / 'templates/claude.md.j2').write_text(
        '{% for item in ' + kind + ' %}{{ item.body }}|{% endfor %}')
    assert validator.validate(fleet, paths).warnings == []
    assert composer.compose_claude_md(bot, fleet, paths) == 'FIRST|OVERLAY Sample|LAST|'


@pytest.mark.parametrize('kind', MARKDOWN_BOT_REFERENCE_FIELDS)
@pytest.mark.parametrize('readme_only', [False, True])
def test_empty_folders_warn_for_every_registered_category(scene, kind, readme_only):
    bot, fleet, paths = scene
    (paths.base_library / kind / 'empty').mkdir(parents=True)
    if readme_only:
        _put(paths, kind, 'empty/README')
    setattr(bot, kind, ['empty/'])
    warnings = validator.validate(fleet, paths).warnings
    assert len(warnings) == 1 and "folder 'empty/' empty or missing" in warnings[0]


def test_legacy_order_is_stable_and_new_policy_warnings_belong_to_each_bot(scene):
    bot, fleet, paths = scene
    for kind in MARKDOWN_BOT_REFERENCE_FIELDS:
        setattr(bot, kind, ['missing'])
    second = deepcopy(bot)
    second.bot_id = 'second'
    second.name = 'Second'
    fleet.bots['second'] = second
    expected_order = ['integrations', 'guardrails', 'protocols', 'resources',
                      'lessons', 'post_actions', 'principles', 'permissions']
    report = validator.validate(fleet, paths)
    assert report.errors == []
    # Fleet-wide policy is unrelated to per-bot library diagnostic order.
    warnings = [warning for warning in report.warnings if 'library/' in warning]
    assert len(warnings) == 16
    for bot_name, group in [('sample', warnings[:8]), ('second', warnings[8:])]:
        assert all(w.startswith(f"bot '{bot_name}':") for w in group)
        assert [w.split('library/')[1].split('/')[0] for w in group] == expected_order


@pytest.mark.parametrize('ref', ['missing', 'nested/missing', 'folder/'])
def test_registered_extension_is_not_silently_omitted_by_validator(scene, monkeypatch, ref):
    """Registry extension control, not a newly discovered current-category defect."""
    bot, fleet, paths = scene
    fields = (*MARKDOWN_BOT_REFERENCE_FIELDS, 'reviews')
    monkeypatch.setattr(validator, 'MARKDOWN_BOT_REFERENCE_FIELDS', fields)
    monkeypatch.setattr(composer, 'MARKDOWN_BOT_REFERENCE_FIELDS', fields)
    bot.reviews = [ref]
    report = validator.validate(fleet, paths)
    assert report.errors == []
    assert len(report.warnings) == 1
    assert f"review {'folder ' if ref.endswith('/') else ''}'{ref}'" in report.warnings[0]
    assert 'library/reviews/' in report.warnings[0]
    _put(paths, 'reviews', 'nested/present', 'EXTENSION')
    bot.reviews = ['nested/']
    (paths.root / 'templates/claude.md.j2').write_text(
        '{% for item in reviews %}{{ item.body }}{% endfor %}')
    assert validator.validate(fleet, paths).warnings == []
    assert composer.compose_claude_md(bot, fleet, paths) == 'EXTENSION'
