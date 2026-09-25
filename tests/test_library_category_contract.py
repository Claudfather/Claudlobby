"""Category metadata reaches composition/listing and missing-reference checks.

The existing validator loops remain separately owned by the phase08 refactor;
these tests pin the new principles/permissions preflight without duplicating
legacy warnings or widening required-expertise/skill/requires semantics.
"""
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
import logging

import pytest

from claudlobby.commands import core
from claudlobby.composer import compose_claude_md, compose_settings_local
from claudlobby.config import BotConfig, FleetConfig, SystemDefaultsConfig
from claudlobby.paths import Paths
from claudlobby.validator import validate

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    (root / 'library/expertise').mkdir(parents=True)
    (root / 'library/expertise/demo.md').write_text('# Demo\n\nSpecialist.\n')
    (root / 'templates').mkdir()
    (root / 'templates/claude.md.j2').write_text('{{ expertise_body }}\n')
    home = tmp_path / 'home'; home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    bot = BotConfig(bot_id='sample', name='Sample', expertise=['demo'])
    fleet = FleetConfig(name='fixture', service_prefix='com.test', bots={'sample': bot},
                        system_defaults=SystemDefaultsConfig(enabled=False))
    paths = Paths(root=root)
    baseline = validate(fleet, paths)
    assert baseline.errors == [] and baseline.warnings == []
    return bot, fleet, paths


def _put(paths, kind, name, body='body', *, overlay=False):
    root = paths.overlay_library if overlay else paths.base_library
    target = root / kind / f'{name}.md'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f'---\ntitle: {name}\n---\n\n{body}\n')
    return target


@pytest.mark.parametrize('kind', ['principles', 'permissions'])
@pytest.mark.parametrize('ref', ['missing', 'nested/missing', 'folder/'])
def test_missing_markdown_reference_warns_and_strict_validation_fails(scene, monkeypatch, kind, ref):
    bot, fleet, paths = scene
    monkeypatch.setattr(core, '_resolve_paths', lambda _args: paths)
    monkeypatch.setattr(core, '_load_env', lambda _paths: None)
    monkeypatch.setattr(core, '_load_fleet_or_exit', lambda _paths: (fleet, {}))
    assert core.cmd_validate(SimpleNamespace(strict=True)) == 0
    setattr(bot, kind, [ref])
    report = validate(fleet, paths)
    assert report.errors == [] and len(report.warnings) == 1
    warning = report.warnings[0]
    assert "bot 'sample'" in warning and f'library/{kind}/' in warning and f"'{ref}'" in warning
    assert ('folder' in warning and 'empty or missing' in warning) if ref.endswith('/') else 'section will be skipped' in warning
    assert core.cmd_validate(SimpleNamespace(strict=False)) == 0
    assert core.cmd_validate(SimpleNamespace(strict=True)) == 1


@pytest.mark.parametrize('kind', ['principles', 'permissions'])
@pytest.mark.parametrize('readme_only', [False, True])
def test_empty_or_readme_only_folder_is_not_a_resolved_reference(scene, kind, readme_only):
    bot, fleet, paths = scene
    (paths.base_library / kind / 'empty').mkdir(parents=True)
    if readme_only:
        _put(paths, kind, 'empty/README', 'Unreferenced documentation')
    setattr(bot, kind, ['empty/'])
    assert any("folder 'empty/' empty or missing" in w for w in validate(fleet, paths).warnings)


@pytest.mark.parametrize('kind', ['principles', 'permissions'])
def test_overlay_only_nested_and_folder_references_resolve_in_order(scene, kind):
    bot, fleet, original = scene
    overlay = original.root / 'local/f'
    paths = Paths(root=original.root, fleet_dir=overlay)
    _put(paths, kind, 'bundle/a', 'BASE')
    _put(paths, kind, 'bundle/a', 'OVERLAY {{BOT_NAME}}', overlay=True)
    _put(paths, kind, 'bundle/z', 'LAST', overlay=True)
    _put(paths, kind, 'single/nested', 'FIRST', overlay=True)
    _put(paths, kind, 'bundle/README', 'DO NOT COMPOSE', overlay=True)
    (original.root / 'templates/claude.md.j2').write_text(
        '{% for item in ' + kind + ' %}{{ item.body }}|{% endfor %}')
    setattr(bot, kind, ['single/nested', 'bundle/', 'bundle/a'])
    assert validate(fleet, paths).warnings == []
    assert compose_claude_md(bot, fleet, paths) == 'FIRST|OVERLAY Sample|LAST|'


def test_unreferenced_markdown_and_readme_are_not_bot_reference_errors(scene):
    _bot, fleet, paths = scene
    for kind in ('principles', 'permissions'):
        _put(paths, kind, 'README', 'Category docs')
        _put(paths, kind, 'not-equipped', 'Unreferenced body')
    assert validate(fleet, paths).warnings == []


def test_category_registry_is_immutable_and_describes_actual_library_storage():
    from claudlobby.known_values import LIBRARY_CATEGORIES, MARKDOWN_BOT_REFERENCE_FIELDS
    categories = {c.name: c for c in LIBRARY_CATEGORIES}
    assert len(categories) == len(LIBRARY_CATEGORIES)
    assert set(categories) == {p.name for p in (REPO / 'library').iterdir() if p.is_dir()}
    assert set(MARKDOWN_BOT_REFERENCE_FIELDS) == {'resources', 'integrations', 'principles', 'permissions', 'protocols', 'guardrails', 'lessons', 'post_actions'}
    assert all(categories[name].storage == 'markdown' for name in MARKDOWN_BOT_REFERENCE_FIELDS)
    assert categories['skills'].storage == 'skill-directory' and 'folder' in categories['skills'].reference_forms
    assert categories['tools'].storage == 'tool-directory' and 'folder' not in categories['tools'].reference_forms
    assert categories['mcp'].storage == 'mcp-json'
    assert 'folder' not in categories['expertise'].reference_forms
    with pytest.raises(FrozenInstanceError):
        categories['principles'].name = 'other'


def test_markdown_template_slots_are_all_loaded_from_the_category_view(scene):
    from claudlobby.known_values import MARKDOWN_BOT_REFERENCE_FIELDS
    bot, fleet, paths = scene
    fields = list(MARKDOWN_BOT_REFERENCE_FIELDS)
    for kind in fields:
        _put(paths, kind, 'piece', f'[{kind}] {{{{BOT_NAME}}}}')
        setattr(bot, kind, ['piece'])
    (paths.root / 'templates/claude.md.j2').write_text(''.join(
        '{% for item in ' + kind + ' %}{{ item.body }}|{% endfor %}' for kind in fields))
    assert compose_claude_md(bot, fleet, paths) == ''.join(f'[{kind}] Sample|' for kind in fields)


def test_list_library_includes_principles_permissions_but_not_readme(scene, monkeypatch, caplog):
    _bot, _fleet, paths = scene
    for kind in ('principles', 'permissions'):
        _put(paths, kind, 'nested/listed-' + kind)
        _put(paths, kind, 'README-unlisted')
    monkeypatch.setattr(core, '_resolve_paths', lambda _args: paths)
    with caplog.at_level(logging.INFO, logger='claudlobby'):
        assert core.cmd_list_library(SimpleNamespace()) == 0
    assert 'nested/listed-principles' in caplog.text and 'nested/listed-permissions' in caplog.text
    assert 'README-unlisted' not in caplog.text


def test_existing_warning_and_specialized_expertise_policy_are_unchanged(scene):
    bot, fleet, paths = scene
    bot.guardrails = ['missing-guardrail']
    assert validate(fleet, paths).warnings == ["bot 'sample': guardrail 'missing-guardrail' not in any library/guardrails/ — section will be skipped"]
    bot.expertise = ['folder/']
    _put(paths, 'expertise', 'folder/nested', 'Not folder-capable expertise')
    assert any("expertise 'folder/' not found" in e for e in validate(fleet, paths).errors)


def test_protocol_requires_skill_validation_stays_specialized(scene):
    _bot, fleet, paths = scene
    protocol = _put(paths, 'protocols', 'unequipped', 'Protocol')
    protocol.write_text('---\nrequires:\n  skills: [missing-skill]\n---\nBody\n')
    assert any("requires skill 'missing-skill'" in e for e in validate(fleet, paths).errors)


def test_permissions_markdown_is_prose_not_a_tool_grant_source(scene):
    bot, fleet, paths = scene
    baseline = compose_settings_local(bot, fleet, paths)
    permission = _put(paths, 'permissions', 'described', 'Permission prose')
    permission.write_text('---\ntool_grants: ["Bash(*)"]\npermissions:\n  allow: ["Write(*)"]\n---\nPermission prose\n')
    bot.permissions = ['described']
    assert validate(fleet, paths).warnings == []
    assert compose_settings_local(bot, fleet, paths) == baseline
