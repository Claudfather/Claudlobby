"""Stage wheel-built assets for tests that import an editable source checkout.

Run ONLY in an explicitly disposable test checkout, after its editable dev
install, using that venv's interpreter:

    .venv/bin/python tests/prepare_resources.py --disposable-checkout "$PWD"

A private ``git archive`` export has no index: first run ``git init --quiet``
and ``git add --all`` there, BEFORE preparing resources. New authored files must
be indexed too. No commit, remote, or Git identity is required. Indexed paths
are copied using their current working bytes, so unstaged edits are included;
untracked files, old build output, and previously staged test assets are not.

The wheel is built in a separate temporary source tree with ``python -m build
--no-isolation`` (the dev extra supplies its dependencies). Only _artifact.json
and _resources/ are copied back; runtime scripts are already authored at their
installed package path, _runtime_scripts/. Python imports continue to resolve to
the exact checkout, preserving the test_cli origin guard. This is test setup,
never a runtime resource fallback. Do not add the generated paths to Git or
copy them into source-build fixtures. Rerun preparation after resource source
changes; pytest checks the prepared copies against those sources before tests.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile


GENERATED = ("_artifact.json", "_resources")


def _run(args, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        [str(arg) for arg in args], cwd=cwd, env=env, capture_output=True,
        text=True, timeout=300,
    )
    if result.returncode:
        raise RuntimeError(f"Command failed: {args!r}\n{result.stdout}{result.stderr}")
    return result.stdout


def _relative(name: str) -> Path:
    parts = name.split("/")
    if (not name or "\\" in name or "\0" in name
            or PurePosixPath(name).is_absolute()
            or any(part in ("", ".", "..", ".git") for part in parts)):
        raise ValueError(f"Unsafe relative path: {name!r}")
    return Path(*parts)


def _copy_indexed_source(root: Path, source: Path, env: dict[str, str]) -> None:
    if not (root / ".git").exists():
        raise ValueError("Disposable checkout needs its own Git index; index exports first")
    top = _run(["git", "rev-parse", "--show-toplevel"], root, env).strip()
    if Path(top).resolve() != root:
        raise ValueError("Disposable checkout must be the Git worktree root")
    rows = _run(["git", "ls-files", "--stage", "-z"], root, env).split("\0")
    names = []
    for row in filter(None, rows):
        header, name = row.split("\t", 1)
        mode, _oid, stage = header.split()
        relative = _relative(name)
        if mode not in ("100644", "100755") or stage != "0":
            raise ValueError(f"Indexed source must be a resolved regular file: {name}")
        if (len(relative.parts) > 1 and relative.parts[0] == "claudlobby"
                and relative.parts[1] in GENERATED):
            raise ValueError(f"Generated test assets must not be indexed: {name}")
        path = root / relative
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Indexed source is missing or escapes the checkout: {name}")
        names.append(relative)
    required = {Path(name) for name in (
        "setup.py", "pyproject.toml", "claudlobby/__init__.py", "claudlobby/system.yaml",
    )}
    if not required.issubset(names):
        raise ValueError("Git index is missing required Claudlobby build inputs")
    for relative in names:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)
    # ResourceBuild uses the index as its authored-resource inventory. This
    # private index contains only the validated source files copied above.
    _run(["git", "init", "--quiet"], source, env)
    _run(["git", "add", "--force", "--all"], source, env)


def _extract_assets(wheel: Path, destination: Path) -> None:
    seen = set()
    runtime_scripts = set()
    with zipfile.ZipFile(wheel) as archive:
        for item in archive.infolist():
            name = item.filename.rstrip("/") if item.is_dir() else item.filename
            relative = _relative(name)
            if item.orig_filename != item.filename or name in seen:
                raise ValueError(f"Ambiguous wheel member: {item.orig_filename!r}")
            seen.add(name)
            mode = item.external_attr >> 16
            kind = stat.S_IFMT(mode)
            expected = stat.S_IFDIR if item.is_dir() else stat.S_IFREG
            if kind not in (0, expected):
                raise ValueError(f"Wheel member is not a regular file/directory: {name}")
            if relative.parts[:1] != ("claudlobby",) or len(relative.parts) < 2:
                continue
            asset = relative.parts[1]
            if asset == "_runtime_scripts" and not item.is_dir():
                runtime_scripts.add(name)
            if asset not in GENERATED:
                continue
            if asset == "_artifact.json" and (len(relative.parts) != 2 or item.is_dir()):
                raise ValueError("Wheel artifact metadata must be one regular file")
            target = destination / relative
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    output.write(archive.read(item))
                target.chmod(0o644 | (mode & 0o111))
    package = destination / "claudlobby"
    metadata = json.loads((package / "_artifact.json").read_text())
    directories = [
        package / "_resources" / name for name in ("library", "library/voices", "templates", "seeds")
    ]
    if metadata.get("schema") != 1 or not metadata.get("artifact_id"):
        raise ValueError("Wheel has unsupported or missing artifact metadata")
    if not runtime_scripts or any(not path.is_dir() or not any(path.iterdir())
                                  for path in directories):
        raise ValueError("Wheel is missing required runtime resource directories")


def prepare(root: Path) -> None:
    root = root.resolve()
    if root != Path(__file__).resolve().parents[1]:
        raise ValueError("Run the helper belonging to the disposable checkout being prepared")
    package = root / "claudlobby"
    if package.is_symlink() or any((package / name).is_symlink() for name in GENERATED):
        raise ValueError("Test asset destinations must not be symlinks")
    with tempfile.TemporaryDirectory(prefix="claudlobby-test-resources-") as temporary:
        scratch = Path(temporary)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("GIT_") and key not in ("PYTHONPATH", "PYTHONHOME")}
        for key, name in (("HOME", "home"), ("TMPDIR", "tmp"),
                          ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                          ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state")):
            path = scratch / name
            path.mkdir()
            env[key] = str(path)
        env.update(PLANE_EMIT_DISABLED="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0")
        source = scratch / "source"
        source.mkdir()
        _copy_indexed_source(root, source, env)
        wheels = scratch / "wheels"
        _run([sys.executable, "-m", "build", "--no-isolation", "--wheel",
              "--outdir", wheels, source], scratch, env)
        candidates = list(wheels.glob("*.whl"))
        if len(candidates) != 1:
            raise ValueError("Test preparation requires exactly one built wheel")
        extracted = scratch / "extracted"
        _extract_assets(candidates[0], extracted)
        # Validate/build everything before touching the disposable test package.
        # Metadata is installed last, so a partial refresh cannot look complete.
        (package / "_artifact.json").unlink(missing_ok=True)
        for name in ("_resources",):
            target = package / name
            if target.exists():
                if not target.is_dir():
                    raise ValueError(f"Test asset destination is not a directory: {target}")
                shutil.rmtree(target)
            shutil.copytree(extracted / "claudlobby" / name, target)
        shutil.copy2(extracted / "claudlobby/_artifact.json", package / "_artifact.json")
    print(f"Prepared wheel resources in disposable test checkout: {root}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-checkout", required=True, type=Path,
                        help="explicitly permit asset writes to this disposable test checkout")
    args = parser.parse_args()
    prepare(args.disposable_checkout)
