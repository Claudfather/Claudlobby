"""Launch a pinned npx MCP server without npx's resident npm wrapper (#1604).

`npx -y <pkg>@<ver>` runs the server as the grandchild of an `npm exec` process
that stays resident for the server's whole life and does nothing. Measured on
the Pi, 2026-10-04: 42 wrappers holding 61 MiB in RAM and 1,439 MiB of swap.
This module is the ONE answer to three questions, for the composer, `config
plan`, `host cache warm`, `doctor` and `start-bot.sh` alike, so they cannot
disagree:

* which copies an armed bot needs (`armed_specs`), where each lives, and how
  one gets there (`install`)
* whether a server can launch from it, and if not, why (`direct_launch`)
* whether a composed `.mcp.json` launches a copy that is gone
  (`missing_copies`), and what brings it back (`remedy`)

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
import sys
import tempfile
from pathlib import Path

#: Under the install root. Per version, so a pin bump never rewrites a copy a
#: running server has open.
INSTALL_ROOT = ("state", "mcp", "npm")
#: `--prefer-offline` resolves from npm's cache first, which is where the npx
#: warm left the same tarballs, so the tree matches what npx runs where it can.
NPM_INSTALL_FLAGS = ("--prefer-offline", "--no-audit", "--no-fund")
INSTALL_TIMEOUT_S = 300

#: The one reason an install can fix. Every other reason is about the package.
NOT_INSTALLED = "not installed"
#: An installed copy whose entry script is gone: damage after the install,
#: which is atomic, so `install` sets it aside and installs again.
ENTRY_MISSING = "entry point missing"

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
        return None, ENTRY_MISSING
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


def armed_specs(fleet, paths, g) -> dict[str, tuple[str, str]]:
    """``spec -> (bare, version)`` for each exactly pinned npx package an ARMED
    bot of *fleet* launches: the copies `host cache warm` and `config plan`
    install, collected once so the two cannot install different sets. The walk
    is the grammar's own (`declared_packages`), whose npx spec is the one
    `direct_launch` reads, so what is installed is what composition looks for."""
    fragments = sorted({
        str(path) for bot in fleet.bots.values() if bot.mcp_direct_launch
        for entry in bot.mcp
        if (path := paths.find_library_file("mcp", entry.name, ".json")) is not None})
    found: dict[str, tuple[str, str]] = {}
    for _fragment, _key, runtime, spec, _bare, _pinned, _argv in g.declared_packages(fragments):
        if runtime == "npx" and (pin := pinned(g, spec)) is not None:
            found[spec] = pin
    return found


def install_armed(fleet_paths) -> list[tuple[str, str, str]]:
    """Install every copy an armed bot of these fleets launches directly, and
    return ``(spec, outcome, detail)`` for each. The step staging's callers run
    BEFORE they stage (`config plan`, `fleet setup`, `bot move`): composition
    writes `node <copy>` only for a copy that is there, so without it the first
    plan on a fresh host composes the npx fallback. A fleet this cannot load is
    staging's to report; a failed install leaves that package's servers on npx,
    which composition's own warning names."""
    from .config import load_fleet  # lazy: start-bot imports this module bare
    from .mcp_grammar import grammar

    specs: dict[str, tuple[str, str]] = {}
    for paths in fleet_paths:
        try:
            # Staging's own parse, without load_context's --fleet identity check: a fleet
            # whose directory is not named as it declares itself is still composed, so
            # its copies must still be installed.
            fleet, _defaults = load_fleet(paths.fleet_yaml, projects_yaml=paths.projects_yaml)
            specs.update(armed_specs(fleet, paths, grammar(paths)))
        except Exception:  # noqa: BLE001 — staging reports this fleet; this step only forgoes copies
            continue
    return [(spec, *install(fleet_paths[0].root, bare, version))
            for spec, (bare, version) in sorted(specs.items())]


def install(root: Path, bare: str, version: str) -> tuple[str, str]:
    """Put ``<bare>@<version>`` under ``state/mcp/npm``, atomically.

    Returns ``(outcome, detail)``: ``present`` (a launchable copy is there),
    ``installed``, ``unusable`` (the package installs but cannot launch
    directly, so npx stays; ``detail`` says why) or ``failed``.

    npm installs into a temporary SIBLING that is renamed into place only once
    it is whole, so a torn install is never what a compose finds, and a failure
    leaves nothing behind. An install that cannot launch directly is kept: the
    next plan or warm then answers ``unusable`` without running npm, and
    composition names the package's own reason. A copy that lost its manifest
    or its entry script after it landed is replaced by a fresh install, so one
    warm restores a copy a composed file names.
    """
    final = install_dir(root, bare, version)
    entry, why = entry_point(package_dir(root, bare, version), bare)
    if entry is not None:
        return "present", str(entry)
    if final.exists() and why not in (NOT_INSTALLED, ENTRY_MISSING):
        return "unusable", why
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{final.name}.", dir=final.parent))
    try:
        argv = ["npm", "install", "--prefix", str(tmp), *NPM_INSTALL_FLAGS, f"{bare}@{version}"]
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
        if final.exists():
            # A concurrent install may have landed while npm ran; theirs is as
            # good as ours. Otherwise the copy there is the damaged one, and a
            # whole replacement now stands ready.
            if entry_point(package_dir(root, bare, version), bare)[0] is not None:
                return "present", "installed concurrently"
            shutil.rmtree(final, ignore_errors=True)
        try:
            os.rename(tmp, final)
        except OSError as exc:
            if entry_point(package_dir(root, bare, version), bare)[0] is not None:
                return "present", "installed concurrently"
            return "failed", f"could not move the install into place: {exc}"
        if entry is None:
            return "unusable", why
        return "installed", str(package_dir(root, bare, version))
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def composed_copies(mcp) -> list[tuple[str, Path]]:
    """``(server, entry)`` for each server a composed ``.mcp.json`` launches as
    ``node <entry>`` from a copy under ``state/mcp/npm``.

    Judged by the path's SEGMENTS, not by a prefix of one spelling of the data
    root: a file composed under another spelling of the root still names a
    copy. A ``node`` entry anywhere else (the global-binary swap) is not a copy
    this module vouches for."""
    servers = mcp.get("mcpServers") if isinstance(mcp, dict) else None
    if not isinstance(servers, dict):
        return []
    found = []
    for name, server in servers.items():
        if not isinstance(server, dict) or server.get("command") != "node":
            continue
        args = server.get("args") or []
        if not isinstance(args, list) or not args:
            continue
        entry = Path(str(args[0]))
        parts, n = entry.parts, len(INSTALL_ROOT)
        if any(parts[i:i + n] == INSTALL_ROOT for i in range(len(parts))):
            found.append((name, entry))
    return found


def missing_copies(mcp) -> list[tuple[str, Path]]:
    """The composed copies whose entry script is gone. Each is a server that
    fails to start at the bot's next session until the copy is back, the one
    predicate `doctor` and `start-bot.sh` both read. Its bound: only the entry
    script is checked, so a copy whose script survived a partial deletion of
    its `node_modules` passes."""
    return [(name, entry) for name, entry in composed_copies(mcp) if not entry.is_file()]


def remedy(fleet: str) -> str:
    """What brings a missing copy back. The copy's path is fixed by its pin, so
    a warm reinstalls it where the composed file looks and no new plan is
    needed; a bot whose key is off no longer counts as armed for the warm, so
    a new plan composes its npx launch instead."""
    return (f"run `claudlobby --fleet {fleet or '<fleet>'} host cache warm` to reinstall it at"
            " the same path, then restart the bot (a bot with mcp_direct_launch off needs"
            " a new config plan instead)")


def missing_line(count: int, listed: str, fleet: str) -> str:
    """The one sentence for composed servers whose copy is gone, with its
    remedy: `start-bot.sh`'s notice and doctor's rung both say it."""
    return (f"{count} MCP server(s) will not start: the state/mcp copy each launches is"
            f" gone: {listed}. Remedy: {remedy(fleet)}")


def install_fix(fleet: str) -> str:
    """What to do about a server that kept npx because its copy is not installed."""
    return ("config plan installs a missing copy before it composes; run `claudlobby"
            f" --fleet {fleet or '<fleet>'} host cache warm` to see why one did not install,"
            " then stage and activate a new config plan")


def _main(argv: list[str]) -> int:
    """``python -I -B -m claudlobby.mcp_direct notice <.mcp.json> <bot> <fleet>``
    prints the notice for the composed copies that are gone, or nothing.

    `start-bot.sh` runs it before every session start. A file it cannot read
    prints nothing (rc 2), and it never raises, so a boot it checks is never a
    boot it blocks."""
    if len(argv) != 4 or argv[0] != "notice":
        print("usage: python -m claudlobby.mcp_direct notice <.mcp.json> <bot> <fleet>",
              file=sys.stderr)
        return 2
    try:
        mcp = json.loads(Path(argv[1]).read_text())
    except (OSError, ValueError):
        return 2
    missing = missing_copies(mcp)
    if missing:
        listed = ", ".join(f"{name} ({entry})" for name, entry in missing)
        print(f"{argv[2]}: {missing_line(len(missing), listed, argv[3])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
