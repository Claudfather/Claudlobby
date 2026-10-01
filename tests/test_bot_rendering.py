"""Bot staging renders real destination bytes without changing runtime state."""

import copy
import json
from pathlib import Path

import pytest

from claudlobby.composer import (
    GH_APP_IDENTITY_FILENAME,
    GITCONFIG_FILENAME,
    compose_access_json,
    compose_bot,
    link_mounts,
    reconcile_access_content,
    render_bot_files,
    resolve_mount_sources,
    telegram_channel_rel,
)
from claudlobby.config import (
    BotConfig, FleetConfig, SystemDefaultsConfig, TelegramConfig, ToolEntry,
    ToolPermissionsConfig,
)
from claudlobby.paths import Paths
from tests.package_fixtures import source_package


def _tree(root):
    return {
        str(path.relative_to(root)): (
            ("link", str(path.readlink())) if path.is_symlink() else
            ("dir",) if path.is_dir() else ("file", path.read_bytes(), path.stat().st_mode)
        ) for path in root.rglob("*")
    }


@pytest.fixture
def render_case(tmp_path):
    root = tmp_path / "data"
    tool = root / "library/tools/render-tool"
    tool.mkdir(parents=True)
    (tool / "tool.yaml").write_text("type: script\n")
    (tool / "render-tool.sh.j2").write_text("#!/bin/sh\necho '{{ bot_dir }}/data'\n")
    skill = root / "library/skills/render-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: render-skill\n---\n# Render skill\n")
    bot = BotConfig(
        bot_id="worker", name="worker", expertise=["software-engineering"],
        channels=[], skills=["render-skill"], tools=[ToolEntry("render-tool")],
        telegram=TelegramConfig(handle="render_worker", chat_id="-10042"),
        tool_permissions=ToolPermissionsConfig(deny=["Write"], allow=["Bash(echo *)"]),
    )
    fleet = FleetConfig(
        name="render-fleet", service_prefix="com.render", manager="manager",
        bots={"worker": bot, "manager": BotConfig("manager", "manager", [])},
        system_defaults=SystemDefaultsConfig(enabled=False), human_telegram_id="human",
    )
    return bot, fleet, Paths(root=root, package=source_package())


def test_render_preserves_state_and_compose_writes_the_same_artifacts(render_case):
    bot, fleet, paths = render_case
    directory = paths.bot_runtime(bot.bot_id)
    skills = directory / ".claude/skills"
    skills.mkdir(parents=True)
    (skills / "old").symlink_to(paths.root / "library/skills/render-skill")
    (directory / ".claude/settings.local.json").write_text('{"permissions":{"allow":["old"]}}')
    (directory / GITCONFIG_FILENAME).write_text("stale git routing")
    (directory / GH_APP_IDENTITY_FILENAME).write_text("stale App identity")
    (directory / "tools").mkdir()
    (directory / "tools/stale.sh").write_text("stale tool")
    access_path = Path.home() / telegram_channel_rel(bot.telegram.handle) / "access.json"
    access_path.parent.mkdir(parents=True)
    existing = {"dmPolicy": "open", "allowFrom": ["runtime-user"],
                "groups": {"-10042": {"requireMention": False, "allowFrom": ["peer"]}},
                "pending": {"request": {"user": "runtime-user"}}}
    access_path.write_text(json.dumps(existing))
    before = (_tree(paths.root), _tree(Path.home()), copy.deepcopy(fleet))

    rendered = render_bot_files(bot, fleet, paths, boot_delay_s=0, cascade={})
    assert render_bot_files(bot, fleet, paths, boot_delay_s=0, cascade={}) == rendered
    assert (_tree(paths.root), _tree(Path.home()), fleet) == before
    assert str(directory / "data") in rendered["tools/render-tool.sh"].content
    assert rendered["tools/render-tool.sh"].mode == 0o755
    assert rendered[GITCONFIG_FILENAME] is rendered[GH_APP_IDENTITY_FILENAME] is None

    fresh = compose_access_json(bot, fleet)
    input_snapshot = copy.deepcopy((existing, fresh))
    expected_access = reconcile_access_content(existing, fresh, bot, fleet)
    assert (existing, fresh) == input_snapshot
    assert expected_access["pending"] == existing["pending"]
    assert expected_access["groups"]["-10042"]["requireMention"] is True

    assert compose_bot(bot, fleet, paths, boot_delay_s=0, cascade={}) == directory
    for name, artifact in rendered.items():
        target = directory / name
        if artifact is None:
            assert not target.exists()
        else:
            assert target.read_text() == artifact.content
            assert target.stat().st_mode & 0o777 == artifact.mode
    assert not (directory / "tools/stale.sh").exists()
    assert not (skills / "old").exists()
    assert (skills / "render-skill").resolve() == paths.root / "library/skills/render-skill"
    assert json.loads(access_path.read_text()) == expected_access
    assert fleet == before[2]


def test_render_rejects_bad_tool_wiring_without_output(render_case):
    bot, fleet, paths = render_case
    template = paths.root / "library/tools/render-tool/render-tool.sh.j2"
    template.write_text("#!/bin/sh\necho /foreign/runtime/bots/worker/data\n")
    before = _tree(paths.root)
    with pytest.raises(ValueError, match="improper absolute fleet path"):
        render_bot_files(bot, fleet, paths, boot_delay_s=0, cascade={})
    assert _tree(paths.root) == before
    assert not paths.bot_runtime(bot.bot_id).exists()


def test_mount_resolution_is_read_only_and_linker_uses_its_skip_rules(tmp_path):
    directory = tmp_path / "bot"
    mounts = directory / "mounts"
    (mounts / "occupied").mkdir(parents=True)
    target = Path.home() / "mount-target"
    target.mkdir()
    missing = Path.home() / "missing-target"
    (mounts / "stale").symlink_to(target)
    bot = BotConfig("worker", "worker", [], mounts={
        "allowed": str(target), "missing": str(missing),
        "occupied": str(target), "escape": str(tmp_path / "outside"),
    })
    before, messages = _tree(directory), []
    resolved = resolve_mount_sources(bot, directory, messages.append)
    assert resolved == {"allowed": target, "missing": missing}
    assert _tree(directory) == before
    assert any("escapes home" in message for message in messages)
    assert any("non-symlink" in message for message in messages)
    link_mounts(bot, directory, messages.append)
    assert all((mounts / name).readlink() == path for name, path in resolved.items())
    assert (mounts / "occupied").is_dir()
    assert not (mounts / "escape").exists()
    assert not (mounts / "stale").exists()
