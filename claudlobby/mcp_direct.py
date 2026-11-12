"""Launch a pinned npx MCP server without npx's resident npm wrapper (#1604).

`npx -y <pkg>@<ver>` runs the server as the grandchild of an `npm exec` process
that stays resident for the server's whole life and does nothing. Measured on
the Pi, 2026-09-29: 41 wrappers holding 45 MB private and 1,388 MB of swap.
This module is the ONE answer to two questions, for the composer, `warm-cache`
and `doctor` alike, so the three cannot disagree:

* where a package's direct copy lives, and how one gets there (`install`)
* whether a server can launch from it, and if not, why (`direct_launch`)

The copy lives in a directory claudlobby owns, ``state/mcp/npm/<name>@<version>/``,
never in npm's npx cache. ``~/.npm/_npx/<hash>/`` is keyed by an npm-internal
hash (the Pi holds one package under two of them), and a composed path that
moves takes the bot's tools with it.

Every "no" here carries a reason instead of raising. The caller keeps the
server's npx launch, which cannot break a server, it only forgoes the saving,
and reports the reason so the forgone saving stays visible.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

#: Under the install root. Per version, so a pin bump never rewrites a copy a
#: running server has open.
INSTALL_ROOT = ("state", "mcp", "npm")
#: `--prefer-offline` resolves from npm's cache first, which is where the npx
#: warm left the same tarballs, so the tree matches what npx runs where it can.
NPM_INSTALL_FLAGS = ("--prefer-offline", "--no-audit", "--no-fund")
INSTALL_TIMEOUT_S = 300

#: The one reason `warm-cache` can fix. Every other reason is about the package.
NOT_INSTALLED = "not installed"

#: A plain node script, so `node <path>` runs exactly what npx's `sh -c <bin>`
#: does. A shebang with arguments (`env -S node --flag`) would lose them under
#: `node <path>`, and a shell script is not node at all: both keep npx.
_NODE_SHEBANG = re.compile(r"#!\s*(?:/usr/bin/env\s+node|/\S*/node)\s*")


def install_dir(root: Path, bare: str, version: str) -> Path:
    """The prefix one pinned package installs into."""
    return Path(root).joinpath(*INSTALL_ROOT) / f"{bare}@{version}"


def package_dir(root: Path, bare: str, version: str) -> Path:
    """The installed package itself, inside its prefix."""
    return install_dir(root, bare, version) / "node_modules" / bare


def pinned(g, spec: str | None) -> tuple[str, str] | None:
    """``(bare, version)`` for an EXACT pin, else None — `is_pinned`'s rule, so
    a range or a dist-tag (whatever the registry serves today) is never
    installed as if it were a version."""
    if not spec or not g.is_pinned("npx", spec):
        return None
    bare = g.bare_name("npx", spec)
    version = spec[len(bare) + 1 :]
    if not version or spec != f"{bare}@{version}":
        return None
    return bare, version


def entry_point(pkg_dir: Path, bare: str) -> tuple[Path | None, str]:
    """The script npx would run for an installed package, or why there is none.

    npx's own rule (libnpmexec ``getBinFromManifest``): a single bin target,
    else the bin named after the unscoped package; anything else is ambiguous
    and npx itself refuses it.
    """
    pkg_dir = Path(os.path.normpath(pkg_dir))
    try:
        manifest = json.loads((pkg_dir / "package.json").read_text())
    except FileNotFoundError:
        return None, NOT_INSTALLED
    except (OSError, ValueError) as exc:
        return None, f"unreadable package.json ({exc.__class__.__name__})"
    unscoped = bare.split("/", 1)[-1]
    bins = manifest.get("bin") if isinstance(manifest, dict) else None
    if isinstance(bins, str):
        bins = {unscoped: bins}
    if not isinstance(bins, dict) or not bins:
        return None, "package.json declares no bin"
    if len(set(bins.values())) == 1:
        rel = next(iter(bins.values()))
    elif unscoped in bins:
        rel = bins[unscoped]
    else:
        return None, f"cannot tell which bin npx would run ({', '.join(sorted(bins))})"
    if not isinstance(rel, str) or not rel:
        return None, "package.json bin entry is not a path"
    path = Path(os.path.normpath(pkg_dir / rel))
    if pkg_dir not in path.parents:
        return None, "bin entry points outside the package"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline().rstrip("\r\n")
    except OSError:
        return None, "entry point missing"
    if not _NODE_SHEBANG.fullmatch(first):
        return (
            None,
            f"entry point is not a plain node script (first line {first[:60]!r})",
        )
    return path, ""


def direct_launch(server: dict, root: Path, g) -> tuple[dict | None, str, str]:
    """For an npx server: ``(config, spec, "")`` launching it directly, or
    ``(None, spec, reason)`` when it must keep npx.

    The returned config is the server's own, with ``command``/``args`` swapped:
    ``env`` and every other key ride through untouched.
    """
    spec, rest = g.split_npx_args(server.get("args", []))
    if not spec:
        return None, "", "names no package"
    pin = pinned(g, spec)
    if pin is None:
        return None, spec, "not an exact version pin"
    entry, why = entry_point(package_dir(root, *pin), pin[0])
    if entry is None:
        return None, spec, why
    return {**server, "command": "node", "args": [str(entry), *rest]}, spec, ""


def install(root: Path, bare: str, version: str) -> tuple[str, str]:
    """Put ``<bare>@<version>`` under ``state/mcp/npm``, atomically.

    Returns ``(outcome, detail)``: ``present`` (a launchable copy is there),
    ``installed``, ``unusable`` (the package installs but cannot launch
    directly, so npx stays; ``detail`` says why) or ``failed``.

    npm installs into a temporary SIBLING that is renamed into place only once
    it holds a launchable entry point, so a torn install is never what a
    compose finds, and a failure leaves nothing behind.
    """
    final = install_dir(root, bare, version)
    entry, why = entry_point(package_dir(root, bare, version), bare)
    if entry is not None:
        return "present", str(entry)
    if final.exists():
        return "unusable", why
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{final.name}.", dir=final.parent))
    try:
        argv = [
            "npm",
            "install",
            "--prefix",
            str(tmp),
            *NPM_INSTALL_FLAGS,
            f"{bare}@{version}",
        ]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            return "failed", f"npm install timed out after {INSTALL_TIMEOUT_S}s"
        except FileNotFoundError:
            return "failed", "npm not found — install Node.js first"
        except (subprocess.SubprocessError, OSError) as exc:
            return "failed", f"npm install could not run: {exc}"
        if proc.returncode != 0:
            out = (proc.stderr or proc.stdout or "").strip().splitlines()
            last = out[-1].strip() if out else "(no output)"
            return "failed", f"npm install exited {proc.returncode}: {last}"
        entry, why = entry_point(tmp / "node_modules" / bare, bare)
        if entry is None:
            return "unusable", why
        try:
            os.rename(tmp, final)
        except OSError as exc:
            # A concurrent warm got there first: theirs is as good as ours.
            if entry_point(package_dir(root, bare, version), bare)[0] is not None:
                return "present", "installed concurrently"
            return "failed", f"could not move the install into place: {exc}"
        return "installed", str(package_dir(root, bare, version))
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
