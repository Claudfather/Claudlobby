"""Strict field-wise telemetry arming and the native exporter contract (#2145)."""

import pytest

from claudlobby.config import (load_fleet, scalar_config_origin, TelemetryConfig,
                               _STRICT_MAPPING_FIELDS, _parse_strict_mapping)
from claudlobby.composer import compose_bot_conf
from claudlobby.known_values import OTEL_INTAKE_PORT
from claudlobby.paths import Paths
from tests.package_fixtures import source_package


def fleet_file(tmp_path, defaults=None, bot=None):
    import yaml
    path = tmp_path / 'fleet.yaml'
    path.write_text(yaml.safe_dump({'fleet': {'name': 'canary', 'manager': 'canary-1', 'system_defaults': False,
                    'defaults': defaults or {}, 'bots': {'canary-1': {'expertise': ['software-engineering'], **(bot or {})}}}}))
    return path


@pytest.mark.parametrize('defaults,bot,expected', [
    ({}, {}, TelemetryConfig()),
    ({'enabled': True, 'content': 'full'}, {}, TelemetryConfig(True, 'full')),
    ({'content': 'full'}, {'enabled': True}, TelemetryConfig(True, 'full')),
    ({'enabled': True}, {'enabled': False}, TelemetryConfig(False)),
    ({'content': 'full'}, {'content': None}, TelemetryConfig(False, 'full')),
])
def test_fieldwise_inheritance(tmp_path, defaults, bot, expected):
    fleet, _ = load_fleet(fleet_file(tmp_path, {'telemetry': defaults}, {'telemetry': bot}))
    assert fleet.bots['canary-1'].telemetry == expected


@pytest.mark.parametrize('value,message', [
    ({'enabled': 'yes'}, 'must be a YAML boolean'),
    ({'content': 'loud'}, 'Invalid bots.canary-1.telemetry.content'),
    ({'enable': True}, 'unknown key\\(s\\) enable — accepted: enabled, content'),
    (True, 'must be'),
])
def test_strict_arming_and_shape(tmp_path, value, message):
    with pytest.raises(ValueError, match=message):
        load_fleet(fleet_file(tmp_path, bot={'telemetry': value}))


@pytest.mark.parametrize('name', _STRICT_MAPPING_FIELDS)
def test_registered_mappings_refuse_unknown_fields(name):
    with pytest.raises(ValueError, match='unknown key.*misspelled'):
        _parse_strict_mapping({'misspelled': True}, name, _STRICT_MAPPING_FIELDS[name])


def test_subfield_provenance(tmp_path):
    path = fleet_file(tmp_path, {'telemetry': {'content': 'full'}}, {'telemetry': {'enabled': True}})
    _, defaults = load_fleet(path)
    assert scalar_config_origin(path, defaults, 'telemetry.enabled', bot='canary-1') == ('bot', 'fleet.bots.canary-1.telemetry.enabled')
    assert scalar_config_origin(path, defaults, 'telemetry.content', bot='canary-1') == ('fleet.defaults', 'fleet.defaults.telemetry.content')
    path = fleet_file(tmp_path)
    assert scalar_config_origin(path, {}, 'telemetry.enabled', bot='canary-1') == ('built_in', None)
    with pytest.raises(NotImplementedError):
        scalar_config_origin(path, {}, 'telemetry', bot='canary-1')


@pytest.mark.parametrize('enabled,content,cli,count', [(False, 'metadata', 'claude', 0), (True, 'metadata', 'claude', 7),
                                                      (True, 'full', 'claude', 8), (True, 'full', 'codex', 0)])
def test_exporter_block(tmp_path, enabled, content, cli, count):
    fleet, _ = load_fleet(fleet_file(tmp_path, bot={'telemetry': {'enabled': enabled, 'content': content}, 'agent_cli': cli}))
    conf = compose_bot_conf(fleet.bots['canary-1'], fleet, Paths(root=tmp_path, package=source_package()))
    lines = [line for line in conf.splitlines() if line.startswith('export OTEL_') or line.startswith('export CLAUDE_CODE_ENABLE_TELEMETRY=')]
    assert len(lines) == count
    if count:
        assert f'export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:{OTEL_INTAKE_PORT}' in lines
        assert 'export OTEL_EXPORTER_OTLP_PROTOCOL=http/json' in lines
        assert 'export CLAUDE_CODE_ENABLE_TELEMETRY=1' in lines
        assert 'export OTEL_LOG_TOOL_DETAILS=1' in lines
        assert any(f'claudlobby.content={content}' in line for line in lines)
        assert ('export OTEL_LOG_USER_PROMPTS=1' in lines) == (content == 'full')
    else:
        assert 'OTEL_' not in conf
