"""Tests for tests/fixtures/worktree_export.py (#1794).

The export is what lets a compose test run from a checkout under a bot's
``projects/`` dir, so the property that matters is that it still carries the
IN-PROGRESS tree. A helper that quietly exported HEAD would turn every one of
those tests into a test of the last commit, and nothing would say so: the
tests would stay green while the edit under test never reached them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import git_isolation_env
from tests.fixtures.worktree_export import export_working_tree


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway repository with one commit, isolated from the host's git
    config. The isolation reaches the export's own ``git`` call through the
    environment, because a global excludes file could otherwise hide a file
    these tests expect to see copied."""
    no_excludes = tmp_path / "no-global-excludes"
    no_excludes.write_text("")
    cfg = tmp_path / "gitconfig"
    cfg.write_text(
        "[user]\n\tname = Export Test\n\temail = export-test@example.com\n"
        f"[core]\n\texcludesFile = {no_excludes}\n"
        "[commit]\n\tgpgsign = false\n"
    )
    for key, value in git_isolation_env(cfg).items():
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "tracked.md").write_text("committed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def test_the_export_is_the_working_tree_not_head(repo, tmp_path):
    """An unstaged edit, a staged-but-uncommitted file and a never-added file
    all reach the export with the content on disk. A `git archive HEAD`
    export fails all three; an index export fails the first and the last."""
    (repo / "tracked.md").write_text("edited, not committed\n")
    (repo / "staged.md").write_text("added, not committed\n")
    _git(repo, "add", "staged.md")
    (repo / "untracked.md").write_text("never added\n")

    out = export_working_tree(repo, tmp_path / "export")

    assert (out / "tracked.md").read_text() == "edited, not committed\n"
    assert (out / "staged.md").read_text() == "added, not committed\n"
    assert (out / "untracked.md").read_text() == "never added\n"
