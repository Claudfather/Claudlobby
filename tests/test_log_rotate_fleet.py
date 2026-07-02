"""Regression coverage for lib/log-rotate-fleet.sh — previously untested,
which let a dangling `-o` in its data/ find expression ship: find rejected
the whole expression with a syntax error (eaten by 2>/dev/null inside a
process substitution), silently dropping rotation for every data/ log type."""

import os
import shutil
import subprocess

from tests.conftest import _scrubbed_env

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO_ROOT, "lib")

# Every data/ log name the find expression must match.
DATA_LOGS = ("cron.log", "git-pull.log", "briefing-morning.log", "home-assistant.log")


def _fleet_root(tmp_path):
    root = tmp_path / "root"
    libdir = root / "lib"
    libdir.mkdir(parents=True)
    # The script resolves its rotator from CLAUDLOBBY_ROOT.
    for script in ("log-rotate.sh", "lib-common.sh"):
        shutil.copy2(os.path.join(LIB, script), libdir / script)
    bot = root / "local" / "f" / "runtime" / "bots" / "b1"
    (bot / "logs").mkdir(parents=True)
    (bot / "data").mkdir()
    body = "line\n" * 600
    (bot / "logs" / "session.log").write_text(body)
    for name in DATA_LOGS:
        (bot / "data" / name).write_text(body)
    (bot / "data" / "binary.ldb").write_text(body)
    return root, bot


def _run(root):
    return subprocess.run(
        ["bash", os.path.join(LIB, "log-rotate-fleet.sh"), "--keep", "100"],
        env=_scrubbed_env(CLAUDLOBBY_ROOT=str(root)),
        capture_output=True,
        text=True,
    )


def _lines(path):
    with open(path) as f:
        return sum(1 for _ in f)


class TestLogRotateFleet:
    def test_rotates_every_discovered_log_type(self, tmp_path):
        # Also the find-expression validity regression: a syntax error there
        # leaves every data/ log at 600 lines and this fails loudly.
        root, bot = _fleet_root(tmp_path)
        r = _run(root)
        assert r.returncode == 0, r.stderr + r.stdout
        assert _lines(bot / "logs" / "session.log") == 100
        for name in DATA_LOGS:
            assert _lines(bot / "data" / name) == 100, f"{name} not rotated"

    def test_unknown_data_files_left_alone(self, tmp_path):
        root, bot = _fleet_root(tmp_path)
        r = _run(root)
        assert r.returncode == 0, r.stderr + r.stdout
        assert _lines(bot / "data" / "binary.ldb") == 600

    def test_data_find_expression_is_valid(self, tmp_path):
        # Belt to the behavior test's braces: run the exact expression shape
        # WITHOUT the error-eating redirect — find must accept it.
        d = tmp_path / "data"
        d.mkdir()
        r = subprocess.run(
            [
                "find", str(d), "-maxdepth", "3", "-type", "f",
                "(", "-name", "cron.log", "-o", "-name", "git-pull.log",
                "-o", "-name", "briefing*.log", "-o", "-name", "home-assistant.log", ")",
                "-print0",
            ],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
