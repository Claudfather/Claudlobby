"""Regression coverage for claudlobby/_runtime_scripts/log-rotate-fleet.sh: it
rotates the logs the framework's own jobs write (each bot's root and logs/,
the host's state/logs/), and nothing under a bot's data/, which holds the
bot's own files. The weekly data-sweep.sh purges no log there by name either,
so a file under data/ is neither cut nor deleted by its name (#3165)."""

import os
import subprocess
import time

import pytest

from tests.conftest import _scrubbed_env

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO_ROOT, "claudlobby", "_runtime_scripts")

# The names the data/ walk once matched to depth 3, and the sweep at any depth.
DROPPED_DATA_NAMES = ("cron.log", "git-pull.log", "briefing-morning.log", "home-assistant.log")
# Older than the sweep's 30-day window.
STALE = time.time() - 40 * 86400


def _fleet_root(tmp_path):
    root = tmp_path / "root"
    (root / "state" / "logs").mkdir(parents=True)
    (root / "state" / "logs" / "bot-sweep-cron.log").write_text("line\n" * 600)
    bot = root / "local" / "f" / "runtime" / "bots" / "b1"
    (bot / "logs").mkdir(parents=True)
    (bot / "data").mkdir()
    body = "line\n" * 600
    (bot / "keepalive.log").write_text(body)
    (bot / "logs" / "session.log").write_text(body)
    (bot / "data" / "binary.ldb").write_text(body)
    return root, bot


def _stale_file(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("line\n" * lines)
    os.utime(path, (STALE, STALE))
    return path


def _run(script, *args, root):
    return subprocess.run(
        ["bash", os.path.join(LIB, script), *args],
        env=_scrubbed_env(CLAUDLOBBY_ROOT=str(root)),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _rotate(root):
    return _run("log-rotate-fleet.sh", "--keep", "100", root=root)


def _sweep(root):
    return _run("data-sweep.sh", "--purge", "f", root=root)


def _lines(path):
    with open(path) as f:
        return sum(1 for _ in f)


def _assert_neither_rotated_nor_swept(root, f, lines):
    before = os.stat(f)
    r = _rotate(root)
    assert r.returncode == 0, r.stderr + r.stdout
    after = os.stat(f)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns), "rotated"
    assert _lines(f) == lines
    r = _sweep(root)
    assert r.returncode == 0, r.stderr + r.stdout
    assert f.exists(), "swept"


class TestLogRotateFleet:
    def test_rotates_the_logs_the_framework_writes(self, tmp_path):
        root, bot = _fleet_root(tmp_path)
        r = _rotate(root)
        assert r.returncode == 0, r.stderr + r.stdout
        assert _lines(bot / "keepalive.log") == 100
        assert _lines(bot / "logs" / "session.log") == 100
        assert _lines(root / "state" / "logs" / "bot-sweep-cron.log") == 100

    @pytest.mark.parametrize("name", DROPPED_DATA_NAMES)
    def test_a_data_file_by_a_dropped_name_is_neither_rotated_nor_swept(self, tmp_path, name):
        root, bot = _fleet_root(tmp_path)
        _assert_neither_rotated_nor_swept(root, _stale_file(bot / "data" / name, 600), 600)

    # Over the cap, the walk cut it; under it, the rotation fix alone would
    # have left it stale, for the sweep's name match to delete.
    @pytest.mark.parametrize("lines", [600, 50])
    def test_a_bots_own_record_is_neither_rotated_nor_swept(self, tmp_path, lines):
        root, bot = _fleet_root(tmp_path)
        f = _stale_file(bot / "data" / "archive" / "q1" / "briefing-notes.log", lines)
        _assert_neither_rotated_nor_swept(root, f, lines)

    def test_unknown_data_files_left_alone(self, tmp_path):
        root, bot = _fleet_root(tmp_path)
        r = _rotate(root)
        assert r.returncode == 0, r.stderr + r.stdout
        assert _lines(bot / "data" / "binary.ldb") == 600
