"""Final rendered prompt accounting through validate/generate on private roots."""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby import composer
from claudlobby.commands import core
from claudlobby.config import BotConfig, FleetConfig, SystemDefaultsConfig
from claudlobby.paths import Paths
from claudlobby.validator import validate


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "library/expertise").mkdir(parents=True)
    (root / "library/expertise/demo.md").write_text("# Demo\n\nEngineer.\n")
    (root / "library/protocols").mkdir()
    protocol = root / "library/protocols/example.md"
    protocol.write_text("---\ntitle: Example\n---\nSmall instruction.\n")
    (root / "templates").mkdir()
    (root / "templates/claude.md.j2").write_text(
        "# {{ bot.name }}\n{{ expertise_body }}\n## Protocols\n"
        "{% for p in protocols %}{{ p.body }}\n{% endfor %}"
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    bot = BotConfig(bot_id="example", name="Example", expertise=["demo"], protocols=["example"])
    fleet = FleetConfig(name="demo", service_prefix="com.example", bots={"example": bot},
                        system_defaults=SystemDefaultsConfig(enabled=False))
    paths = Paths(root=root)
    monkeypatch.setattr(core, "_resolve_paths", lambda _args: paths)
    monkeypatch.setattr(core, "_load_env", lambda _paths: None)
    monkeypatch.setattr(core, "_load_fleet_or_exit", lambda _paths: (fleet, {}))
    assert validate(fleet, paths).warnings == []
    return bot, fleet, paths, protocol


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def test_real_validation_measures_rendered_content_and_strict_promotes_warning(scene):
    bot, fleet, paths, protocol = scene
    protocol.write_text("---\ntitle: Example\n---\n" + "é" * 22_000 + "\n")
    rendered = composer.compose_claude_md(bot, fleet, paths)
    assert len(rendered) < 40_000 < len(rendered.encode("utf-8"))
    before = snapshot(paths.root)
    report = validate(fleet, paths)
    warnings = [w for w in report.warnings if "composed CLAUDE.md" in w]
    assert len(warnings) == 1
    assert str(len(rendered.encode("utf-8"))) in warnings[0]
    assert "example" in warnings[0] and "Protocols" in warnings[0] and "40000" in warnings[0]
    assert core.cmd_validate(SimpleNamespace(strict=False)) == 0
    assert core.cmd_validate(SimpleNamespace(strict=True)) == 1
    assert snapshot(paths.root) == before
    assert not paths.runtime.exists()


def test_strict_generate_refuses_oversize_before_writing(scene):
    _bot, _fleet, paths, protocol = scene
    protocol.write_text("# Example\n\n" + "x" * 45_000)
    before = snapshot(paths.root)
    assert core.cmd_generate(SimpleNamespace(strict=True, bot="example")) == 1
    assert snapshot(paths.root) == before
    assert not paths.runtime.exists()


@pytest.mark.parametrize("selected", ["example", None])
def test_generate_reports_only_actual_selected_files(scene, monkeypatch, caplog, selected):
    bot, fleet, paths, _protocol = scene
    fleet.bots["other"] = BotConfig(bot_id="other", name="Other", expertise=["demo"], bench=True)
    # Keep the actual bot composer; unrelated fleet/host jobs and plane effects
    # are outside this prompt-reporting contract and never run in this fixture.
    for name in ("compose_fleet_timers", "compose_host_timers", "compose_host_bot_handles", "compose_host_mention_allowlist"):
        monkeypatch.setattr(composer, name, lambda *_args, **_kwargs: paths.root / "unused")
    monkeypatch.setattr(core, "_warn_unresolvable_skill_refs", lambda _paths: None)
    from claudlobby.plane import registry_emit
    monkeypatch.setattr(registry_emit, "run_generate_scan", lambda *_args, **_kwargs: None)
    with caplog.at_level(logging.INFO, logger="claudlobby"):
        assert core.cmd_generate(SimpleNamespace(strict=False, bot=selected)) == 0
    generated = (paths.bot_runtime(bot.bot_id) / "CLAUDE.md").read_bytes()
    records = [r.getMessage() for r in caplog.records if "composed size" in r.getMessage()]
    assert len(records) == (1 if selected else 2)
    assert "example" in records[0] and "other" not in records[0]
    if not selected:
        assert "other" in records[1]
    assert str(len(generated)) in records[0] and "Protocols" in records[0] and "lines" in records[0]
    assert paths.bot_runtime("other").exists() is (selected is None)


def test_small_composition_keeps_old_validation_result(scene):
    _bot, fleet, paths, _protocol = scene
    assert validate(fleet, paths).warnings == []
    assert core.cmd_validate(SimpleNamespace(strict=True)) == 0


@pytest.mark.parametrize("size,expected", [(0, False), (30_000, False), (40_000, False), (40_001, True)])
def test_warning_boundary_uses_bytes_without_role_or_token_estimation(size, expected):
    from claudlobby.prompt_budget import measure, over_budget
    result = measure("x" * size)
    assert result.nbytes == size
    assert bool(over_budget(result)) == expected


def test_sections_use_real_h2_boundaries_and_keep_fenced_bytes_in_owner():
    from claudlobby.prompt_budget import measure
    markdown = "Preamble é\n## Alpha\n```md\n## FAKE\n```\n~~~\n## FAKE2\n~~~\n## Beta ###\nβ\n"
    result = measure(markdown)
    assert result.nbytes == len(markdown.encode("utf-8")) and result.lines == 10
    assert [(s.name, s.start_line, s.lines) for s in result.sections] == [("Alpha", 2, 7), ("Beta", 9, 2)]
    assert sum(s.nbytes for s in result.sections) + len("Preamble é\n".encode()) == result.nbytes


def test_fence_length_indent_and_unclosed_fences_do_not_invent_sections():
    from claudlobby.prompt_budget import measure
    markdown = "## Before\n   ````python\n## hidden\n```\n## still hidden\n````\n   ## After\n    ## code\n~~~\n## hidden to EOF"
    assert [s.name for s in measure(markdown).sections] == ["Before", "After"]


def test_largest_sections_rank_bytes_and_ties_by_source_order():
    from claudlobby.prompt_budget import measure, summary
    markdown = "## A\n12345\n## B\n12345\n## C\n" + "é" * 8 + "\n## D\nx\n"
    result = measure(markdown)
    assert [s.name for s in result.largest] == ["C", "A", "B"]
    line = summary(result)
    assert "C" in line and "A" in line and "B" in line and "D:" not in line
    assert "token" not in line.lower()


@pytest.mark.parametrize("size", [39_999, 40_000, 40_001])
def test_actual_validation_counts_final_template_bytes_including_marker_overhead(scene, size):
    _bot, fleet, paths, _protocol = scene
    marker = "<!-- Generated by compositor -->\n"
    (paths.root / "templates/claude.md.j2").write_text(marker + "x" * (size - len(marker)))
    warnings = [w for w in validate(fleet, paths).warnings if "composed CLAUDE.md" in w]
    assert len(warnings) == (1 if size > 40_000 else 0)
    if warnings:
        assert "40001 bytes" in warnings[0] and "no H2 sections" in warnings[0]


def test_validation_discloses_unmeasurable_template_without_its_contents(scene):
    _bot, fleet, paths, _protocol = scene
    (paths.root / "templates/claude.md.j2").write_text("{% PRIVATE_INVALID_TAG %}")
    warnings = [w for w in validate(fleet, paths).warnings if "composed CLAUDE.md" in w]
    assert len(warnings) == 1 and "size unavailable (TemplateSyntaxError)" in warnings[0]
    assert "budget was not checked" in warnings[0] and "PRIVATE_INVALID_TAG" not in warnings[0]


def test_reporting_is_read_only_and_continues_after_an_unreadable_artifact(scene, caplog):
    _bot, _fleet, paths, _protocol = scene
    good = paths.bot_runtime("good") / "CLAUDE.md"
    good.parent.mkdir(parents=True)
    good.write_bytes("## Good\r\né\r\n".encode("utf-8"))
    bad = paths.bot_runtime("bad") / "CLAUDE.md"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"\xffPRIVATE_BAD_ENCODING")
    before = snapshot(paths.root)
    with caplog.at_level(logging.INFO, logger="claudlobby"):
        core._report_composed_sizes(paths, ["bad", "missing", "good"])
    assert "bot 'bad': composed size unavailable (UnicodeDecodeError)" in caplog.text
    assert "bot 'missing': composed size unavailable (FileNotFoundError)" in caplog.text
    assert "bot 'good': composed size 13 bytes, 2 lines" in caplog.text
    assert "PRIVATE_BAD_ENCODING" not in caplog.text
    assert snapshot(paths.root) == before


def test_non_strict_generate_warns_once_and_reports_final_size(scene, monkeypatch, caplog):
    bot, _fleet, paths, protocol = scene
    protocol.write_text("# Example\n\n" + "x" * 45_000)
    for name in ("compose_fleet_timers", "compose_host_timers", "compose_host_bot_handles", "compose_host_mention_allowlist"):
        monkeypatch.setattr(composer, name, lambda *_args, **_kwargs: paths.root / "unused")
    monkeypatch.setattr(core, "_warn_unresolvable_skill_refs", lambda _paths: None)
    from claudlobby.plane import registry_emit
    monkeypatch.setattr(registry_emit, "run_generate_scan", lambda *_args, **_kwargs: None)
    with caplog.at_level(logging.INFO, logger="claudlobby"):
        assert core.cmd_generate(SimpleNamespace(strict=False, bot=bot.bot_id)) == 0
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "composed CLAUDE.md" in r.getMessage()]
    assert len(warnings) == 1
    size = len((paths.bot_runtime(bot.bot_id) / "CLAUDE.md").read_bytes())
    assert str(size) in warnings[0]
    assert any(f"composed size {size} bytes" in r.getMessage() for r in caplog.records)


def test_markdown_newlines_and_empty_h2_use_physical_lines():
    from claudlobby.prompt_budget import measure
    markdown = "## ###\r\nprose\u2028## not a heading\n##\rbody"
    result = measure(markdown)
    assert result.lines == 4
    assert [(s.name, s.start_line, s.lines) for s in result.sections] == [
        ("(untitled)", 1, 2), ("(untitled)", 3, 2)]
    assert sum(s.nbytes for s in result.sections) == len(markdown.encode("utf-8"))
