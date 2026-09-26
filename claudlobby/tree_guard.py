"""Refuse a run whose subprocess would import another tree's claudlobby (#1316).

An editable install of this package is normally importable, and a green run
against a stale one is indistinguishable from a green run against the tree
under test. The failure mode is a PASS, so it is checked, not assumed. A red
run is no better: the same mismatch failed 18 tests for a defect the tree
under test does not have, which reads exactly like a regression (#1316).

So both doors RAISE and never warn: a warning printed beside a green result
is a green result. Each asks the subprocess itself which ``claudlobby`` it
imports, started the way it will run, rather than inferring it from paths.

* ``assert_imports_tree(root, python)``: an interpreter started in ``root``,
  as ``python -m claudlobby`` is by a harness that composes an exported tree.
  The cwd is first on ``sys.path``, so the tree must win over any installed
  copy. This was ``lib/naked-bot-observe.py``'s private ``_assert_compositor``
  (#1168), and that gate first ran on a host whose shared install had been 27
  commits stale hours earlier.
* ``assert_cli_imports_tree(root, cli)``: a console script. Its
  ``sys.path[0]`` is its own directory, not the cwd, so it imports whatever
  its interpreter's site-packages names: with an editable install, the tree
  the venv was built from. The interpreter comes from the script's shebang
  (plain, or pip's ``/bin/sh`` trampoline for a path too long for one) and is
  asked from the script's own directory, which reproduces the script's import.

Stdlib only.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path

_PROBE = "import claudlobby, sys; sys.stdout.write(claudlobby.__file__)"

#: The line pip writes under ``#!/bin/sh`` when the interpreter path is too
#: long for a shebang (over 127 bytes on Linux) or holds a space: the shell
#: runs it, and Python reads it inside a string.
_TRAMPOLINE = "'''exec' "

_WHY = (
    "Why: a run against another tree's claudlobby reports on that tree. Its "
    "pass is not a pass here, and its failure is not a regression here (#1316)."
)


class NotTheTreeUnderTest(RuntimeError):
    """A run was about to exercise another tree's claudlobby."""


def assert_imports_tree(root: Path | str, python: str) -> None:
    """Refuse unless ``python``, started in ``root``, imports ``root``'s
    claudlobby."""
    root = Path(root)
    _refuse_unless_tree(
        root,
        [python],
        cwd=root,
        who=f"{python} started in {root}",
        remedy=(
            f"{root} has to hold the claudlobby package, and the interpreter "
            "has to keep its cwd first on sys.path (no -I, -P or PYTHONSAFEPATH)."
        ),
    )


def assert_cli_imports_tree(
    root: Path | str,
    cli: Path | str | None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Refuse unless the console script ``cli``, run in ``env`` (default: this
    process's), imports ``root``'s claudlobby. Returns ``cli`` as a string."""
    root = Path(root)
    remedy = (
        "build a venv from this tree and run with it:\n"
        f"  cd {root} && python3 -m venv .venv && "
        "./.venv/bin/python -m pip install -e '.[dev]'\n"
        "  then use ./.venv/bin/pytest (or ./.venv/bin/python)."
    )
    if not cli:
        raise NotTheTreeUnderTest(
            _refusal(root, "the claudlobby CLI", "nothing: no CLI was found", remedy)
        )
    who = f"the CLI {cli}"
    script = Path(cli).resolve()
    if not script.is_file():
        raise NotTheTreeUnderTest(
            _refusal(root, who, "nothing: there is no such file", remedy)
        )
    argv = _interpreter(script)
    if not argv:
        raise NotTheTreeUnderTest(
            _refusal(root, who, "nothing: its shebang names no interpreter", remedy)
        )
    _refuse_unless_tree(
        root,
        argv,
        cwd=script.parent,
        who=f"{who} (run by {shlex.join(argv)})",
        remedy=remedy,
        env=env,
    )
    return str(cli)


def _interpreter(script: Path) -> list[str]:
    """The command ``script``'s shebang runs it with, or [] when unreadable."""
    try:
        with script.open("rb") as fh:
            lines = fh.read(4096).decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    if not lines or not lines[0].startswith("#!"):
        return []
    try:
        if (
            lines[0].strip() == "#!/bin/sh"
            and len(lines) > 1
            and lines[1].startswith(_TRAMPOLINE)
        ):
            argv = shlex.split(lines[1][len(_TRAMPOLINE):])
            return argv[:-2] if argv[-2:] == ["$0", "$@"] else []
        return shlex.split(lines[0][2:])
    except ValueError:  # unbalanced quotes
        return []


def _refuse_unless_tree(
    root: Path,
    argv: list[str],
    *,
    cwd: Path,
    who: str,
    remedy: str,
    env: Mapping[str, str] | None = None,
) -> None:
    try:
        proc = subprocess.run(
            [*argv, "-c", _PROBE],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise NotTheTreeUnderTest(
            _refusal(root, who, f"nothing: it could not be run ({exc})", remedy)
        ) from exc
    got = proc.stdout.strip()
    if proc.returncode != 0 or not got:
        err = " | ".join(proc.stderr.strip().splitlines()[-2:]) or "no output"
        raise NotTheTreeUnderTest(
            _refusal(root, who, f"nothing: rc {proc.returncode}, {err}", remedy)
        )
    loaded = Path(got).resolve()
    if loaded != _package_init(root):
        raise NotTheTreeUnderTest(_refusal(root, who, str(loaded), remedy))


def _package_init(root: Path) -> Path:
    return (root / "claudlobby" / "__init__.py").resolve()


def _refusal(root: Path, who: str, imports: str, remedy: str) -> str:
    return (
        f"REFUSING: {who} does not import the claudlobby under test.\n"
        f"  it imports: {imports}\n"
        f"  under test: {_package_init(root)}\n"
        f"{_WHY}\n"
        f"Remedy: {remedy}"
    )
