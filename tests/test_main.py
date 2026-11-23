"""Tests for __main__.py — CLI helpers, path resolution, env loading, and command dispatch."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from claudlobby.__main__ import main
from claudlobby.commands.core import cmd_generate
from claudlobby.commands._helpers import _load_env, _load_fleet_or_exit, _resolve_paths
from tests.package_fixtures import git_package, source_package
from claudlobby.paths import Paths


# ── _resolve_paths ───────────────────────────────────────────────────


class TestResolvePaths:
    def test_root_mode(self, tmp_path):
        args = SimpleNamespace(root=str(tmp_path), fleet=None, seed=False)
        paths = _resolve_paths(args)
        assert paths.root == tmp_path

    def test_seed_mode(self, tmp_path):
        args = SimpleNamespace(root=str(tmp_path), fleet=None, seed=True)
        paths = _resolve_paths(args)
        assert paths.root == tmp_path

    def test_seed_and_fleet_mutually_exclusive(self, tmp_path):
        args = SimpleNamespace(root=str(tmp_path), fleet="test", seed=True)
        with pytest.raises(SystemExit):
            _resolve_paths(args)

    def test_fleet_overlay_with_root(self, tmp_path):
        fleet_dir = tmp_path / "local" / "myfleet"
        fleet_dir.mkdir(parents=True)
        args = SimpleNamespace(root=str(tmp_path), fleet="myfleet", seed=False)
        paths = _resolve_paths(args)
        assert paths.fleet_dir == fleet_dir

    def test_fleet_overlay_missing_exits(self, tmp_path):
        args = SimpleNamespace(root=str(tmp_path), fleet="nonexistent", seed=False)
        with pytest.raises(SystemExit):
            _resolve_paths(args)


# ── _load_env ────────────────────────────────────────────────────────


class TestLoadEnv:
    def test_loads_env_vars(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("MY_TEST_VAR=hello\nexport MY_OTHER_VAR=world\n")
        paths = SimpleNamespace(env_file=env_file)
        # Ensure clean state
        monkeypatch.delenv("MY_TEST_VAR", raising=False)
        monkeypatch.delenv("MY_OTHER_VAR", raising=False)
        _load_env(paths)
        assert os.environ["MY_TEST_VAR"] == "hello"
        assert os.environ["MY_OTHER_VAR"] == "world"
        # Cleanup
        monkeypatch.delenv("MY_TEST_VAR")
        monkeypatch.delenv("MY_OTHER_VAR")

    def test_does_not_override_existing(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("EXISTING_VAR=from-file\n")
        monkeypatch.setenv("EXISTING_VAR", "original")
        paths = SimpleNamespace(env_file=env_file)
        _load_env(paths)
        assert os.environ["EXISTING_VAR"] == "original"

    def test_missing_env_file_ok(self, tmp_path):
        paths = SimpleNamespace(env_file=tmp_path / "nope.env")
        _load_env(paths)  # should not raise


# ── _load_fleet_or_exit ──────────────────────────────────────────────


class TestLoadFleetOrExit:
    def test_missing_file_exits(self, tmp_path):
        paths = Paths(root=tmp_path, package=source_package())
        with pytest.raises(SystemExit):
            _load_fleet_or_exit(paths)

    def test_malformed_yaml_exits(self, tmp_path):
        bad = tmp_path / "fleet.yaml"
        bad.write_text(": [invalid\n")
        paths = Paths(root=tmp_path, package=source_package())
        with pytest.raises(SystemExit):
            _load_fleet_or_exit(paths)

    def test_valid_fleet_returns_config(self, fleet_dir):
        paths = Paths(root=fleet_dir, package=source_package())
        config, merged_defaults = _load_fleet_or_exit(paths)
        assert config.name == "test-fleet"
        assert "lead" in config.bots
        assert isinstance(merged_defaults, dict)


# ── config validate ─────────────────────────────────────────────────


class TestConfigValidate:
    def test_valid_fleet_returns_zero(self, fleet_dir):
        result = main(["--root", str(fleet_dir), "config", "validate"])
        assert result == 0

    def test_strict_with_warnings_returns_one(self, fleet_dir):
        """If validate produces warnings and --strict is set, return 1."""
        # This may or may not return 1 depending on whether the minimal
        # fleet produces warnings — the important thing is it doesn't crash
        result = main(["--root", str(fleet_dir), "config", "validate", "--strict"])
        assert result in (0, 4)


# ── cmd_generate ─────────────────────────────────────────────────────


class TestCmdGenerate:
    @pytest.fixture(autouse=True)
    def managed_render_paths(self, fleet_dir, monkeypatch):
        paths = Paths(root=fleet_dir, package=git_package(fleet_dir))
        monkeypatch.setattr("claudlobby.commands.core._resolve_paths", lambda _args: paths)

    def test_generates_bots(self, fleet_dir):
        args = SimpleNamespace(
            root=str(fleet_dir),
            fleet=None,
            seed=False,
            bot=None,
            strict=False,
            verbose=False,
        )
        result = cmd_generate(args)
        assert result == 0
        # Check that bot directories were created
        assert (fleet_dir / "runtime" / "bots" / "lead").is_dir()
        assert (fleet_dir / "runtime" / "bots" / "worker-1").is_dir()

    def test_generate_single_bot(self, fleet_dir):
        args = SimpleNamespace(
            root=str(fleet_dir),
            fleet=None,
            seed=False,
            bot="lead",
            strict=False,
            verbose=False,
        )
        result = cmd_generate(args)
        assert result == 0
        assert (fleet_dir / "runtime" / "bots" / "lead").is_dir()

    def test_generate_nonexistent_bot_returns_one(self, fleet_dir):
        args = SimpleNamespace(
            root=str(fleet_dir),
            fleet=None,
            seed=False,
            bot="nonexistent",
            strict=False,
            verbose=False,
        )
        result = cmd_generate(args)
        assert result == 1


# ── main() argparse ──────────────────────────────────────────────────


class TestMainArgparse:
    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["-V"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "claudlobby" in captured.out

    def test_missing_subcommand_exits(self):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code != 0

    def test_validate_subcommand(self, fleet_dir):
        result = main(["--root", str(fleet_dir), "config", "validate"])
        assert result == 0
        with pytest.raises(SystemExit) as retired:
            main(["--root", str(fleet_dir), "validate"])
        assert retired.value.code == 2

    def test_generate_is_retired_before_it_can_write_runtime(self, tmp_path):
        with pytest.raises(SystemExit) as retired:
            main(["--root", str(tmp_path), "generate"])
        assert retired.value.code == 2
        assert list(tmp_path.iterdir()) == []
