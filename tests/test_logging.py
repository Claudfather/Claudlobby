"""Tests for structured logging throughout the claudlobby compositor.

Verifies that print() calls have been replaced with logging calls,
that the --verbose flag enables DEBUG output, and that commands emit
the right log levels for success, warning, and error conditions.
"""

from __future__ import annotations

import logging
import types
from pathlib import Path
from unittest.mock import patch  # noqa: F401 — used in generate tests

from claudlobby.__main__ import (
    cmd_generate,
    cmd_list_library,
    cmd_memory_migrate,
    cmd_validate,
)


def _args(**kwargs):
    """Build a minimal args namespace for direct command calls."""
    defaults = dict(root=None, fleet=None, strict=False, bot=None, verbose=False)
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


# ── Logging configuration ─────────────────────────────────────────────────────


class TestLoggingConfiguration:
    def test_log_format_includes_timestamp(self, fleet_dir, monkeypatch, caplog):
        """basicConfig format string has %(asctime)s."""
        # Inspect the handler format registered by main()
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        # main() internally calls logging.basicConfig — verify the format
        # by checking the formatter on any root handler that ends up attached.
        import claudlobby.__main__ as m
        import inspect

        src = inspect.getsource(m.main)
        assert "%(asctime)s" in src
        assert "%(levelname)" in src
        assert "%(message)s" in src

    def test_verbose_flag_arg_sets_debug_level(self, fleet_dir, monkeypatch, caplog):
        """--verbose in main() maps to logging.DEBUG."""
        import claudlobby.__main__ as m
        import inspect

        src = inspect.getsource(m.main)
        assert "logging.DEBUG if args.verbose else logging.INFO" in src

    def test_claudlobby_logger_name(self):
        """The top-level logger is named 'claudlobby'."""
        from claudlobby.__main__ import log

        assert log.name == "claudlobby"

    def test_newbot_logger_uses_module_name(self):
        """newbot.py uses __name__ logger (claudlobby.newbot)."""
        from claudlobby.newbot import log as newbot_log

        assert newbot_log.name == "claudlobby.newbot"


# ── cmd_validate ──────────────────────────────────────────────────────────────


class TestValidateCommandLogging:
    def test_validate_logs_ok_on_clean_fleet(self, fleet_dir, monkeypatch, caplog):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        args = _args(root=str(fleet_dir))
        with caplog.at_level(logging.INFO, logger="claudlobby"):
            result = cmd_validate(args)
        assert result == 0
        # Something was logged (warnings or OK); no errors means clean pass
        assert caplog.records

    def test_validate_logs_errors_for_missing_expertise(self, fleet_dir, caplog):
        text = (fleet_dir / "fleet.yaml").read_text()
        (fleet_dir / "fleet.yaml").write_text(
            text.replace("orchestration", "no-such-expertise")
        )
        args = _args(root=str(fleet_dir))
        with caplog.at_level(logging.ERROR, logger="claudlobby"):
            result = cmd_validate(args)
        assert result == 1
        assert "no-such-expertise" in caplog.text

    def test_validate_logs_warnings_for_missing_env(
        self, fleet_dir, monkeypatch, caplog
    ):
        monkeypatch.delenv("TELEGRAM_TOKEN_LEAD", raising=False)
        monkeypatch.delenv("TELEGRAM_TOKEN_WORKER1", raising=False)
        args = _args(root=str(fleet_dir))
        with caplog.at_level(logging.WARNING, logger="claudlobby"):
            cmd_validate(args)
        assert "TELEGRAM_TOKEN_LEAD" in caplog.text

    def test_validate_strict_logs_error_on_warnings(
        self, fleet_dir, monkeypatch, caplog
    ):
        monkeypatch.delenv("TELEGRAM_TOKEN_LEAD", raising=False)
        monkeypatch.delenv("TELEGRAM_TOKEN_WORKER1", raising=False)
        args = _args(root=str(fleet_dir), strict=True)
        with caplog.at_level(logging.ERROR, logger="claudlobby"):
            result = cmd_validate(args)
        assert result == 1
        assert "strict" in caplog.text.lower()


# ── cmd_generate ──────────────────────────────────────────────────────────────


class TestGenerateCommandLogging:
    def test_generate_logs_composed_message(self, fleet_dir, monkeypatch, caplog):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        args = _args(root=str(fleet_dir))
        fake_out = {"lead": fleet_dir / "runtime/bots/lead"}
        with (
            caplog.at_level(logging.INFO, logger="claudlobby"),
            patch("claudlobby.__main__.compose_fleet", return_value=fake_out),
        ):
            result = cmd_generate(args)
        assert result == 0
        assert "composed" in caplog.text.lower()

    def test_generate_logs_error_on_invalid_fleet(self, fleet_dir, caplog):
        text = (fleet_dir / "fleet.yaml").read_text()
        (fleet_dir / "fleet.yaml").write_text(
            text.replace("orchestration", "nonexistent-expertise-xyz")
        )
        args = _args(root=str(fleet_dir))
        with caplog.at_level(logging.ERROR, logger="claudlobby"):
            result = cmd_generate(args)
        assert result == 1
        # Should log validation errors and refuse
        assert "error" in caplog.text.lower() or "nonexistent" in caplog.text

    def test_generate_single_bot_logs_composed(self, fleet_dir, monkeypatch, caplog):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        args = _args(root=str(fleet_dir), bot="lead")
        with (
            caplog.at_level(logging.INFO, logger="claudlobby"),
            patch(
                "claudlobby.__main__.compose_bot",
                return_value=fleet_dir / "runtime/bots/lead",
            ),
        ):
            result = cmd_generate(args)
        assert result == 0
        assert "lead" in caplog.text


# ── cmd_list_library ──────────────────────────────────────────────────────────


class TestListLibraryLogging:
    def test_list_library_logs_expertise_header(self, fleet_dir, caplog):
        args = _args(root=str(fleet_dir))
        with caplog.at_level(logging.INFO, logger="claudlobby"):
            result = cmd_list_library(args)
        assert result == 0
        assert "Expertise" in caplog.text

    def test_list_library_logs_skills_header(self, fleet_dir, caplog):
        args = _args(root=str(fleet_dir))
        with caplog.at_level(logging.INFO, logger="claudlobby"):
            cmd_list_library(args)
        assert "Skills" in caplog.text

    def test_list_library_logs_root_mode_message(self, fleet_dir, caplog):
        """No fleet overlay → logs root-mode message."""
        args = _args(root=str(fleet_dir))
        with caplog.at_level(logging.INFO, logger="claudlobby"):
            cmd_list_library(args)
        assert "root mode" in caplog.text or "no fleet overlay" in caplog.text.lower()

    def test_list_library_logs_expertise_names(self, fleet_dir, caplog):
        """Expertise entries are logged as INFO records."""
        args = _args(root=str(fleet_dir))
        with caplog.at_level(logging.INFO, logger="claudlobby"):
            cmd_list_library(args)
        # orchestration.md and software-engineering.md are in the fixture
        assert "orchestration" in caplog.text


# ── cmd_memory_migrate ────────────────────────────────────────────────────────


class TestMemoryMigrateLogging:
    def test_memory_migrate_logs_error_when_no_projects_dir(
        self, fleet_dir, monkeypatch, tmp_path, caplog
    ):
        """Logs error (not print to stderr) when ~/.claude/projects/ missing."""
        # Point home to tmp_path so there's definitely no .claude/projects
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        args = _args(root=str(fleet_dir), map=None, force=False)
        with caplog.at_level(logging.ERROR, logger="claudlobby"):
            result = cmd_memory_migrate(args)
        assert result == 1
        assert "projects" in caplog.text.lower() or "claude" in caplog.text.lower()


# ── No-print audit ────────────────────────────────────────────────────────────


class TestNoPrintInMainCommands:
    """Regression: verify that logging-converted commands do not call print()
    for status/error output. This catches accidental re-introduction of prints."""

    def test_no_print_in_cmd_validate_path(self, fleet_dir, monkeypatch, capsys):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        args = _args(root=str(fleet_dir))
        cmd_validate(args)
        captured = capsys.readouterr()
        # cmd_validate should emit nothing to stdout (only logging)
        assert captured.out == ""

    def test_no_print_in_cmd_generate_path(self, fleet_dir, monkeypatch, capsys):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        args = _args(root=str(fleet_dir))
        with patch("claudlobby.__main__.compose_fleet", return_value={}):
            cmd_generate(args)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_no_print_in_list_library(self, fleet_dir, capsys):
        args = _args(root=str(fleet_dir))
        cmd_list_library(args)
        captured = capsys.readouterr()
        # list-library now uses log.info(), not print()
        assert captured.out == ""
