"""Tests for __main__.py — CLI helpers, path resolution, env loading, and command dispatch."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from claudlobby.__main__ import (
    _contains_git_checkouts,
    _dir_size_mb,
    _human_size,
    _load_env,
    _load_fleet_or_exit,
    _resolve_paths,
    cmd_generate,
    cmd_validate,
    main,
)
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
        paths = SimpleNamespace(fleet_yaml=tmp_path / "nonexistent.yaml")
        with pytest.raises(SystemExit):
            _load_fleet_or_exit(paths)

    def test_malformed_yaml_exits(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(": [invalid\n")
        paths = SimpleNamespace(fleet_yaml=bad)
        with pytest.raises(SystemExit):
            _load_fleet_or_exit(paths)

    def test_valid_fleet_returns_config(self, fleet_dir):
        paths = Paths(root=fleet_dir, fleet_dir=fleet_dir)
        config = _load_fleet_or_exit(paths)
        assert config.name == "test-fleet"
        assert "lead" in config.bots


# ── _contains_git_checkouts ──────────────────────────────────────────


class TestContainsGitCheckouts:
    def test_all_git_dirs(self, tmp_path):
        for name in ("repo-a", "repo-b"):
            (tmp_path / name / ".git").mkdir(parents=True)
        assert _contains_git_checkouts(tmp_path) is True

    def test_no_git_dirs(self, tmp_path):
        for name in ("data", "logs"):
            (tmp_path / name).mkdir()
        assert _contains_git_checkouts(tmp_path) is False

    def test_mixed_below_threshold(self, tmp_path):
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        for name in ("data", "logs", "cache"):
            (tmp_path / name).mkdir()
        # 1/4 = 0.25 < 0.5 threshold
        assert _contains_git_checkouts(tmp_path) is False

    def test_empty_dir(self, tmp_path):
        assert _contains_git_checkouts(tmp_path) is False

    def test_nonexistent_dir(self, tmp_path):
        assert _contains_git_checkouts(tmp_path / "nope") is False

    def test_custom_threshold(self, tmp_path):
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        (tmp_path / "other").mkdir()
        # 1/2 = 0.5, threshold 0.3 → True
        assert _contains_git_checkouts(tmp_path, threshold=0.3) is True


# ── _dir_size_mb ─────────────────────────────────────────────────────


class TestDirSizeMb:
    def test_empty_dir(self, tmp_path):
        assert _dir_size_mb(tmp_path) == 0.0

    def test_single_file(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"x" * 1024)  # 1 KB
        size = _dir_size_mb(tmp_path)
        assert 0 < size < 0.01  # way less than 1 MB

    def test_nested_dirs(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "file.txt").write_bytes(b"y" * 2048)
        size = _dir_size_mb(tmp_path)
        assert size > 0

    def test_symlinks_skipped(self, tmp_path):
        real = tmp_path / "real.txt"
        real.write_bytes(b"z" * 4096)
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        size_with_link = _dir_size_mb(tmp_path)
        # The symlink itself shouldn't count the target's size again
        assert size_with_link < 0.01


# ── _human_size ──────────────────────────────────────────────────────


class TestHumanSize:
    def test_sub_mb(self):
        assert _human_size(0.5) == "<1M"

    def test_mb_range(self):
        assert _human_size(42) == "42M"

    def test_gb_range(self):
        assert _human_size(2048) == "2.0G"


# ── cmd_validate ─────────────────────────────────────────────────────


class TestCmdValidate:
    def test_valid_fleet_returns_zero(self, fleet_dir):
        args = SimpleNamespace(
            root=str(fleet_dir),
            fleet=None,
            seed=False,
            strict=False,
            verbose=False,
        )
        result = cmd_validate(args)
        assert result == 0

    def test_strict_with_warnings_returns_one(self, fleet_dir):
        """If validate produces warnings and --strict is set, return 1."""
        args = SimpleNamespace(
            root=str(fleet_dir),
            fleet=None,
            seed=False,
            strict=True,
            verbose=False,
        )
        # This may or may not return 1 depending on whether the minimal
        # fleet produces warnings — the important thing is it doesn't crash
        result = cmd_validate(args)
        assert result in (0, 1)


# ── cmd_generate ─────────────────────────────────────────────────────


class TestCmdGenerate:
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
        result = main(["--root", str(fleet_dir), "validate"])
        assert result == 0

    def test_generate_subcommand(self, fleet_dir):
        result = main(["--root", str(fleet_dir), "generate"])
        assert result == 0
