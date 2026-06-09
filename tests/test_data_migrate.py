"""Tests for data-migrate command — top-level file handling (#23)."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch


from claudlobby.config import BotConfig, FleetConfig
from claudlobby.commands.data_migrate import cmd_data_migrate
from claudlobby.paths import Paths


def _make_fleet(tmp_path: Path, bot_names: list[str]) -> tuple[FleetConfig, Paths]:
    """Scaffold a minimal fleet with runtime dirs under tmp_path."""
    bots = {}
    for name in bot_names:
        bots[name] = BotConfig(
            bot_id=name, name=name, expertise=["software-engineering"]
        )
    fleet = FleetConfig(name="test-fleet", service_prefix="com.test", bots=bots)

    # Create runtime bot dirs (data-migrate expects these to exist)
    for name in bot_names:
        (tmp_path / "runtime" / "bots" / name / "data").mkdir(parents=True)

    # Minimal library + template dirs so Paths doesn't complain
    (tmp_path / "library").mkdir(exist_ok=True)
    (tmp_path / "templates").mkdir(exist_ok=True)
    (tmp_path / "lib").mkdir(exist_ok=True)

    paths = Paths(root=tmp_path)
    return fleet, paths


def _make_source(
    tmp_path: Path, bot_name: str, dirs: list[str], files: dict[str, str]
) -> Path:
    """Create a source bot directory with given subdirs and top-level files."""
    source = tmp_path / "legacy"
    bot_dir = source / bot_name
    bot_dir.mkdir(parents=True)
    for d in dirs:
        (bot_dir / d).mkdir()
        # Put a file in each dir so it's not empty
        (bot_dir / d / "placeholder.txt").write_text("data")
    for name, content in files.items():
        (bot_dir / name).write_text(content)
    return source


def _make_args(
    source: Path,
    apply: bool = False,
    include: str | None = None,
    exclude: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        source=str(source),
        map=[],
        apply=apply,
        include=include,
        exclude=exclude,
        root=None,
        fleet=None,
        seed=False,
    )


class TestDataMigrateTopLevelFiles:
    """Verify that top-level files are included in the migration plan and copied."""

    def test_files_appear_in_plan(self, tmp_path):
        fleet, paths = _make_fleet(tmp_path, ["bot1"])
        source = _make_source(
            tmp_path,
            "bot1",
            dirs=["scripts"],
            files={"config.yaml": "key: value", "notes.txt": "some notes"},
        )
        args = _make_args(source)
        with (
            patch("claudlobby.commands._helpers._resolve_paths", return_value=paths),
            patch(
                "claudlobby.commands._helpers._load_fleet_or_exit", return_value=fleet
            ),
        ):
            rc = cmd_data_migrate(args)
        assert rc == 0

    def test_files_are_copied_on_apply(self, tmp_path):
        fleet, paths = _make_fleet(tmp_path, ["bot1"])
        source = _make_source(
            tmp_path,
            "bot1",
            dirs=["scripts"],
            files={"config.yaml": "key: value", "notes.txt": "some notes"},
        )
        args = _make_args(source, apply=True)
        with (
            patch("claudlobby.commands._helpers._resolve_paths", return_value=paths),
            patch(
                "claudlobby.commands._helpers._load_fleet_or_exit", return_value=fleet
            ),
        ):
            rc = cmd_data_migrate(args)
        assert rc == 0

        data_dir = tmp_path / "runtime" / "bots" / "bot1" / "data"
        assert (data_dir / "config.yaml").exists()
        assert (data_dir / "config.yaml").read_text() == "key: value"
        assert (data_dir / "notes.txt").exists()
        assert (data_dir / "notes.txt").read_text() == "some notes"
        # Dir should also be copied
        assert (data_dir / "scripts").is_dir()

    def test_dotfiles_skipped_by_default(self, tmp_path):
        fleet, paths = _make_fleet(tmp_path, ["bot1"])
        source = _make_source(
            tmp_path,
            "bot1",
            dirs=[],
            files={".hidden": "secret", "visible.txt": "ok"},
        )
        args = _make_args(source, apply=True)
        with (
            patch("claudlobby.commands._helpers._resolve_paths", return_value=paths),
            patch(
                "claudlobby.commands._helpers._load_fleet_or_exit", return_value=fleet
            ),
        ):
            cmd_data_migrate(args)

        data_dir = tmp_path / "runtime" / "bots" / "bot1" / "data"
        assert not (data_dir / ".hidden").exists()
        assert (data_dir / "visible.txt").exists()

    def test_empty_files_skipped(self, tmp_path):
        fleet, paths = _make_fleet(tmp_path, ["bot1"])
        source = _make_source(
            tmp_path,
            "bot1",
            dirs=[],
            files={"empty.txt": "", "nonempty.txt": "data"},
        )
        args = _make_args(source, apply=True)
        with (
            patch("claudlobby.commands._helpers._resolve_paths", return_value=paths),
            patch(
                "claudlobby.commands._helpers._load_fleet_or_exit", return_value=fleet
            ),
        ):
            cmd_data_migrate(args)

        data_dir = tmp_path / "runtime" / "bots" / "bot1" / "data"
        assert not (data_dir / "empty.txt").exists()
        assert (data_dir / "nonempty.txt").exists()

    def test_existing_destination_file_skipped(self, tmp_path):
        fleet, paths = _make_fleet(tmp_path, ["bot1"])
        source = _make_source(
            tmp_path,
            "bot1",
            dirs=[],
            files={"config.yaml": "new content"},
        )
        # Pre-create destination file
        data_dir = tmp_path / "runtime" / "bots" / "bot1" / "data"
        (data_dir / "config.yaml").write_text("old content")

        args = _make_args(source, apply=True)
        with (
            patch("claudlobby.commands._helpers._resolve_paths", return_value=paths),
            patch(
                "claudlobby.commands._helpers._load_fleet_or_exit", return_value=fleet
            ),
        ):
            cmd_data_migrate(args)

        # Should NOT be overwritten
        assert (data_dir / "config.yaml").read_text() == "old content"

    def test_exclude_filter_applies_to_files(self, tmp_path):
        fleet, paths = _make_fleet(tmp_path, ["bot1"])
        source = _make_source(
            tmp_path,
            "bot1",
            dirs=[],
            files={"keep.txt": "yes", "drop.txt": "no"},
        )
        args = _make_args(source, apply=True, exclude="drop.txt")
        with (
            patch("claudlobby.commands._helpers._resolve_paths", return_value=paths),
            patch(
                "claudlobby.commands._helpers._load_fleet_or_exit", return_value=fleet
            ),
        ):
            cmd_data_migrate(args)

        data_dir = tmp_path / "runtime" / "bots" / "bot1" / "data"
        assert (data_dir / "keep.txt").exists()
        assert not (data_dir / "drop.txt").exists()

    def test_include_filter_applies_to_files(self, tmp_path):
        fleet, paths = _make_fleet(tmp_path, ["bot1"])
        source = _make_source(
            tmp_path,
            "bot1",
            dirs=[],
            files={"wanted.txt": "yes", "unwanted.txt": "no"},
        )
        args = _make_args(source, apply=True, include="wanted.txt")
        with (
            patch("claudlobby.commands._helpers._resolve_paths", return_value=paths),
            patch(
                "claudlobby.commands._helpers._load_fleet_or_exit", return_value=fleet
            ),
        ):
            cmd_data_migrate(args)

        data_dir = tmp_path / "runtime" / "bots" / "bot1" / "data"
        assert (data_dir / "wanted.txt").exists()
        assert not (data_dir / "unwanted.txt").exists()
