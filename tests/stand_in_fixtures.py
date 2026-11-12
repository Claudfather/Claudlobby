"""Executables named `bun` and `claude`, for tests that walk a real process tree.

`ps` and /proc must report these processes as `bun` and `claude`, so each name
is given to a real binary. The note above `requires_node` in
tests/test_bridge_state.py says why that binary is node (#1012, #1087).

On Linux a process is named after the path it was started from, so a symbolic
link named `bun` runs as `bun`, its command line starts with the link's path,
and nothing is copied. Copying node (over 100 MB) for every case cost over a
gigabyte per run in pytest's tmp_path, which on a Raspberry Pi is the SD card
(#2239). Only /proc/<pid>/exe still names node, and nothing under test reads it.

Elsewhere each name is a copy, as before: whether a link keeps its own name on
macOS has not been checked. tests/conftest.py's `native_stand_ins` makes them
once per session.
"""

import os
import shutil
import sys
from pathlib import Path

NODE = shutil.which("node")

# Read at each call, so a test can take the other branch on any host.
LINK_NAMES_THE_PROCESS = sys.platform == "linux"


def make_stand_ins(bindir: Path, executable: str, names=("bun", "claude")) -> Path:
    """Give `executable` each of `names` in the existing `bindir`; return `bindir`."""
    source = Path(executable).resolve()
    for name in names:
        if LINK_NAMES_THE_PROCESS:
            (bindir / name).symlink_to(source)
        else:
            shutil.copy(source, bindir / name)
            os.chmod(bindir / name, 0o755)
    # Homebrew Node may load libnode through an executable-relative rpath.
    # Preserve that runtime dependency without editing the copied executable.
    for library in (source.parent.parent / "lib").glob("libnode*.dylib"):
        (bindir / library.name).symlink_to(library)
    return bindir
