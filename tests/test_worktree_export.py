"""Tests for tests/fixtures/worktree_export.py (#1794).

The export is what lets a compose test run from a checkout under a bot's
``projects/`` dir, so the property that matters is that it still carries the
IN-PROGRESS tree. A helper that quietly exported HEAD would turn every one of
those tests into a test of the last commit, and nothing would say so: the
tests would stay green while the edit under test never reached them.
"""

from __future__ import annotations

import os
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
    (root / ".gitignore").write_text("ignored.md\n")
    (root / "tracked.md").write_text("committed\n")
    (root / "doomed.md").write_text("committed, then deleted\n")
    (root / "run.sh").write_text("#!/bin/sh\necho ok\n")
    os.chmod(root / "run.sh", 0o755)
    os.symlink("tracked.md", root / "link.md")
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


def test_the_export_leaves_out_what_a_commit_would_not_carry(repo, tmp_path):
    """Ignored files, files deleted from the working tree and `.git` stay
    out. A plain copy of the directory carries all three: `.git` would hand
    the history to the compose, and an ignored `local/` or `.env` would make
    the result depend on whoever ran it."""
    (repo / "ignored.md").write_text("gitignored\n")
    (repo / "doomed.md").unlink()

    out = export_working_tree(repo, tmp_path / "export")

    assert (out / "tracked.md").is_file()  # the positive control: it did copy
    assert not (out / "ignored.md").exists()
    assert not (out / "doomed.md").exists()
    assert not (out / ".git").exists()


def test_the_export_keeps_executable_bits_and_symlinks(repo, tmp_path):
    """The executable bit is part of the tree git records, and a byte-only
    copy drops it; a caller that runs a `lib/` script from the export
    directly, rather than through `bash`, needs it. A symlink stays a
    symlink with its own target rather than becoming a copy of whatever it
    pointed at."""
    out = export_working_tree(repo, tmp_path / "export")

    assert os.access(out / "run.sh", os.X_OK)
    assert (out / "link.md").is_symlink()
    assert os.readlink(out / "link.md") == "tracked.md"


def test_the_export_refuses_a_destination_inside_a_bot_runtime_tree(repo, tmp_path):
    """An export under `…/runtime/bots/…` fails `path_audit`'s shape check
    exactly as the checkout did, so it would bring back #1794 with a less
    obvious cause. It refuses before copying anything."""
    dest = tmp_path / "runtime" / "bots" / "somebot" / "export"

    with pytest.raises(ValueError, match="runtime/bots"):
        export_working_tree(repo, dest)

    assert not dest.exists()
