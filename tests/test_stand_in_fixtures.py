"""The `bun` and `claude` stand-ins copy node only where a script cannot name the process
(#2239), and keep their own names on every node (#2930).

tests/test_bridge_state.py and tests/test_claude_session_pid.py once copied node
for every case they ran. Both now take one shared set from tests/conftest.py's
session fixture `native_stand_ins`, which tests/stand_in_fixtures.py builds.
Most of these tests build stand-ins from a small file in node's place, so they
never write what they guard against; the ones that read a running stand-in's
name start the real node through the scripts, which copies nothing. They run
without conftest.
"""

import ast
import json
import os
import shlex
import subprocess
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


def test_this_host_copies_node_only_where_a_script_cannot_name_the_process(tmp_path):
    """On Linux no stand-in holds a copy of node; elsewhere each name holds one."""
    _, bindir = _build(tmp_path)
    assert _copies(bindir) == ([] if sys.platform == "linux" else ["bun", "claude"])


@pytest.mark.parametrize("scripts", [True, False], ids=["script", "copy"])
def test_each_name_is_node_as_a_script_or_as_exactly_one_copy(tmp_path, monkeypatch, scripts):
    """Both branches, on any host: an executable node under each name. A script
    runs node by its resolved path, with the preload beside it."""
    monkeypatch.setattr(stand_in_fixtures, "SCRIPTS_NAME_THE_PROCESS", scripts)
    node, bindir = _build(tmp_path)
    preload = bindir / stand_in_fixtures.NAME_PRELOAD
    for name in ("bun", "claude"):
        stand_in = bindir / name
        assert not stand_in.is_symlink()
        if scripts:
            text = stand_in.read_text()
            assert text.startswith("#!/bin/bash\n")
            assert f'exec -a "$0" {shlex.quote(str(node.resolve()))} "$@"' in text
            assert f'--require=\\"{preload.resolve()}\\"' in text
        else:
            assert stand_in.read_bytes() == PAYLOAD
        assert os.access(stand_in, os.X_OK), f"{name} is not executable"
    assert preload.exists() is scripts
    if scripts:
        assert preload.read_text() == stand_in_fixtures.NAME_PRELOAD_SOURCE
    assert _copies(bindir) == ([] if scripts else ["bun", "claude"])


def test_a_copy_keeps_homebrews_libnode_beside_it(tmp_path, monkeypatch):
    """A copied node loads libnode through an executable-relative rpath, so the
    library is linked in beside the copies."""
    monkeypatch.setattr(stand_in_fixtures, "SCRIPTS_NAME_THE_PROCESS", False)
    node, bindir = _build(tmp_path)
    library = bindir / "libnode.127.dylib"
    assert library.is_symlink()
    assert library.resolve() == (node.parent.parent / "lib" / "libnode.127.dylib").resolve()


# What a running stand-in says its name is: /proc's comm, ps's comm, and its
# command line, which bridge_state reads for the executable.
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


@pytest.mark.skipif(stand_in_fixtures.NODE is None or sys.platform != "linux",
                    reason="runs node's stand-ins under Linux /proc")
@pytest.mark.parametrize("renamed", [False, True], ids=["as-started", "renamed-at-startup"])
def test_a_running_stand_in_keeps_its_own_name(tmp_path, renamed):
    """Each stand-in runs as its own name, however node names its main thread:
    the lineage walk reads `comm`, and a stand-in named `MainThread` reads as
    neither bun nor claude (#2930)."""
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
