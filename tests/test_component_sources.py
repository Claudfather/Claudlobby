"""Source markers identify actual rendered components without instruction rewrites."""
from pathlib import Path
import logging

import pytest

from claudlobby.composer import compose_claude_md
from claudlobby.config import BotConfig, FleetConfig, SystemDefaultsConfig
from claudlobby.paths import Paths
from claudlobby.validator import validate
from claudlobby.commands import core

REPO = Path(__file__).resolve().parents[1]
FIELDS = ('resources', 'integrations', 'principles', 'permissions', 'protocols', 'guardrails', 'lessons', 'post_actions')


def put(root, relative, text):
    p = root / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    overlay = root / 'local/demo'
    paths = Paths(root=root, fleet_dir=overlay)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    put(root, 'templates/claude.md.j2', (REPO / 'templates/claude.md.j2').read_text())
    put(root, 'library/expertise/eng.md', '# Engineer\n\n## Work\nBuild.\n')
    put(overlay, 'library/expertise/second.md', '# Extra\n\nSecond expertise.\n')
    put(root, 'voices/plain.md', '---\ntitle: Plain\n---\nCalm voice.\n')
    bot = BotConfig(bot_id='example', name='Example', expertise=['eng', 'second'], voice='plain.md')
    for kind in FIELDS:
        put(root, f'library/{kind}/piece.md', f'---\ntitle: {kind}\n---\n{kind} body {{{{BOT_NAME}}}}.\n')
        setattr(bot, kind, ['piece'])
    put(overlay, 'library/resources/piece.md', '---\ntitle: Resource\n---\nOVERLAY resource {{BOT_NAME}}.\n')
    fleet = FleetConfig(name='demo', service_prefix='com.example', bots={'example': bot},
                        system_defaults=SystemDefaultsConfig(enabled=False))
    return bot, fleet, paths


def test_real_renderer_marks_every_actual_component_and_selected_source_tier(scene):
    bot, fleet, paths = scene
    rendered = compose_claude_md(bot, fleet, paths)
    starts = [line for line in rendered.splitlines() if line.startswith('<!-- claudlobby:source ')]
    assert len(starts) == 11
    assert '<!-- claudlobby:source fleet/library/resources/piece.md -->' in starts
    assert '<!-- claudlobby:source shared/library/resources/piece.md -->' not in starts
    assert '<!-- claudlobby:source fleet/library/expertise/second.md -->' in starts
    assert '<!-- claudlobby:source shared/voices/plain.md -->' in starts
    assert all(str(paths.root) not in line for line in starts)
    assert rendered.count('<!-- /claudlobby:source -->') == len(starts)
    assert 'OVERLAY resource Example.' in rendered


def test_duplicate_h2_warning_names_expertise_and_template_source(scene):
    bot, fleet, paths = scene
    put(paths.root, 'library/expertise/eng.md', '# Engineer\n\n## Principles\nExpertise rule.\n')
    warnings = [w for w in validate(fleet, paths).warnings if 'duplicate H2' in w]
    assert len(warnings) == 1
    assert 'Principles' in warnings[0] and "bot 'example'" in warnings[0]
    assert 'shared/library/expertise/eng.md' in warnings[0]
    assert 'shared/templates/claude.md.j2' in warnings[0]
    assert 'line ' in warnings[0] and str(paths.root) not in warnings[0]


def test_unique_headings_do_not_add_collision_warnings(scene):
    _bot, fleet, paths = scene
    assert not [w for w in validate(fleet, paths).warnings if 'duplicate H2' in w]


def test_final_attribution_counts_marker_overhead_and_template_remainder(scene):
    from claudlobby.component_sources import attribute
    bot, fleet, paths = scene
    rendered = compose_claude_md(bot, fleet, paths)
    report = attribute(rendered)
    assert report.available and len(report.components) == 11
    assert report.total_bytes == len(rendered.encode('utf-8'))
    assert report.unattributed_bytes > 0
    assert sum(c.nbytes for c in report.components) + report.unattributed_bytes == report.total_bytes
    assert all(c.marker_bytes > 0 and c.nbytes > c.marker_bytes for c in report.components)
    assert [c.source for c in report.components][:3] == [
        'shared/voices/plain.md', 'shared/library/expertise/eng.md', 'fleet/library/expertise/second.md']


def test_source_labels_escape_comment_delimiters_without_host_paths(scene):
    from claudlobby.component_sources import source_label
    _bot, _fleet, paths = scene
    path = paths.base_library / 'principles/odd-->name\n.md'
    label = source_label(path, paths)
    assert label == 'shared/library/principles/odd--%3Ename%0A.md'
    assert source_label(Path('/private/unknown/component.md'), paths) == 'unknown/component.md'
    assert source_label(paths.root / 'voices/../outside.md', paths) == 'unknown/outside.md'


@pytest.mark.parametrize('text', ['no markers', '<!-- claudlobby:source shared/library/x.md -->\nbody',
    '\n<!-- /claudlobby:source -->',
    '<!-- claudlobby:source shared/a -->\n<!-- claudlobby:source shared/b -->\nbody\n<!-- /claudlobby:source -->'])
def test_absent_or_broken_markers_disclose_unavailable_attribution(text):
    from claudlobby.component_sources import attribute
    result = attribute(text)
    assert not result.available and result.reason
    assert result.components == () and result.unattributed_bytes == len(text.encode('utf-8'))


def test_custom_template_without_markers_reports_unavailable_not_input_body_sizes(scene, caplog):
    bot, fleet, paths = scene
    put(paths.fleet_dir, 'templates/claude.md.j2', '# Custom\n{{ bot.name }}\n')
    rendered = compose_claude_md(bot, fleet, paths)
    put(paths.bot_runtime(bot.bot_id), 'CLAUDE.md', rendered)
    before = (paths.bot_runtime(bot.bot_id) / 'CLAUDE.md').read_bytes()
    with caplog.at_level(logging.INFO, logger='claudlobby'):
        core._report_composed_sources(paths, [bot.bot_id])
    assert 'component attribution unavailable' in caplog.text and 'no source markers' in caplog.text
    assert 'shared/library/resources' not in caplog.text
    assert (paths.bot_runtime(bot.bot_id) / 'CLAUDE.md').read_bytes() == before


def test_end_marker_does_not_charge_following_template_heading_to_component():
    from claudlobby.component_sources import attribute, duplicate_heading_sources
    opening = '<!-- claudlobby:source fleet/library/expertise/one.md -->\r\n'
    body = '## Same\r\nα\r\n```\r\n## Hidden\r\n```'
    closing = '\r\n<!-- /claudlobby:source -->'
    tail = '\r\n\r\n## Same\r\nTemplate text\r\n'
    markdown = 'Preamble\r\n' + opening + body + closing + tail
    result = attribute(markdown)
    assert result.available and len(result.components) == 1
    component = result.components[0]
    assert (component.start_line, component.end_line) == (2, 8)
    assert component.nbytes == len((opening + body + closing).encode('utf-8'))
    assert component.marker_bytes == len((opening + closing).encode('utf-8'))
    assert result.unattributed_bytes == len(('Preamble\r\n' + tail).encode('utf-8'))
    warnings = duplicate_heading_sources(markdown, 'fleet/templates/claude.md.j2')
    assert warnings == ["duplicate H2 'Same': fleet/library/expertise/one.md line 3; fleet/templates/claude.md.j2 (template/unattributed) line 10"]


def test_literal_or_nested_reserved_markers_refuse_attribution_instead_of_guessing():
    from claudlobby.component_sources import attribute
    markdown = ('<!-- claudlobby:source shared/outer -->\n```\n'
                '<!-- claudlobby:source shared/example -->\n```\n'
                '<!-- /claudlobby:source -->')
    assert attribute(markdown).reason == 'nested source markers'


def test_report_lists_exact_components_and_keeps_artifacts_unchanged(scene, caplog):
    from claudlobby.component_sources import attribute
    bot, fleet, paths = scene
    rendered = compose_claude_md(bot, fleet, paths)
    path = put(paths.bot_runtime(bot.bot_id), 'CLAUDE.md', rendered)
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
    result = attribute(rendered)
    with caplog.at_level(logging.INFO, logger='claudlobby'):
        core._report_composed_sources(paths, ['absent', bot.bot_id])
    assert 'unavailable (FileNotFoundError)' in caplog.text
    for component in result.components:
        assert f'{component.source}: {component.nbytes} final bytes ({component.marker_bytes} marker bytes)' in caplog.text
    assert f'{result.unattributed_bytes} template/unattributed bytes' in caplog.text
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) == before


def test_empty_expertise_body_does_not_manufacture_a_component(scene):
    from claudlobby.component_sources import attribute
    bot, fleet, paths = scene
    put(paths.root, 'library/expertise/eng.md', '# Engineer\n')
    result = attribute(compose_claude_md(bot, fleet, paths))
    assert result.available and len(result.components) == 10
    assert 'shared/library/expertise/eng.md' not in {c.source for c in result.components}
