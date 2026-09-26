"""A venv whose ``claudlobby`` is whichever tree you point it at (#1316).

Both routes #1316 measured were a CLI importing a different tree than the one
under test: the venv of another checkout, and the host install further down a
bot session's PATH. This builds that situation for real, and cheaply: a
pip-less venv (a symlinked interpreter, about 0.02 s on a Pi) whose
site-packages holds one ``.pth`` line naming the tree its ``claudlobby`` comes
from, which is the path entry an editable install writes, plus a console
script in the shape pip writes. Both script shapes are pip's own output,
captured from the ``ScriptMaker`` vendored in pip 23.0.1 with the paths
replaced.
"""

from __future__ import annotations

import venv
from pathlib import Path

#: pip's console script when the interpreter path fits in a shebang.
SCRIPT = """#!{python}
# -*- coding: utf-8 -*-
import re
import sys
from claudlobby.__main__ import main
if __name__ == '__main__':
    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])
    sys.exit(main())
"""

#: pip's console script when it does not (longer than 127 bytes on Linux, or
#: spaced): /bin/sh runs the second line, which re-execs the interpreter, and
#: Python reads the first three lines as one string.
TRAMPOLINE_SCRIPT = """#!/bin/sh
'''exec' {python} "$0" "$@"
' '''
# -*- coding: utf-8 -*-
import re
import sys
from claudlobby.__main__ import main
if __name__ == '__main__':
    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])
    sys.exit(main())
"""


def tree(where: Path) -> Path:
    """A tree holding a ``claudlobby`` package and nothing else."""
    pkg = where / "claudlobby"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.0.0"\n')
    return where


def venv_importing(
    where: Path, tree_root: Path, *, script: str | None = SCRIPT
) -> Path:
    """A venv at ``where`` whose ``claudlobby`` is ``tree_root``'s; returns its
    ``bin/``, which holds a ``claudlobby`` console script unless ``script`` is
    None."""
    venv.create(where, with_pip=False, symlinks=True)
    site = next(where.glob("lib/python*/site-packages"))
    (site / "tree.pth").write_text(f"{tree_root}\n")
    bin_dir = where / "bin"
    if script is not None:
        cli = bin_dir / "claudlobby"
        cli.write_text(script.format(python=bin_dir / "python"))
        cli.chmod(0o755)
    return bin_dir
