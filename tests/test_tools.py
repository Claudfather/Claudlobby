"""Tests for the library tools category — config parsing, tool_resolve,
compose (render/0755/reconcile), validator checks, and diff coverage."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby.composer import compose_tool_outputs, compose_tools
from claudlobby.config import (
    ToolEntry,
    _coerce_bot,
    _merge_tool_lists,
    _parse_tools_list,
    load_fleet,
)
from claudlobby.diff import diff_bot
from claudlobby.paths import Paths
from claudlobby.tool_resolve import (
    RESERVED_TOOL_CONTEXT,
    load_tool_manifest,
    resolve_tool_params,
    tool_context,
    tool_template_path,
)
from claudlobby.validator import validate


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root)


def _write_tool(
    library: Path,
    name: str,
    manifest: str,
    template: str,
    template_name: str | None = None,
) -> Path:
    tool_dir = library / "tools" / name
    tool_dir.mkdir(parents=True)
    (tool_dir / "tool.yaml").write_text(dedent(manifest))
    (tool_dir / (template_name or f"{name}.py.j2")).write_text(dedent(template))
    return tool_dir


GREET_MANIFEST = """\
    type: script
    params:
      subdir:
        default: greetings
      lookback_days:
        default: 7
    env:
      - GREET_TOKEN
"""

GREET_TEMPLATE = """\
    #!/usr/bin/env python3
    OUT_DIR = "{{ data_dir }}/{{ subdir }}"
    LOOKBACK = {{ lookback_days }}
    BOT = "{{ bot_id }}"
    print(OUT_DIR, LOOKBACK, BOT)
"""


class TestToolsListParsing:
    def test_bare_and_dict_forms(self):
        entries = _parse_tools_list(["a", {"b": {"params": {"x": 1}}}], where="t")
        assert entries == [
            ToolEntry(name="a", params={}),
            ToolEntry(name="b", params={"x": 1}),
        ]

    def test_dedupe_first_seen(self):
        entries = _parse_tools_list(["a", "a"], where="t")
        assert [e.name for e in entries] == ["a"]

    def test_legacy_permissions_shape_raises(self):
        with pytest.raises(ValueError, match="tool_permissions"):
            _parse_tools_list({"deny": ["Write"]}, where="t")

    def test_multi_key_dict_entry_raises(self):
        with pytest.raises(ValueError, match="unrecognized"):
            _parse_tools_list([{"a": {}, "b": {}}], where="t")

    def test_non_mapping_config_raises(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            _parse_tools_list([{"a": ["nope"]}], where="t")

    def test_merge_params_override_per_key(self):
        merged = _merge_tool_lists(
            [ToolEntry("a", {"x": 1, "y": 2}), ToolEntry("b", {})],
            [ToolEntry("a", {"y": 3})],
        )
        assert merged == [
            ToolEntry("a", {"x": 1, "y": 3}),
            ToolEntry("b", {}),
        ]

    def test_coerce_bot_merges_defaults(self):
        bot = _coerce_bot(
            "t",
            {"expertise": ["eng"], "tools": [{"a": {"params": {"y": 3}}}]},
            {"tools": [{"a": {"params": {"x": 1, "y": 2}}}, "b"]},
        )
        assert bot.tools == [
            ToolEntry("a", {"x": 1, "y": 3}),
            ToolEntry("b", {}),
        ]


class TestToolResolve:
    def test_missing_manifest_raises(self, tmp_path):
        tool_dir = tmp_path / "t"
        tool_dir.mkdir()
        with pytest.raises(ValueError, match="missing tool.yaml"):
            load_tool_manifest(tool_dir)

    def test_unknown_type_raises(self, tmp_path):
        tool_dir = tmp_path / "t"
        tool_dir.mkdir()
        (tool_dir / "tool.yaml").write_text("type: daemon\n")
        with pytest.raises(ValueError, match="unknown type"):
            load_tool_manifest(tool_dir)

    def test_reserved_param_shadow_raises(self, tmp_path):
        tool_dir = tmp_path / "t"
        tool_dir.mkdir()
        (tool_dir / "tool.yaml").write_text(
            "type: script\nparams:\n  data_dir:\n    default: x\n"
        )
        with pytest.raises(ValueError, match="reserved context"):
            load_tool_manifest(tool_dir)

    def test_sole_template_resolves(self, tmp_path):
        tool_dir = tmp_path / "t"
        tool_dir.mkdir()
        (tool_dir / "tool.yaml").write_text("type: script\n")
        (tool_dir / "run.py.j2").write_text("x")
        assert tool_template_path(tool_dir, {"type": "script"}).name == "run.py.j2"

    def test_ambiguous_templates_raise(self, tmp_path):
        tool_dir = tmp_path / "t"
        tool_dir.mkdir()
        (tool_dir / "tool.yaml").write_text("type: script\n")
        (tool_dir / "a.py.j2").write_text("x")
        (tool_dir / "b.py.j2").write_text("x")
        with pytest.raises(ValueError, match="exactly one"):
            tool_template_path(tool_dir, {"type": "script"})

    def test_declared_template_missing_raises(self, tmp_path):
        tool_dir = tmp_path / "t"
        tool_dir.mkdir()
        with pytest.raises(ValueError, match="not found"):
            tool_template_path(tool_dir, {"template": "nope.j2"})

    def test_param_resolution(self):
        manifest = {
            "params": {
                "a": {"default": 1},
                "b": {"required": True},
                "c": None,
            }
        }
        resolved = resolve_tool_params("t", manifest, {"b": 2})
        assert resolved == {"a": 1, "b": 2}

    def test_missing_required_param_raises(self):
        with pytest.raises(ValueError, match="required param 'b'"):
            resolve_tool_params("t", {"params": {"b": {"required": True}}}, {})

    def test_unknown_override_raises(self):
        with pytest.raises(ValueError, match="unknown param"):
            resolve_tool_params("t", {"params": {"a": {}}}, {"typo": 1})

    def test_context_keys_lockstep_with_reserved(self):
        ctx = tool_context(
            {},
            bot_id="b",
            bot_name="B",
            fleet_name="f",
            bot_dir="/x",
            data_dir="/x/data",
        )
        assert set(ctx) == set(RESERVED_TOOL_CONTEXT)

    def test_base_context_wins_over_params(self):
        ctx = tool_context(
            {"bot_id": "shadow"},
            bot_id="real",
            bot_name="B",
            fleet_name="f",
            bot_dir="/x",
            data_dir="/x/data",
        )
        assert ctx["bot_id"] == "real"


class TestComposeTools:
    def _setup(self, tmp_path, tools_yaml: str) -> tuple:
        root = tmp_path / "claudlobby"
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text(
            "---\ntitle: Eng\n---\n# Eng\n"
        )
        _write_tool(root / "library", "greeter", GREET_MANIFEST, GREET_TEMPLATE)
        (root / "fleet.yaml").write_text(
            dedent(f"""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                worker:
                  expertise: [eng]
{tools_yaml}
            """)
        )
        fleet, _md = load_fleet(root / "fleet.yaml")
        paths = _make_paths(root)
        bot_dir = paths.bot_runtime("worker")
        bot_dir.mkdir(parents=True)
        (bot_dir / "data").mkdir()
        return fleet.bots["worker"], fleet, paths, bot_dir

    def test_render_bakes_params_and_context(self, tmp_path):
        bot, fleet, paths, bot_dir = self._setup(
            tmp_path,
            "                  tools:\n"
            "                    - greeter:\n"
            "                        params:\n"
            "                          lookback_days: 30\n",
        )
        compose_tools(bot, fleet, paths, bot_dir)
        target = bot_dir / "tools" / "greeter.py"
        content = target.read_text()
        assert f'OUT_DIR = "{bot_dir}/data/greetings"' in content
        assert "LOOKBACK = 30" in content
        assert 'BOT = "worker"' in content
        assert target.stat().st_mode & 0o777 == 0o755

    def test_strict_undefined_raises(self, tmp_path):
        bot, fleet, paths, bot_dir = self._setup(
            tmp_path, "                  tools: [greeter]\n"
        )
        tool_dir = paths.find_library_dir("tools", "greeter")
        (tool_dir / "greeter.py.j2").write_text("{{ not_declared }}")
        with pytest.raises(ValueError, match="undeclared/unset"):
            compose_tools(bot, fleet, paths, bot_dir)

    def test_missing_tool_raises(self, tmp_path):
        bot, fleet, paths, bot_dir = self._setup(
            tmp_path, "                  tools: [ghost]\n"
        )
        with pytest.raises(ValueError, match="not in any library/tools/"):
            compose_tools(bot, fleet, paths, bot_dir)

    def test_reconcile_removes_detached_only(self, tmp_path):
        bot, fleet, paths, bot_dir = self._setup(
            tmp_path, "                  tools: [greeter]\n"
        )
        compose_tools(bot, fleet, paths, bot_dir)
        stray = bot_dir / "tools" / "hand-placed.py"
        stray.write_text("gone on next generate")
        data_file = bot_dir / "data" / "ledger.json"
        data_file.write_text("{}")
        compose_tools(bot, fleet, paths, bot_dir)
        assert not stray.exists()
        assert (bot_dir / "tools" / "greeter.py").is_file()
        assert data_file.read_text() == "{}"

    def test_detach_removes_rendered_file(self, tmp_path):
        bot, fleet, paths, bot_dir = self._setup(
            tmp_path, "                  tools: [greeter]\n"
        )
        compose_tools(bot, fleet, paths, bot_dir)
        assert (bot_dir / "tools" / "greeter.py").is_file()
        bot.tools = []
        compose_tools(bot, fleet, paths, bot_dir)
        assert not (bot_dir / "tools" / "greeter.py").exists()

    def test_self_heal_recreates_identical(self, tmp_path):
        bot, fleet, paths, bot_dir = self._setup(
            tmp_path, "                  tools: [greeter]\n"
        )
        compose_tools(bot, fleet, paths, bot_dir)
        target = bot_dir / "tools" / "greeter.py"
        original = target.read_text()
        target.unlink()
        compose_tools(bot, fleet, paths, bot_dir)
        assert target.read_text() == original
        assert target.stat().st_mode & 0o777 == 0o755

    def test_no_tools_no_dir(self, tmp_path):
        bot, fleet, paths, bot_dir = self._setup(tmp_path, "")
        compose_tools(bot, fleet, paths, bot_dir)
        assert not (bot_dir / "tools").exists()

    def test_duplicate_target_raises(self, tmp_path):
        bot, fleet, paths, bot_dir = self._setup(
            tmp_path, "                  tools: [greeter, greeter2]\n"
        )
        _write_tool(
            paths.root / "library",
            "greeter2",
            "type: script\n",
            "x\n",
            template_name="greeter.py.j2",
        )
        with pytest.raises(ValueError, match="duplicate target"):
            compose_tool_outputs(bot, fleet, paths, bot_dir)

    def test_overlay_shadows_base(self, tmp_path):
        bot, fleet, paths, bot_dir = self._setup(
            tmp_path, "                  tools: [greeter]\n"
        )
        overlay = paths.root / "local-overlay"
        # fleet_dir == root in this fixture, so the overlay library is
        # root/library — already exercised. Point a second Paths at a real
        # overlay layout instead.
        (overlay / "library").mkdir(parents=True)
        _write_tool(
            overlay / "library",
            "greeter",
            "type: script\n",
            "OVERLAY\n",
        )
        shadow_paths = Paths(root=paths.root, fleet_dir=overlay)
        outputs = compose_tool_outputs(bot, fleet, shadow_paths, bot_dir)
        assert outputs["greeter.py"] == "OVERLAY\n"


class TestToolsValidation:
    def _setup_root(self, tmp_path, fleet_tools: str) -> tuple:
        root = tmp_path / "claudlobby"
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text(
            "---\ntitle: Eng\n---\n# Eng\n"
        )
        _write_tool(root / "library", "greeter", GREET_MANIFEST, GREET_TEMPLATE)
        (root / "fleet.yaml").write_text(
            dedent(f"""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                worker:
                  expertise: [eng]
{fleet_tools}
            """)
        )
        fleet, _md = load_fleet(root / "fleet.yaml")
        return fleet, _make_paths(root)

    def test_missing_ref_is_error_with_suggestion(self, tmp_path):
        fleet, paths = self._setup_root(tmp_path, "                  tools: [greetr]\n")
        report = validate(fleet, paths)
        assert any(
            "tool 'greetr' not in any library/tools/" in e
            and "did you mean 'greeter'" in e
            for e in report.errors
        )

    def test_unknown_param_is_error(self, tmp_path):
        fleet, paths = self._setup_root(
            tmp_path,
            "                  tools:\n"
            "                    - greeter:\n"
            "                        params:\n"
            "                          typo: 1\n",
        )
        report = validate(fleet, paths)
        assert any("unknown param" in e for e in report.errors)

    def test_env_contract_warns_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GREET_TOKEN", raising=False)
        fleet, paths = self._setup_root(
            tmp_path, "                  tools: [greeter]\n"
        )
        report = validate(fleet, paths)
        assert any("tool 'greeter' requires GREET_TOKEN" in w for w in report.warnings)

    def test_env_contract_quiet_when_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREET_TOKEN", "x")
        fleet, paths = self._setup_root(
            tmp_path, "                  tools: [greeter]\n"
        )
        report = validate(fleet, paths)
        assert not any("GREET_TOKEN" in w for w in report.warnings)

    def test_target_collision_is_validate_error(self, tmp_path):
        fleet, paths = self._setup_root(
            tmp_path, "                  tools: [greeter, greeter2]\n"
        )
        _write_tool(
            paths.root / "library",
            "greeter2",
            "type: script\n",
            "x\n",
            template_name="greeter.py.j2",
        )
        report = validate(fleet, paths)
        assert any(
            "both render 'greeter.py'" in e and "generate would fail" in e
            for e in report.errors
        )

    def test_suggestions_ignore_dirs_without_manifest(self, tmp_path):
        fleet, paths = self._setup_root(
            tmp_path, "                  tools: [not-a-tool]\n"
        )
        (paths.root / "library" / "tools" / "not-a-toolx").mkdir()
        report = validate(fleet, paths)
        err = next(e for e in report.errors if "not-a-tool" in e)
        assert "did you mean" not in err or "greeter" in err

    def test_clean_fleet_no_tool_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREET_TOKEN", "x")
        fleet, paths = self._setup_root(
            tmp_path, "                  tools: [greeter]\n"
        )
        report = validate(fleet, paths)
        assert not any("tool" in e and "greeter" in e for e in report.errors)


class TestToolsDiff:
    def _setup(self, tmp_path) -> tuple:
        root = tmp_path / "claudlobby"
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text(
            "---\ntitle: Eng\n---\n# Eng\n"
        )
        (root / "templates").mkdir()
        (root / "templates" / "claude.md.j2").write_text("# {{ bot.name }}\n")
        _write_tool(root / "library", "greeter", GREET_MANIFEST, GREET_TEMPLATE)
        (root / "fleet.yaml").write_text(
            dedent("""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                worker:
                  expertise: [eng]
                  tools: [greeter]
            """)
        )
        fleet, _md = load_fleet(root / "fleet.yaml")
        paths = _make_paths(root)
        bot_dir = paths.bot_runtime("worker")
        bot_dir.mkdir(parents=True)
        (bot_dir / "data").mkdir()
        return fleet, paths, bot_dir

    def _compose_rest(self, fleet, paths, bot_dir):
        # diff_bot also compares CLAUDE.md/.mcp.json/bot.conf — compose the
        # full bot once so tools drift is the only signal under test.
        from claudlobby.composer import compose_bot

        compose_bot(fleet.bots["worker"], fleet, paths)

    def test_hand_edit_is_drift(self, tmp_path):
        fleet, paths, bot_dir = self._setup(tmp_path)
        self._compose_rest(fleet, paths, bot_dir)
        assert "no drift" in diff_bot("worker", fleet, paths)
        (bot_dir / "tools" / "greeter.py").write_text("tampered\n")
        assert "tools/greeter.py drift" in diff_bot("worker", fleet, paths)

    def test_stray_file_is_drift(self, tmp_path):
        fleet, paths, bot_dir = self._setup(tmp_path)
        self._compose_rest(fleet, paths, bot_dir)
        (bot_dir / "tools" / "stray.py").write_text("x\n")
        assert "tools/stray.py drift" in diff_bot("worker", fleet, paths)

    def test_deleted_file_is_drift(self, tmp_path):
        fleet, paths, bot_dir = self._setup(tmp_path)
        self._compose_rest(fleet, paths, bot_dir)
        (bot_dir / "tools" / "greeter.py").unlink()
        assert "tools/greeter.py drift" in diff_bot("worker", fleet, paths)
