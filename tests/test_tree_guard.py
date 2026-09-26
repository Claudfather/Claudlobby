"""The guard against a run that exercises another tree's claudlobby (#1316).

A console script imports whatever tree its venv was built from, so a CLI
borrowed from another checkout, or found further down PATH, records and prunes
with that tree's code while the test reads back with this one's. The guard
asks the subprocess which ``claudlobby`` it imports, and raises when it is not
the tree under test.

Every refusal here has a control: the same kind of venv and script pointed at
the tree, which the guard accepts. Without one, a guard that refused anything
built this way would pass every refusal test without ever looking at the tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from claudlobby.tree_guard import (
    NotTheTreeUnderTest,
    assert_cli_imports_tree,
    assert_imports_tree,
)
from tests.conftest import plane_emit_env
from tests.fixtures.venv_from_tree import TRAMPOLINE_SCRIPT, tree, venv_importing

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_pythonpath(monkeypatch):
    # PYTHONPATH outranks site-packages, so an ambient one would decide what
    # every venv built here imports.
    monkeypatch.delenv("PYTHONPATH", raising=False)


def _init(root: Path) -> str:
    return str((root / "claudlobby" / "__init__.py").resolve())


def test_an_interpreter_started_in_a_tree_without_the_package_is_refused(tmp_path):
    """naked-bot-observe's route: `generate` runs as `python -m claudlobby`
    started in the export, so the export has to win over any installed copy.
    With no package there, the interpreter imports whatever its site-packages
    names, and every arm would compose with that."""
    other = tree(tmp_path / "other")
    python = venv_importing(tmp_path / "venv", other, script=None) / "python"
    export = tmp_path / "export"
    export.mkdir()
    with pytest.raises(NotTheTreeUnderTest) as refused:
        assert_imports_tree(export, str(python))
    assert f"it imports: {_init(other)}" in str(refused.value)
    assert f"under test: {_init(export)}" in str(refused.value)


def test_the_same_interpreter_started_in_the_tree_is_accepted(tmp_path):
    """The control: the cwd comes first on sys.path, so the tree wins over the
    venv's own copy. The naked-bot export relies on exactly that."""
    other = tree(tmp_path / "other")
    python = venv_importing(tmp_path / "venv", other, script=None) / "python"
    assert_imports_tree(tree(tmp_path / "export"), str(python))


def test_a_cli_whose_venv_imports_the_tree_is_accepted(tmp_path):
    """The control for every CLI refusal: a venv and script of the same kind,
    built to import the tree under test."""
    cli = venv_importing(tmp_path / "venv", REPO_ROOT) / "claudlobby"
    assert assert_cli_imports_tree(REPO_ROOT, cli) == str(cli)


def test_a_cli_behind_pips_sh_trampoline_is_read_through_it(tmp_path):
    """pip writes `#!/bin/sh` and an exec line when the interpreter path is too
    long for a shebang (over 127 bytes on Linux), which a checkout under a
    bot's projects/ can reach. The refusal has to name the tree the exec line's
    interpreter imports, not fail to find an interpreter at all."""
    other = tree(tmp_path / "other")
    bin_dir = venv_importing(tmp_path / "venv", other, script=TRAMPOLINE_SCRIPT)
    with pytest.raises(NotTheTreeUnderTest) as refused:
        assert_cli_imports_tree(REPO_ROOT, bin_dir / "claudlobby")
    assert f"it imports: {_init(other)}" in str(refused.value)


def test_no_cli_to_ask_is_a_refusal_not_a_skip(tmp_path):
    """A launcher whose PATH rung finds nothing falls through to whatever
    python3 imports, which nothing checks either."""
    with pytest.raises(NotTheTreeUnderTest, match="no CLI was found"):
        assert_cli_imports_tree(REPO_ROOT, None)
    with pytest.raises(NotTheTreeUnderTest, match="no such file"):
        assert_cli_imports_tree(REPO_ROOT, tmp_path / "bin" / "claudlobby")


def test_plane_emit_env_refuses_a_cli_that_imports_another_tree(tmp_path, monkeypatch):
    """#1316's first route: the suite records through the CLI beside the
    interpreter running pytest. Run with another checkout's venv, that CLI
    recorded with the other checkout's code while the test read back with this
    tree's, and 18 tests failed for a defect this tree does not have."""
    other = tree(tmp_path / "other")
    bin_dir = venv_importing(tmp_path / "venv", other)
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))
    with pytest.raises(NotTheTreeUnderTest) as refused:
        plane_emit_env()
    message = str(refused.value)
    assert str(bin_dir / "claudlobby") in message
    assert f"it imports: {_init(other)}" in message
    assert "python3 -m venv .venv" in message  # the remedy travels with it


def test_plane_emit_env_hands_out_a_cli_that_imports_this_tree(tmp_path, monkeypatch):
    bin_dir = venv_importing(tmp_path / "venv", REPO_ROOT)
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))
    assert plane_emit_env()["PLANE_EMIT_CLI"] == str(bin_dir / "claudlobby")
