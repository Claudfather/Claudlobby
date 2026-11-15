"""The `bun` and `claude` stand-ins copy node only where a link cannot name the process (#2239),
and a stand-in for node keeps its name on Node 24 (#2930).

tests/test_bridge_state.py and tests/test_claude_session_pid.py once copied node
for every case they ran. Both now take one shared set from tests/conftest.py's
session fixture `native_stand_ins`, which tests/stand_in_fixtures.py builds.
Most of these tests build stand-ins from a small file in place of the
executable, so they never write what they guard against. The ones that read a
running stand-in's name start the real node or bash through a script or a link,
which copies nothing. They run without conftest.
"""

import ast
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests import stand_in_fixtures
from tests.stand_in_fixtures import make_stand_ins

TESTS = Path(__file__).resolve().parent
PAYLOAD = b"a small file in node's place\n" * 256


def _build(tmp_path: Path, name: str = "node") -> tuple[Path, Path]:
    """Stand-ins for a small fake executable called `name`, laid out as Homebrew
    lays out node: the binary in <prefix>/bin and a libnode dylib in <prefix>/lib."""
    node = tmp_path / "prefix" / "bin" / name
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
def test_each_name_is_the_executable_as_a_link_or_as_exactly_one_copy(tmp_path, monkeypatch, links):
    """Both branches, on any host: an executable that is not node under each
    name, as tests/test_bridge_state.py gives bash."""
    monkeypatch.setattr(stand_in_fixtures, "LINK_NAMES_THE_PROCESS", links)
    node, bindir = _build(tmp_path, "bash")
    for name in ("bun", "claude"):
        stand_in = bindir / name
        assert stand_in.is_symlink() is links
        if links:
            assert stand_in.resolve() == node.resolve()
        assert stand_in.read_bytes() == PAYLOAD
        assert os.access(stand_in, os.X_OK), f"{name} is not executable"
    assert _copies(bindir) == ([] if links else ["bun", "claude"])


def test_a_node_stand_in_is_a_script_where_a_link_names_the_process(tmp_path, monkeypatch):
    """Where a link would name the process, node gets a script instead: it runs
    node by its resolved path as itself, with the preload beside it, and nothing
    is copied."""
    monkeypatch.setattr(stand_in_fixtures, "LINK_NAMES_THE_PROCESS", True)
    node, bindir = _build(tmp_path)
    preload = bindir / stand_in_fixtures.NAME_PRELOAD
    assert preload.read_text() == stand_in_fixtures.NAME_PRELOAD_SOURCE
    for name in ("bun", "claude"):
        stand_in = bindir / name
        assert not stand_in.is_symlink()
        text = stand_in.read_text()
        assert text.startswith("#!/bin/bash\n")
        assert f'exec -a "$0" {shlex.quote(str(node.resolve()))} "$@"' in text
        assert f'--require=\\"{preload.resolve()}\\"' in text
        assert os.access(stand_in, os.X_OK), f"{name} is not executable"
    assert _copies(bindir) == []


def test_a_copy_keeps_homebrews_libnode_beside_it(tmp_path, monkeypatch):
    """A copied node loads libnode through an executable-relative rpath, so the
    library is linked in beside the copies."""
    monkeypatch.setattr(stand_in_fixtures, "LINK_NAMES_THE_PROCESS", False)
    node, bindir = _build(tmp_path)
    library = bindir / "libnode.127.dylib"
    assert library.is_symlink()
    assert library.resolve() == (node.parent.parent / "lib" / "libnode.127.dylib").resolve()


# What a running node stand-in says its name is: /proc's comm, ps's comm, and
# its command line, which bridge_state reads for the executable.
_SHOW = """\
const ps = (field) => require('child_process').execFileSync(
    'ps', ['-ww', '-o', field, '-p', String(process.pid)], {encoding: 'utf8'}).trim();
console.log(JSON.stringify({proc: require('fs').readFileSync('/proc/self/comm', 'utf8').trim(),
                            comm: ps('comm='), args: ps('args=')}));
"""

# What Node 24 does as it starts (its src/node.cc names the main thread
# `MainThread`, and on Linux that is the process's comm), done by a preload so
# it happens on any node. The stand-in's own preload must still have the last
# word (#2930).
_RENAME = "require('fs').writeFileSync('/proc/self/comm', 'MainThread');\n"

_LINUX = pytest.mark.skipif(sys.platform != "linux", reason="reads Linux /proc names")


@_LINUX
@pytest.mark.skipif(stand_in_fixtures.NODE is None, reason="runs node")
@pytest.mark.parametrize("renamed", [False, True], ids=["as-started", "renamed-at-startup"])
def test_a_running_node_stand_in_keeps_its_own_name(tmp_path, renamed):
    """Each node stand-in runs as its own name, however node names its main
    thread: the lineage walk reads `comm`, and a stand-in named `MainThread`
    reads as neither bun nor claude (#2930)."""
    bindir = tmp_path / "bins"
    bindir.mkdir()
    make_stand_ins(bindir, stand_in_fixtures.NODE)
    show = tmp_path / "show.js"
    show.write_text(_SHOW)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if renamed:
        (tmp_path / "rename.cjs").write_text(_RENAME)
        env["NODE_OPTIONS"] = f'--require="{tmp_path / "rename.cjs"}"'
    for name in ("bun", "claude"):
        run = subprocess.run([str(bindir / name), str(show), "server.ts"], env=env,
                             capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr
        seen = json.loads(run.stdout)
        assert (seen["proc"], seen["comm"]) == (name, name), seen
        assert seen["args"].split() == [str(bindir / name), str(show), "server.ts"], seen


@_LINUX
def test_a_running_bash_stand_in_is_named_by_its_link(tmp_path):
    """Any other executable keeps its link, which the kernel names the process
    by: tests/test_bridge_state.py's bash stand-ins read `bun` and `claude`."""
    bash = shutil.which("bash")
    assert bash, "bash not found"
    bindir = tmp_path / "bins"
    bindir.mkdir()
    make_stand_ins(bindir, bash)
    for name in ("bun", "claude"):
        assert (bindir / name).is_symlink()
        # Builtins only: bash would exec a last external command in its place.
        run = subprocess.run([str(bindir / name), "-c", 'read -r c < /proc/$$/comm; printf "%s\\n" "$c"'],
                             capture_output=True, text=True, timeout=30)
        assert (run.returncode, run.stdout.strip()) == (0, name), run.stderr


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
