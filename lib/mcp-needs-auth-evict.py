#!/usr/bin/env python3
"""Evict provably-false "needs authentication" verdicts from Claude Code's cache.

`~/.claude/mcp-needs-auth-cache.json` is a **host-global** Claude Code cache of MCP
servers judged to need authentication. A server listed there is not spawned at all
by any session started afterwards. The verdict outlives the session that wrote it,
so it is **restart-immune**: a bot that restarts re-reads the cache and skips again.

That is a correct mechanism for a remote OAuth server. It is never correct for a
**local stdio** server, which has no authentication surface to fail — it is a
`command` the session execs on the same host. Such an entry can only be a
misclassified spawn failure, and it silently disables the server estate-wide.

Measured, 2026-08-25: an unclean host restart put 22 bots into a concurrent boot.
One bot's telegram plugin MCP child failed to come up in that storm; Claude Code
wrote `plugin:telegram:telegram` into the cache at 13:49:50. Every session started
after that instant skipped spawning the child entirely — no process, no poller, no
inbound Telegram — across three fleets, through repeated restarts, for 31 minutes.
The same file had disabled the Google Drive MCP host-wide two minutes earlier.

The two entries are the experiment: each one's write time predicts, for its own
server only, exactly which sessions skip it. That is why this is causal and not
coincidence, and it is why the remedy is eviction rather than a retry.

THE SAFETY PROPERTY, and the only thing in this file that really matters:

    Eviction is keyed on the server's DECLARATION, never on its name.

A declaration with `command` and no URL is local stdio and cannot need OAuth, so a
needs-auth verdict on it is false by construction. A declaration with a `url` (or
an http/sse transport) genuinely can, and evicting it would loop a real auth prompt
forever. Name-matching would collapse the two — `plugin:foo:bar` says nothing about
transport. So we resolve the declaration and read the field that ACTS.

Everything unresolvable is left alone. A key we cannot map to a declaration, a
plugin whose install path is missing, an unreadable manifest: all fail closed,
because the cost of wrongly keeping an entry is a server that stays down and
visible, while the cost of wrongly removing one is an auth loop nobody attributes
to this script.

Non-plugin keys are never touched. `claude.ai Google Drive` is not ours to judge.

Standalone stdlib module (`dispatch-overdue.py` precedent) so the discriminator
above is unit-testable; it could not be carried legibly in shell.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_CACHE = "~/.claude/mcp-needs-auth-cache.json"
DEFAULT_INSTALLED = "~/.claude/plugins/installed_plugins.json"

# Cache keys for plugin-provided servers look like `plugin:<plugin>:<server>`.
PLUGIN_KEY_PREFIX = "plugin:"

# A declaration naming any of these is remote: it can genuinely need OAuth.
REMOTE_TRANSPORTS = {"http", "sse", "websocket", "ws"}


def is_local_stdio(decl: object) -> bool:
    """True only when the declaration is unambiguously a local stdio server.

    Unambiguously: it execs a `command`, and nothing about it names a remote
    transport. Anything else — a URL, an http/sse type, a shape we do not
    recognise — returns False and is therefore left in the cache.
    """
    if not isinstance(decl, dict):
        return False
    if not isinstance(decl.get("command"), str) or not decl["command"].strip():
        return False
    if decl.get("url"):
        return False
    transport = decl.get("type") or decl.get("transport")
    if isinstance(transport, str) and transport.strip().lower() in REMOTE_TRANSPORTS:
        return False
    return True


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def plugin_install_paths(installed: object) -> dict[str, Path]:
    """Map plugin name -> install path, from `installed_plugins.json`.

    The install path is read from the manifest rather than globbed off a version
    directory: a glob picks whichever version sorts last, which is the stale one
    exactly when a plugin has just been updated.

    Keys there are `<plugin>@<marketplace>`; cache keys carry only `<plugin>`. A
    plugin name that appears under two marketplaces is therefore AMBIGUOUS, and
    ambiguous is dropped, not guessed — resolving to the wrong marketplace would
    read the wrong manifest and could clear a remote server's real verdict.
    """
    if not isinstance(installed, dict):
        return {}
    plugins = installed.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    seen: dict[str, Path | None] = {}
    for qualified, entries in plugins.items():
        name = str(qualified).split("@", 1)[0]
        if not isinstance(entries, list) or not entries:
            continue
        first = entries[0]
        if not isinstance(first, dict) or not first.get("installPath"):
            continue
        path = Path(str(first["installPath"]))
        if name in seen and seen[name] != path:
            seen[name] = None  # ambiguous across marketplaces
        else:
            seen.setdefault(name, path)
    return {n: p for n, p in seen.items() if p is not None}


def declaration_for(key: str, installs: dict[str, Path]) -> object | None:
    """Resolve a `plugin:<plugin>:<server>` cache key to its server declaration."""
    if not key.startswith(PLUGIN_KEY_PREFIX):
        return None
    parts = key.split(":")
    if len(parts) != 3:
        return None
    _, plugin, server = parts
    root = installs.get(plugin)
    if root is None:
        return None
    manifest = _read_json(root / ".mcp.json")
    if not isinstance(manifest, dict):
        return None
    servers = manifest.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    return servers.get(server)


def evictable(cache: dict, installs: dict[str, Path]) -> list[str]:
    """Cache keys that are false by construction. Order is stable for reporting."""
    out = []
    for key in cache:
        decl = declaration_for(str(key), installs)
        if decl is not None and is_local_stdio(decl):
            out.append(str(key))
    return out


def write_atomic(path: Path, payload: dict) -> None:
    """Replace the cache in one step, preserving its compact on-disk form.

    Compact because that is how Claude Code writes it; reformatting a host-global
    file shared by every bot on the box is a diff nobody asked for. The rename is
    atomic, so a concurrent reader sees the old file or the new one and never a
    truncated one — which matters here because a boot storm is precisely when this
    runs.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mcpauth-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--installed", default=DEFAULT_INSTALLED)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be evicted and write nothing",
    )
    args = ap.parse_args(argv)

    cache_path = Path(os.path.expanduser(args.cache))
    installed_path = Path(os.path.expanduser(args.installed))

    # An absent cache is the healthy state, not a problem to report.
    if not cache_path.exists():
        return 0

    cache = _read_json(cache_path)
    if not isinstance(cache, dict) or not cache:
        return 0

    installs = plugin_install_paths(_read_json(installed_path))
    targets = evictable(cache, installs)
    if not targets:
        return 0

    for key in targets:
        print(
            f"mcp-needs-auth-evict: {key} is local stdio — clearing false verdict",
            file=sys.stderr,
        )
    if args.dry_run:
        return 0

    for key in targets:
        cache.pop(key, None)
    try:
        write_atomic(cache_path, cache)
    except OSError as exc:
        # Never fail a boot over this. A stale verdict is a degraded bridge; a
        # nonzero exit here could be read by a caller as a reason not to launch.
        print(f"mcp-needs-auth-evict: could not rewrite cache: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
