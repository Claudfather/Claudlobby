"""MCP identities are unique before any runtime files or grants are produced."""

import json
from pathlib import Path

import pytest
import yaml

from claudlobby import composer, validator
from claudlobby.config import McpEntry, load_fleet
from claudlobby.paths import Paths
from tests.conftest import load_test_fleet


@pytest.fixture
def scene(fleet_dir, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    monkeypatch.setenv("PLANE_SOCKET", str(tmp_path / "unused.sock"))
    for name in ("audit", "audit-work", "other-name"):
        fragment = {
            name: {"command": "fixture-command", "args": ["--fixture"],
                   "env": {"TOKEN": "${TOKEN}", "SHARED": "${SHARED}"}},
            "_env_contract": {"TOKEN": {"scope": "instance", "secret": True},
                              "SHARED": {"scope": "shared", "secret": False}},
            "_permissions_contract": {"tools": ["read", "write"], "read_only_tools": ["read"]},
        }
        (fleet_dir / "library/mcp" / f"{name}.json").write_text(json.dumps(fragment))
        (fleet_dir / "library/integrations" / f"{name}.md").write_text(
            f"---\ntitle: Fixture\ntool_grants: [mcp__{name}__read]\n---\nFixture.\n")
    fleet = load_test_fleet(fleet_dir)
    for bot in fleet.bots.values():
        bot.telegram.handle = ""
    bot = fleet.bots["lead"]
    paths = Paths(root=fleet_dir, fleet_dir=fleet_dir)
    return fleet, bot, paths


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("door", ["render", "permissions", "integrations", "validate"])
def test_collision_names_both_sources_in_each_consumer(scene, reverse, door):
    fleet, bot, paths = scene
    bot.mcp = [McpEntry("audit", ["work"]), McpEntry("audit-work")]
    if reverse:
        bot.mcp.reverse()
    if door == "validate":
        errors = validator.validate(fleet, paths).errors
        collisions = [error for error in errors if "collision" in error]
        assert len(collisions) == 1, errors
        message = collisions[0]
    else:
        function = {"render": composer.compose_mcp_json,
                    "permissions": composer._resolve_mcp_permissions,
                    "integrations": composer._resolve_integration_grants}[door]
        with pytest.raises(ValueError) as caught:
            function(bot, paths)
        message = str(caught.value)
    assert "audit-work" in message and "audit" in message
    assert "work" in message and "default" in message


@pytest.mark.parametrize("existing", [False, True])
def test_compose_rejects_before_creating_or_touching_runtime(scene, existing):
    fleet, bot, paths = scene
    bot.mcp = [McpEntry("audit", ["work"]), McpEntry("audit-work")]
    runtime = paths.bot_runtime(bot.bot_id)
    if existing:
        (runtime / ".claude").mkdir(parents=True)
        (runtime / "CLAUDE.md").write_text("keep existing instructions")
        (runtime / ".claude/settings.local.json").write_text('{"keep":"permissions"}')
        (runtime / ".mcp.json").write_text('{"keep":"wiring"}')
    def snapshot():
        return {str(path.relative_to(runtime)): path.read_bytes()
                for path in runtime.rglob("*") if path.is_file()} if runtime.exists() else None
    before = snapshot()
    with pytest.raises(ValueError, match="collision"):
        composer.compose_bot(bot, fleet, paths, boot_delay_s=0, cascade={})
    assert snapshot() == before


def test_inherited_collision_is_reported_after_loader_merge(scene):
    fleet, bot, paths = scene
    source = paths.root / "fleet.yaml"
    raw = yaml.safe_load(source.read_text())
    raw["fleet"]["defaults"]["mcp"] = [{"audit": {"instances": ["work"]}}]
    raw["fleet"]["bots"]["lead"]["mcp"] = ["audit-work"]
    source.write_text(yaml.safe_dump(raw))
    merged, _ = load_fleet(source)
    report = validator.validate(merged, paths)
    assert any("lead" in error and "collision" in error for error in report.errors)


def test_repeated_instance_is_refused(scene):
    _, bot, paths = scene
    bot.mcp = [McpEntry("audit", ["work", "work"])]
    with pytest.raises(ValueError, match="collision"):
        composer.compose_mcp_json(bot, paths)


def test_validator_collects_collision_and_independent_errors_for_multiple_bots(scene):
    fleet, _, paths = scene
    for bot in fleet.bots.values():
        bot.mcp = [McpEntry("audit", ["work", "work"])]
        bot.expertise = []
    report = validator.validate(fleet, paths)
    for name in fleet.bots:
        assert sum(name in error and "collision" in error for error in report.errors) == 1
        assert any(name in error and "expertise list is empty" in error for error in report.errors)


def test_noncolliding_names_env_grants_and_trust_are_unchanged(scene):
    fleet, bot, paths = scene
    bot.mcp = [McpEntry("audit", ["default", "work"]), McpEntry("other-name", ["prod-east"])]
    expected = {
        "audit": "AUDIT_TOKEN", "audit-work": "AUDIT_WORK_TOKEN",
        "other-name-prod-east": "OTHER_NAME_PROD_EAST_TOKEN",
    }
    rendered = composer.compose_mcp_json(bot, paths)
    assert list(rendered["mcpServers"]) == list(expected)
    for name, token in expected.items():
        assert rendered["mcpServers"][name] == {
            "command": "fixture-command", "args": ["--fixture"],
            "env": {"TOKEN": "${" + token + "}", "SHARED": "${SHARED}"},
        }
    grants = [f"mcp__{name}__read" for name in expected]
    assert composer._resolve_mcp_permissions(bot, paths) == grants
    assert composer._resolve_integration_grants(bot, paths) == grants
    settings = composer.compose_settings_local(bot, fleet, paths, list(expected))
    assert settings["enabledMcpjsonServers"] == sorted(expected)
    assert all(grant in settings["permissions"]["allow"] for grant in grants)
    assert not any("__write" in grant for grant in settings["permissions"]["allow"])


def test_loader_first_seen_fragment_and_explicit_empty_instances_stay_unchanged(scene):
    _, _, paths = scene
    source = paths.root / "fleet.yaml"
    raw = yaml.safe_load(source.read_text())
    raw["fleet"]["bots"]["lead"]["mcp"] = [
        {"audit": {"instances": []}}, {"audit": {"instances": ["work"]}},
        "other-name", "other-name",
    ]
    source.write_text(yaml.safe_dump(raw))
    fleet, _ = load_fleet(source)
    bot = fleet.bots["lead"]
    assert [(entry.name, entry.instances) for entry in bot.mcp] == [("audit", []), ("other-name", ["default"])]
    assert list(composer.compose_mcp_json(bot, paths)["mcpServers"]) == ["other-name"]
    assert composer._resolve_integration_grants(bot, paths) == ["mcp__other-name__read"]


def test_explicit_empty_instances_still_validate_fragment_syntax(scene):
    _, bot, paths = scene
    bot.mcp = [McpEntry("audit", [])]
    (paths.root / "library/mcp/audit.json").write_text("{malformed")
    with pytest.raises(ValueError, match="invalid JSON"):
        composer.compose_mcp_json(bot, paths)


def test_direct_duplicate_fragment_uses_first_declaration_consistently(scene):
    fleet, bot, paths = scene
    bot.mcp = [McpEntry("audit", ["work"]), McpEntry("audit", ["default"])]
    assert list(composer.compose_mcp_json(bot, paths)["mcpServers"]) == ["audit-work"]
    assert composer._resolve_mcp_permissions(bot, paths) == ["mcp__audit-work__read"]
    settings = composer.compose_settings_local(bot, fleet, paths, ["audit-work"])
    assert settings["permissions"]["allow"].count("mcp__audit-work__read") == 1
    assert "mcp__audit__read" not in settings["permissions"]["allow"]
