"""Executables named `bun` and `claude`, for tests that walk a real process tree.

`ps` and /proc must report these processes as `bun` and `claude`, so each name
is given to a real binary. The note above `requires_node` in
tests/test_bridge_state.py says why that binary is node (#1012, #1087).

On Linux each name is a short bash script that runs node with the script's own
path as argv[0] (`exec -a`), so the command line starts with the stand-in's path,
as a symbolic link's did, and nothing is copied. Copying node (over 100 MB) for
every case cost over a gigabyte per run in pytest's tmp_path, which on a
Raspberry Pi is the SD card (#2239).

A link alone no longer names the process. Node 24 names its main thread
`MainThread` as it starts (its src/node.cc calls uv_thread_setname), and on
Linux the main thread's name is the process's `comm`, so a link named `bun` ran
as `MainThread` and the lineage walk read no `bun` or `claude` (#2930). So the
script gives node a preload, last in NODE_OPTIONS, which writes the name the
stand-in was started as back to /proc/self/comm before any script runs. Only
/proc/<pid>/exe and that NODE_OPTIONS entry still name node, and nothing under
test reads them.

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
SCRIPTS_NAME_THE_PROCESS = sys.platform == "linux"

# The preload each script gives node, written beside the scripts. It runs after
# node has named its main thread and before any script, and writes the
# basename of argv[0] (the script's path, through `exec -a`) as the comm. The
# kernel keeps 15 bytes of it.
NAME_PRELOAD = "stand-in-name.cjs"
NAME_PRELOAD_SOURCE = (
    "try { require('fs').writeFileSync('/proc/self/comm', "
    "require('path').basename(process.argv0).slice(0, 15)); } catch (error) {}\n"
)


def _stand_in_script(node: Path, preload: Path) -> str:
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
    if SCRIPTS_NAME_THE_PROCESS:
        preload = bindir / NAME_PRELOAD
        preload.write_text(NAME_PRELOAD_SOURCE)
    for name in names:
        if SCRIPTS_NAME_THE_PROCESS:
            (bindir / name).write_text(_stand_in_script(source, preload.resolve()))
            os.chmod(bindir / name, 0o755)
        else:
            shutil.copy(source, bindir / name)
            os.chmod(bindir / name, 0o755)
    # Homebrew Node may load libnode through an executable-relative rpath.
    # Preserve that runtime dependency without editing the copied executable.
    for library in (source.parent.parent / "lib").glob("libnode*.dylib"):
        (bindir / library.name).symlink_to(library)
    return bindir
