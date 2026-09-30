"""Diff previews settings, units and only compositor-owned channel fields."""
import json
import os
from pathlib import Path

import pytest

from claudlobby.composer import bot_boot_delay_s, compose_bot, telegram_channel_rel
from claudlobby.config import load_fleet
from claudlobby.diff import diff_bot
from claudlobby.paths import Paths


@pytest.fixture
def composed(fleet_dir, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    paths = Paths(root=fleet_dir)
    fleet, _ = load_fleet(paths.fleet_yaml)
    fleet.human_telegram_id = '12345'
    bot = fleet.bots['lead']
    compose_bot(bot, fleet, paths)
    channel = home / telegram_channel_rel(bot.telegram.handle) / 'access.json'
    assert channel.exists()
    return fleet, paths, paths.bot_runtime(bot.bot_id), channel


def _diff(composed):
    fleet, paths, _, _ = composed
    return diff_bot('lead', fleet, paths)


def _snapshot(root):
    result = {}
    for p in [root, *sorted(root.rglob('*'))]:
        st = p.lstat()
        data = os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None
        result[str(p)] = (st.st_mode, st.st_size, st.st_mtime_ns, data)
    return result


def test_clean_compare_is_read_only_and_names_coverage(composed):
    fleet, paths, _, channel = composed
    before = _snapshot(paths.root), _snapshot(channel.parents[3])
    output = _diff(composed)
    assert 'no drift in lead' in output
    assert 'not compared: skill/command/agent symlinks' in output
    assert before == (_snapshot(paths.root), _snapshot(channel.parents[3]))


def test_settings_runtime_permission_is_reported(composed):
    path = composed[2] / '.claude/settings.local.json'
    obj = json.loads(path.read_text())
    obj['permissions']['allow'].append('Bash(echo runtime-grant)')
    path.write_text(json.dumps(obj))
    result = _diff(composed)
    assert 'settings.local.json drift' in result
    assert 'runtime-grant' in result
    assert 'dropped on next generate' in result


@pytest.mark.parametrize('extension', ['service', 'plist'])
def test_bot_unit_edit_is_reported(composed, extension):
    fleet, _, bot_dir, _ = composed
    path = bot_dir / f'{fleet.service_prefix}.lead.{extension}'
    path.write_text(path.read_text() + '\nunit-drift-marker\n')
    result = _diff(composed)
    assert f'.{extension} drift' in result and 'unit-drift-marker' in result


def test_missing_settings_is_drift(composed):
    (composed[2] / '.claude/settings.local.json').unlink()
    assert 'settings.local.json drift' in _diff(composed)


@pytest.mark.parametrize('change', ['human', 'mention', 'dmPolicy'])
def test_access_owned_fields_are_reported(composed, change):
    fleet, _, _, path = composed
    data = json.loads(path.read_text())
    if change == 'human':
        data['allowFrom'] = ['runtime-approved']
    elif change == 'mention':
        data['groups'][fleet.telegram_group_chat_id]['requireMention'] = True
    else:
        data['dmPolicy'] = 'disabled'
    path.write_text(json.dumps(data))
    result = _diff(composed)
    assert 'access.json drift' in result
    assert {'human':'declaredHumanAllowed','mention':'requireMention','dmPolicy':'dmPolicy'}[change] in result


def test_access_runtime_state_is_preserved_and_not_drift(composed):
    _, paths, _, path = composed
    data = json.loads(path.read_text())
    data['allowFrom'].append('runtime-approved')
    data['pending'] = {'runtime-request': {'nonce': 'private'}}
    data['groups']['extra'] = {'requireMention': True, 'allowFrom': ['runtime-approved']}
    path.write_text(json.dumps(data))
    before = _snapshot(paths.root), _snapshot(path.parent)
    assert 'no drift in lead' in _diff(composed)
    assert before == (_snapshot(paths.root), _snapshot(path.parent))


def test_no_declared_human_ignores_allowlist(composed):
    fleet, _, _, path = composed
    fleet.human_telegram_id = ''
    # Update settings/conf after the intentional manifest change.
    compose_bot(fleet.bots['lead'], fleet, composed[1])
    data = json.loads(path.read_text())
    data['allowFrom'] = ['runtime-approved']
    path.write_text(json.dumps(data))
    assert 'no drift in lead' in _diff(composed)


@pytest.mark.parametrize(('artifact', 'contents'), [
    ('settings', '{INVALID_SECRET'), ('settings', '[]'),
    ('access', '{INVALID_SECRET'), ('access', '[]'),
    ('access', '{"groups": []}'),
])
def test_invalid_artifact_json_is_reported(composed, artifact, contents):
    path = composed[3] if artifact == 'access' else composed[2] / '.claude/settings.local.json'
    path.write_text(contents)
    result = _diff(composed)
    assert path.name in result and 'invalid' in result.lower()
    assert 'no drift' not in result and 'INVALID_SECRET' not in result


@pytest.mark.parametrize('artifact', ['settings', 'access'])
def test_unreadable_artifact_is_named(composed, monkeypatch, artifact):
    path = composed[3] if artifact == 'access' else composed[2] / '.claude/settings.local.json'
    read_text = Path.read_text
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError('synthetic refusal')
        return read_text(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', denied)
    result = _diff(composed)
    assert path.name in result and 'unreadable' in result.lower() and 'no drift' not in result


def test_absent_channel_is_not_created_or_reported_as_drift(composed):
    path = composed[3]
    path.unlink()
    before = _snapshot(path.parent)
    result = _diff(composed)
    assert 'no drift in lead' in result
    assert 'access.json absent (not compared)' in result
    assert before == _snapshot(path.parent)


def test_unit_diff_uses_host_boot_ladder(fleet_dir, tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    local = fleet_dir / 'local'
    for name in ('alpha', 'zeta'):
        target = local / name
        target.mkdir(parents=True)
        (target / 'fleet.yaml').write_bytes((fleet_dir / 'fleet.yaml').read_bytes())
    paths = Paths(root=fleet_dir, fleet_dir=local / 'zeta')
    fleet, _ = load_fleet(paths.fleet_yaml)
    bot = fleet.bots['lead']
    delay = bot_boot_delay_s(bot, fleet, paths)
    assert delay > 0  # first bot of second fleet, not index * stagger
    compose_bot(bot, fleet, paths)
    unit = paths.bot_runtime('lead') / f'{fleet.service_prefix}.lead.service'
    assert f'/bin/sleep {delay}' in unit.read_text()
    assert 'no drift in lead' in diff_bot('lead', fleet, paths)


def test_access_numeric_mention_rule_is_not_equal_to_boolean(composed):
    fleet, _, _, path = composed
    data = json.loads(path.read_text())
    data['groups'][fleet.telegram_group_chat_id]['requireMention'] = 0
    path.write_text(json.dumps(data))
    assert 'access.json drift' in _diff(composed)
