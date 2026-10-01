"""The memory converter previews without creating destination state."""

from argparse import Namespace

from claudlobby.commands import memory_migrate
from claudlobby.config import BotConfig, FleetConfig
from claudlobby.paths import Paths
from tests.package_fixtures import source_package


def test_memory_preview_then_explicit_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / ".claude" / "projects" / "project-bot" / "memory"
    source.mkdir(parents=True)
    (source / "MEMORY.md").write_text("remember this")
    fleet = FleetConfig(name="test", service_prefix="com.test", manager="bot",
                        bots={"bot": BotConfig(bot_id="bot", name="bot", expertise=["software-engineering"])})
    paths = Paths(root=tmp_path / "root", package=source_package())
    monkeypatch.setattr(memory_migrate, "_resolve_paths", lambda args: paths)
    monkeypatch.setattr(memory_migrate, "_load_fleet_or_exit", lambda selected: (fleet, {}))
    args = Namespace(map=None, force=False, apply=False)
    destination = paths.bot_runtime("bot") / "memory" / "MEMORY.md"

    assert memory_migrate.cmd_memory_migrate(args) == 0
    assert not destination.exists()
    args.apply = True
    assert memory_migrate.cmd_memory_migrate(args) == 0
    assert destination.read_text() == "remember this"
