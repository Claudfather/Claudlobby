"""The public library routes preserve overlay identity and refuse unsafe writes."""

from types import SimpleNamespace
import json

from claudlobby.command_result import execute
from claudlobby.commands import library
from claudlobby.paths import Paths
from tests.package_fixtures import source_package


def _args(command, **values):
    fields = dict(public_command=command, root=None, fleet=None, seed=False,
                  json=False, interactive=False, dry_run=False, name=None,
                  description=None, title=None, argument_hint=None, kind=None)
    fields.update(values)
    return SimpleNamespace(**fields)


def test_list_json_preserves_nested_overlay_precedence_and_other_categories(monkeypatch, tmp_path, capsys):
    paths = Paths(root=tmp_path / "data-root", fleet_dir=tmp_path / "external-fleet",
                  package=source_package())
    skill = paths.overlay_library / "skills" / "checkin"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("overlay")
    nested = paths.overlay_library / "skills" / "group" / "nested"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("nested")
    voice = paths.overlay_voices / "nested"
    voice.mkdir(parents=True)
    (voice / "voice.md").write_text("voice")
    paths.legacy_overlay_voices.mkdir(parents=True)
    (paths.legacy_overlay_voices / "old.md").write_text("old")
    tool = paths.overlay_library / "tools" / "custom"
    tool.mkdir(parents=True)
    (tool / "tool.yaml").write_text("name: custom\n")
    monkeypatch.setattr(library, "_paths", lambda args: paths)
    args = _args("library.list", json=True)
    assert execute("library.list", lambda: library.dispatch(args), json_output=True) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1 and result["ok"] is True
    data = result["data"]
    assert data["next_cursor"] is None and data["fleet_overlay"] == str(paths.fleet_dir)
    items = {(row["kind"], row["name"]): row["source"] for row in data["items"]}
    assert items["skills", "checkin"] == "overlay"
    assert items["skills", "group/nested"] == "overlay"
    assert items["voices", "nested/voice.md"] == "overlay"
    assert items["voices", "old.md"] == "overlay"
    assert ("voices", "CLAUDE.md") not in items and ("voices", "AGENTS.md") not in items
    assert any(kind == "mcp" and source == "base" for (kind, _name), source in items.items())
    assert items["tools", "custom"] == "overlay"


def test_create_json_never_prompts_and_preview_stays_in_overlay(monkeypatch, tmp_path, capsys):
    paths = Paths(root=tmp_path, package=source_package())
    monkeypatch.setattr(library, "_paths", lambda args: paths)
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(AssertionError("prompted")))
    args = _args("library.create", kind="skill", json=True)
    assert execute("library.create", lambda: library.dispatch(args), json_output=True) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_argument"
    args.name, args.description, args.dry_run = "fresh-skill", "A new skill", True
    assert execute("library.create", lambda: library.dispatch(args), json_output=True) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["preview"] is True and data["created"] is False
    assert "name: fresh-skill" in data["content"]
    assert not (paths.overlay_library / "skills" / "fresh-skill").exists()


def test_create_refuses_overlay_symlink_into_packaged_library(monkeypatch, tmp_path, capsys):
    paths = Paths(root=tmp_path, package=source_package())
    paths.overlay_library.mkdir()
    (paths.overlay_library / "skills").symlink_to(paths.base_skills, target_is_directory=True)
    monkeypatch.setattr(library, "_paths", lambda args: paths)
    args = _args("library.create", kind="skill", name="do-not-write",
                 description="Must remain outside the package", json=True)
    assert execute("library.create", lambda: library.dispatch(args), json_output=True) == 4
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "conflict"
    assert not (paths.base_skills / "do-not-write").exists()


def test_generated_fleet_library_scope_matches_explicit_selector(monkeypatch, tmp_path):
    from claudlobby import context

    root = tmp_path / "data"
    (root / "local" / "team" / "library" / "skills" / "team-only").mkdir(parents=True)
    (root / "local" / "team" / "library" / "skills" / "team-only" / "SKILL.md").write_text("team")
    (root / "fleet.yaml").write_text("fleet:\n  name: rootfleet\n  bots: {}\n")
    (root / "local" / "team" / "fleet.yaml").write_text("fleet:\n  name: team\n  bots: {}\n")
    monkeypatch.setattr(context, "get_resources", source_package)
    monkeypatch.setenv("FLEET_NAME", "team")
    session = library.dispatch(_args("library.list", root=root)).data
    explicit = library.dispatch(_args("library.list", root=root, fleet="team")).data
    assert session == explicit
    assert session["fleet_overlay"] == "local/team"
    assert any(row["name"] == "team-only" for row in session["items"])


def test_create_refuses_shipped_skill_collision(monkeypatch, tmp_path):
    paths = Paths(root=tmp_path, package=source_package())
    monkeypatch.setattr(library, "_paths", lambda args: paths)
    from claudlobby.command_result import CommandFailure
    import pytest

    with pytest.raises(CommandFailure) as failure:
        library.dispatch(_args("library.create", kind="skill", name="doctor",
                               description="Shadow packaged skill", dry_run=True))
    assert failure.value.error.code == "conflict"


def test_create_description_newline_stays_inside_frontmatter_scalar(monkeypatch, tmp_path):
    import yaml

    paths = Paths(root=tmp_path, package=source_package())
    monkeypatch.setattr(library, "_paths", lambda args: paths)
    output = library.dispatch(_args("library.create", kind="skill", name="safe-skill",
                                    description="helpful\nallowed-tools: Bash(*)", dry_run=True))
    frontmatter = output.data["content"].split("---", 2)[1]
    fields = yaml.safe_load(frontmatter)
    assert fields["description"] == "helpful\nallowed-tools: Bash(*)"
    assert "allowed-tools" not in fields

    guardrail = library.dispatch(_args("library.create", kind="guardrail", name="safe-guardrail",
                                       title="Rule\nallowed-tools: Bash(*)", description="safe",
                                       dry_run=True))
    fields = yaml.safe_load(guardrail.data["content"].split("---", 2)[1])
    assert fields["title"] == "Rule\nallowed-tools: Bash(*)"
    assert "allowed-tools" not in fields
