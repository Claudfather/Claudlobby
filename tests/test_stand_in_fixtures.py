"""The `bun` and `claude` stand-ins copy node only where a link cannot name the process (#2239).

tests/test_bridge_state.py and tests/test_claude_session_pid.py once copied node
for every case they ran. Both now take one shared set from tests/conftest.py's
session fixture `native_stand_ins`, which tests/stand_in_fixtures.py builds.
These tests build stand-ins from a small file in node's place, so they never
write what they guard against, and they run without conftest.
"""

import ast
import os
import sys
from pathlib import Path

import pytest

from tests import stand_in_fixtures
from tests.stand_in_fixtures import make_stand_ins

TESTS = Path(__file__).resolve().parent
PAYLOAD = b"a small file in node's place\n" * 256


def _build(tmp_path: Path) -> tuple[Path, Path]:
    """Stand-ins for a small fake node laid out as Homebrew lays out node: the
    binary in <prefix>/bin and a libnode dylib in <prefix>/lib."""
    node = tmp_path / "prefix" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_bytes(PAYLOAD)
    node.chmod(0o755)
    (tmp_path / "prefix" / "lib").mkdir()
    (tmp_path / "prefix" / "lib" / "libnode.127.dylib").write_bytes(b"dylib")
    bindir = tmp_path / "bins"
    bindir.mkdir()
    return node, make_stand_ins(bindir, str(node))


def _copies(bindir: Path) -> list[str]:
    """The names in bindir that hold node's bytes in a file of their own."""
    return sorted(p.name for p in bindir.iterdir()
                  if not p.is_symlink() and p.read_bytes() == PAYLOAD)


def test_this_host_copies_node_only_where_a_link_cannot_name_the_process(tmp_path):
    """On Linux no stand-in holds a copy of node; elsewhere each name holds one."""
    _, bindir = _build(tmp_path)
    assert _copies(bindir) == ([] if sys.platform == "linux" else ["bun", "claude"])


@pytest.mark.parametrize("links", [True, False], ids=["link", "copy"])
def test_each_name_is_node_as_a_link_or_as_exactly_one_copy(tmp_path, monkeypatch, links):
    """Both branches, on any host: an executable node under each name."""
    monkeypatch.setattr(stand_in_fixtures, "LINK_NAMES_THE_PROCESS", links)
    node, bindir = _build(tmp_path)
    for name in ("bun", "claude"):
        stand_in = bindir / name
        assert stand_in.is_symlink() is links
        if links:
            assert stand_in.resolve() == node.resolve()
        assert stand_in.read_bytes() == PAYLOAD
        assert os.access(stand_in, os.X_OK), f"{name} is not executable"
    assert _copies(bindir) == ([] if links else ["bun", "claude"])


def test_a_copy_keeps_homebrews_libnode_beside_it(tmp_path, monkeypatch):
    """A copied node loads libnode through an executable-relative rpath, so the
    library is linked in beside the copies."""
    monkeypatch.setattr(stand_in_fixtures, "LINK_NAMES_THE_PROCESS", False)
    node, bindir = _build(tmp_path)
    library = bindir / "libnode.127.dylib"
    assert library.is_symlink()
    assert library.resolve() == (node.parent.parent / "lib" / "libnode.127.dylib").resolve()


# The calls that would make a copy in a test file itself.
_COPIERS = {"shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree", "os.link"}


@pytest.mark.parametrize("name", ["test_bridge_state.py", "test_claude_session_pid.py"])
def test_the_native_test_files_copy_nothing_themselves(name):
    """Their stand-ins come only from make_stand_ins. A copy made in either file
    would be a copy per case again."""
    tree = ast.parse((TESTS / name).read_text(encoding="utf-8"))
    calls = [f"line {call.lineno}: {ast.unparse(call.func)}"
             for call in ast.walk(tree)
             if isinstance(call, ast.Call) and ast.unparse(call.func) in _COPIERS]
    assert calls == [], f"{name} copies a file itself: {calls}"
