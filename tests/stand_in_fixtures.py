"""Executables named `bun` and `claude`, for tests that walk a real process tree.

`ps` and /proc must report these processes as `bun` and `claude`, so each name
is given to a real binary. The note above `requires_node` in
tests/test_bridge_state.py says why that binary is node (#1012, #1087).

On Linux a process is named after the path it was started from, so a symbolic
link named `bun` runs as `bun`, its command line starts with the link's path,
and nothing is copied. Copying node (over 100 MB) for every case cost over a
gigabyte per run in pytest's tmp_path, which on a Raspberry Pi is the SD card
(#2239). Only /proc/<pid>/exe still names node, and nothing under test reads it.

Node itself is the exception from Node 24 on. It names its main thread
`MainThread` as it starts (its src/node.cc calls uv_thread_setname), and on
Linux the main thread's name is the process's `comm`, so a link to node named
`bun` ran as `MainThread` and the lineage walk read no `bun` or `claude`
(#2930). So a stand-in for node is a short bash script: it runs node with the
script's own path as argv[0] (`exec -a`), so its command line is the one a link
gave, and gives node a preload, last in NODE_OPTIONS, that writes that name back
to /proc/self/comm before any script runs. That NODE_OPTIONS entry names node
too, and nothing under test reads it. A stand-in for any other executable, such
as tests/test_bridge_state.py's bash, is a link, as before.

Elsewhere each name is a copy, as before: whether a link keeps its own name on
macOS has not been checked. tests/conftest.py's `native_stand_ins` makes them
once per session.
"""

import os
import shlex
import shutil
import sys
from pathlib import Path

NODE = shutil.which("node")

# Read at each call, so a test can take the other branch on any host.
LINK_NAMES_THE_PROCESS = sys.platform == "linux"

# The names under which an executable is node, which renames itself as it starts.
NODE_NAMES = ("node", "nodejs")

# The preload a node stand-in gives node, written beside the stand-ins. It runs
# after node has named its main thread and before any script, and writes the
# basename of argv[0] (the script's path, through `exec -a`) as the comm. The
# kernel keeps 15 bytes of it.
NAME_PRELOAD = "stand-in-name.cjs"
NAME_PRELOAD_SOURCE = (
    "try { require('fs').writeFileSync('/proc/self/comm', "
    "require('path').basename(process.argv0).slice(0, 15)); } catch (error) {}\n"
)


def _node_script(node: Path, preload: Path) -> str:
    """A bash script that runs `node` as itself. The preload goes after any
    NODE_OPTIONS the caller set, so it runs last and its name is the one ps
    reads; its path is quoted for node's own NODE_OPTIONS parser."""
    unsafe = set('"\\$`')
    if unsafe & set(str(preload)):
        raise ValueError(f"cannot quote {preload} for NODE_OPTIONS: it holds one of {sorted(unsafe)}")
    return ("#!/bin/bash\n"
            f'NODE_OPTIONS="${{NODE_OPTIONS:+$NODE_OPTIONS }}--require=\\"{preload}\\"" '
            f'exec -a "$0" {shlex.quote(str(node))} "$@"\n')


def make_stand_ins(bindir: Path, executable: str, names=("bun", "claude")) -> Path:
    """Give `executable` each of `names` in the existing `bindir`; return `bindir`."""
    source = Path(executable).resolve()
    node = bool({Path(executable).name, source.name} & set(NODE_NAMES))
    if LINK_NAMES_THE_PROCESS and node:
        preload = bindir / NAME_PRELOAD
        preload.write_text(NAME_PRELOAD_SOURCE)
    for name in names:
        if LINK_NAMES_THE_PROCESS and node:
            (bindir / name).write_text(_node_script(source, preload.resolve()))
            os.chmod(bindir / name, 0o755)
        elif LINK_NAMES_THE_PROCESS:
            (bindir / name).symlink_to(source)
        else:
            shutil.copy(source, bindir / name)
            os.chmod(bindir / name, 0o755)
    # Homebrew Node may load libnode through an executable-relative rpath.
    # Preserve that runtime dependency without editing the copied executable.
    for library in (source.parent.parent / "lib").glob("libnode*.dylib"):
        (bindir / library.name).symlink_to(library)
    return bindir
