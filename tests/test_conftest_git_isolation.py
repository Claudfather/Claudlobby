"""Pins for the suite's own git isolation, run through the real conftest.

An inherited GIT_DIR, GIT_WORK_TREE or GIT_INDEX_FILE redirects every repository
a fixture builds, and GIT_CONFIG_COUNT adds config above the one a test composes.
Each failure reads as a flake or a stale checkout. So each case runs one small
child pytest whose parent exports one such variable, pointed at paths under this
test's own tmp_path. The parent also exports a GIT_CONFIG_GLOBAL of its own: that
is not a repository variable, and it must still reach the tests.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from tests.conftest import constructed_env

REPO = Path(__file__).resolve().parents[1]

PROBE = '''import subprocess

import pytest

from tests.conftest import git_isolation_env


def _git(cwd, *args, env=None):
    return subprocess.run(["git", "-C", str(cwd), *args], env=env, capture_output=True,
                          text=True, timeout=30)


def _commit_one_file(repo):
    """Build a repository the way fixtures do, and check it landed where asked."""
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    (repo / "tracked.txt").write_text("x")
    assert _git(repo, "add", "tracked.txt").returncode == 0
    assert _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com",
                "commit", "-q", "-m", "c").returncode == 0
    assert (repo / ".git" / "index").is_file()
    assert _git(repo, "ls-tree", "--name-only", "HEAD").stdout.split() == ["tracked.txt"]


@pytest.fixture(scope="session")
def session_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("session") / "repo"
    _commit_one_file(repo)
    return repo


def test_session_fixture_git_builds_its_own_repository(session_repo):
    assert (session_repo / "tracked.txt").is_file()


def test_fixture_git_builds_its_own_repository(tmp_path):
    _commit_one_file(tmp_path / "repo")


def test_the_composed_config_alone_answers(tmp_path):
    cfg = tmp_path / "composed.gitconfig"
    cfg.write_text("[user]\\n\\temail = composed@example.com\\n")
    result = _git(tmp_path, "config", "--get", "user.email", env=git_isolation_env(cfg))
    assert result.stdout.strip() == "composed@example.com"


def test_the_callers_own_global_config_still_answers(tmp_path):
    assert _git(tmp_path, "config", "--get", "user.email").stdout.strip() == "caller@example.com"
'''

PROBE_TESTS = ("test_session_fixture_git_builds_its_own_repository",
               "test_fixture_git_builds_its_own_repository",
               "test_the_composed_config_alone_answers",
               "test_the_callers_own_global_config_still_answers")

POISONS = {
    "GIT_CONFIG_COUNT": lambda target: {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.email",
                                        "GIT_CONFIG_VALUE_0": "ambient@example.com"},
    "GIT_DIR": lambda target: {"GIT_DIR": target / "repo.git"},
    "GIT_INDEX_FILE": lambda target: {"GIT_INDEX_FILE": target / "index"},
    "GIT_WORK_TREE": lambda target: {"GIT_WORK_TREE": target / "tree"},
}


@pytest.mark.parametrize("poison", sorted(POISONS))
def test_an_exported_git_variable_reaches_no_fixture(poison, tmp_path):
    target = tmp_path / "poisoned"
    target.mkdir()
    caller = tmp_path / "caller.gitconfig"
    caller.write_text("[user]\n\temail = caller@example.com\n")
    with tempfile.TemporaryDirectory(prefix="git-isolation-", dir=REPO / "tests") as directory:
        probe = Path(directory) / "test_probe.py"
        relative = probe.relative_to(REPO)
        probe.write_text(PROBE)
        env = constructed_env(HOME=tmp_path / "home", PYTHONPATH=REPO, GIT_CONFIG_GLOBAL=caller,
                              **POISONS[poison](target))
        result = subprocess.run([sys.executable, "-m", "pytest", "-v", "-p", "no:cacheprovider",
                                 "--override-ini=addopts=", str(probe)],
                                cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in PROBE_TESTS:
        assert f"{relative}::{name} PASSED" in result.stdout
    assert f"{len(PROBE_TESTS)} passed" in result.stdout
    assert not list(target.iterdir())
