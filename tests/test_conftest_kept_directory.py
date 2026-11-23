"""Pins for the session directory the suite keeps when a test fails (#3102).

Each pytest session gets a short ct-* directory for its HOME, XDG directories and
TMPDIR (tests/conftest.py, _isolate_plane_session). A session with a failed test
keeps that directory to be read, so the run has to say where it is, whichever
test ran last: the fixture's teardown runs inside the last test's, whose captured
output pytest shows only when that test failed. Each case runs one small child
pytest through the real conftest, with CLAUDLOBBY_TEST_TMPDIR under this test's
own tmp_path, so the child's directory lands where this test can find and remove
it.
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

from tests.conftest import constructed_env

REPO = Path(__file__).resolve().parents[1]
NAMED = re.compile(r"^retained failed-test files: (.*)$", re.MULTILINE)
# What the conftest's _isolate_home makes in a session directory.
SESSION_DIRECTORIES = {"home", "config", "cache", "state", "data", "tmp"}

PASSES = "def test_passes():\n    pass\n"
FAILS = "def test_fails():\n    assert False\n"
PASSES_LAST = "def test_passes_last():\n    pass\n"
FAILS_LAST = "def test_fails_last():\n    assert False\n"


def _child_session(tmp_path, *tests, flags=()):
    """Run the tests, in this order, in one child pytest through the real conftest."""
    roots = tmp_path / "roots"
    roots.mkdir()
    with tempfile.TemporaryDirectory(prefix="kept-directory-", dir=REPO / "tests") as directory:
        probe = Path(directory) / "test_probe.py"
        probe.write_text("\n\n".join(tests))
        env = constructed_env(HOME=tmp_path / "home", PYTHONPATH=REPO,
                              CLAUDLOBBY_TEST_TMPDIR=roots)
        result = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                                 *flags, str(probe)],
                                cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    return result, roots


def _assert_names_its_kept_directory(result, roots):
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    named = NAMED.findall(output)
    assert len(named) == 1, output
    kept = Path(named[0])
    assert [path.resolve() for path in roots.iterdir()] == [kept], output
    assert kept.name.startswith("ct-") and kept.is_dir()
    assert SESSION_DIRECTORIES <= {path.name for path in kept.iterdir()}
    shutil.rmtree(kept)


@pytest.mark.parametrize("flags", [(), ("-v",), ("-q",)], ids=["default", "verbose", "quiet"])
def test_a_failed_session_names_its_kept_directory_when_its_last_test_passed(flags, tmp_path):
    result, roots = _child_session(tmp_path, FAILS, PASSES_LAST, flags=flags)
    _assert_names_its_kept_directory(result, roots)


def test_a_failed_session_names_its_kept_directory_when_its_last_test_failed(tmp_path):
    result, roots = _child_session(tmp_path, PASSES, FAILS_LAST)
    _assert_names_its_kept_directory(result, roots)


def test_a_passing_session_keeps_nothing_and_names_nothing(tmp_path):
    result, roots = _child_session(tmp_path, PASSES, PASSES_LAST)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "retained failed-test files" not in output
    assert not list(roots.iterdir()), output
