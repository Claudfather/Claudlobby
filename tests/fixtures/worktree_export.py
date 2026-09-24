"""tests/fixtures/worktree_export.py — the working tree, exported (#1794).

A test that composes against THIS checkout fails wherever the checkout lives
under a bot's ``projects/`` dir, which is where the dev-checkout guardrail puts
bot dev work. The root then sits inside ``…/runtime/bots/…``, which
``path_audit._fleet_layout_needles`` reads as fleet-owned by SHAPE, so every
absolute path the compose writes under the root (``CLAUDLOBBY_ROOT`` in
``bot.conf``, the unit log paths) reads as another fleet's path and ``generate``
refuses. CI checks out elsewhere and never sees it.

``lib/naked-bot-observe.py`` avoids this by exporting a named ref with
``git archive``. A test cannot do that: it has to compose the tree it is
testing, uncommitted edits included, or it is testing the last commit. So this
copies the tree as git sees it — tracked files, plus untracked files that are
not ignored — taking each file's content from disk. The result is what
``git archive`` would give for a commit of the working tree as it stands,
without making the commit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def export_working_tree(repo: Path, dest: Path) -> Path:
    """Copy *repo*'s working tree, as git sees it, into *dest*; return *dest*.

    Tracked files and untracked-but-not-ignored files are copied from disk, so
    an unstaged edit, a staged file and a never-added file all arrive as they
    are on disk. A tracked file deleted from disk stays out, as do ignored files
    and ``.git``. File modes and symlinks are kept.

    Refuses a *dest* inside ``…/runtime/bots/…``, since an export there fails
    the same shape check the checkout does.
    """
    if any("/runtime/bots/" in f"{p}/" for p in (dest, dest.resolve())):
        raise ValueError(
            f"refusing to export into {dest}: it sits inside a bot runtime tree "
            "(…/runtime/bots/…), which fails path_audit's shape check exactly as "
            "the checkout does (#1794). Point TMPDIR or --basetemp elsewhere."
        )
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        stdout=subprocess.PIPE,
        check=True,
    ).stdout
    dest.mkdir(parents=True)
    for rel in sorted({os.fsdecode(e) for e in listed.split(b"\0") if e}):
        src, out = repo / rel, dest / rel
        if src.is_symlink():
            out.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(src), out)
        elif src.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
        elif src.is_dir():
            # A submodule or a nested repository: `git archive` exports it as
            # an empty directory, and so does this.
            out.mkdir(parents=True, exist_ok=True)
        # Otherwise it is tracked but deleted from disk, and a commit of this
        # tree would not carry it either.
    return dest
