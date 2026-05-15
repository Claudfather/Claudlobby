"""Claudron CLI — knowledge engine for Claude Code."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .vault import Vault, VaultError, detect, init, status
from .knowledge import lookup


# ── vault resolution ──────────────────────────────────────────────────


def _resolve_vault(args) -> Vault:
    """Resolve vault from --vault flag, CLAUDRON_VAULT env, or walk-up."""
    import os

    vault_hint = getattr(args, "vault", None)
    if vault_hint:
        vault = detect(Path(vault_hint))
    elif os.environ.get("CLAUDRON_VAULT"):
        vault = detect(Path(os.environ["CLAUDRON_VAULT"]))
    else:
        vault = detect()

    if vault is None:
        print(
            "no vault found\n"
            "  create one:  claudron init <path>\n"
            "  or specify:  claudron --vault <path> <command>",
            file=sys.stderr,
        )
        sys.exit(2)
    return vault


# ── commands ──────────────────────────────────────────────────────────


def cmd_init(args) -> int:
    path = Path(args.path).expanduser()
    try:
        root = init(path, adopt=args.adopt)
    except VaultError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"vault created at {root}")
    print("  _shared/knowledge/")
    print("  _shared/decisions/")
    print("  _shared/runbooks/")
    print("  projects/")
    return 0


def cmd_status(args) -> int:
    vault = _resolve_vault(args)
    info = status(vault, stale_days=getattr(args, "stale_days", 90))

    if args.json:
        print(json.dumps(info, indent=2))
        return 0

    print(f"vault: {info['root']}")
    print(f"docs:  {info['total_docs']}  stale: {info['total_stale']}")
    if info["fleets"]:
        print(f"fleets: {', '.join(info['fleets'])}")
    if info["projects"]:
        print(f"projects: {', '.join(info['projects'])}")
    print()
    for tier_name, tier_info in info["tiers"].items():
        if tier_info["docs"] > 0 or args.verbose:
            stale_str = f"  ({tier_info['stale']} stale)" if tier_info["stale"] else ""
            print(f"  {tier_name}: {tier_info['docs']} docs{stale_str}")

    if not info["index_present"]:
        print("\n  index: not built (will auto-build on first lookup)")
    elif not info["index_fresh"]:
        print("\n  index: stale (will auto-rebuild on next lookup)")

    for w in info["warnings"]:
        print(f"\n  warning: {w}")
    return 0


def cmd_lookup(args) -> int:
    vault = _resolve_vault(args)
    query = " ".join(args.query)
    results = lookup(
        query,
        vault,
        project=args.project,
        fleet=args.fleet,
        limit=args.limit,
        include_archived=args.include_archived,
        include_expired=args.include_expired,
    )

    if not results:
        if args.json:
            print(json.dumps({"query": query, "results": []}))
        else:
            print(f"no results for '{query}'")
        return 0

    if args.json:
        out = {
            "query": query,
            "results": [
                {
                    "title": r.doc.title,
                    "score": r.score,
                    "match_type": r.match_type,
                    "tier": r.doc.tier,
                    "path": str(r.doc.source_path.relative_to(vault.root)),
                    "tags": r.doc.tags,
                }
                for r in results
            ],
        }
        print(json.dumps(out, indent=2))
        return 0

    for r in results:
        rel = r.doc.source_path.relative_to(vault.root)
        tags = f"  [{', '.join(r.doc.tags)}]" if r.doc.tags else ""
        print(f"  [{r.score:3d}] {r.doc.title:<40s} {rel}{tags}")
    return 0


def cmd_version(_args) -> int:
    print(f"claudron {__version__}")
    return 0


# ── main ──────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="claudron",
        description="Knowledge engine for Claude Code",
    )
    parser.add_argument("--vault", help="Vault path (default: detect from CWD)")
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Scaffold a new vault")
    p_init.add_argument("path", help="Where to create the vault")
    p_init.add_argument(
        "--adopt", action="store_true", help="Adopt an existing non-empty directory"
    )

    # status
    p_status = sub.add_parser("status", help="Vault contents and health summary")
    p_status.add_argument("--json", action="store_true", help="JSON output")
    p_status.add_argument(
        "--stale-days", type=int, default=90, help="Staleness threshold in days"
    )
    p_status.add_argument(
        "-v", "--verbose", action="store_true", help="Show empty tiers"
    )

    # lookup
    p_lookup = sub.add_parser("lookup", help="Search vault knowledge")
    p_lookup.add_argument("query", nargs="+", help="Search terms")
    p_lookup.add_argument("--project", help="Project context for scoped search")
    p_lookup.add_argument("--fleet", help="Fleet context for scoped search")
    p_lookup.add_argument(
        "--limit", type=int, default=5, help="Max results (default: 5)"
    )
    p_lookup.add_argument("--json", action="store_true", help="JSON output")
    p_lookup.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived/completed docs",
    )
    p_lookup.add_argument(
        "--include-expired", action="store_true", help="Include expired docs"
    )

    # version
    sub.add_parser("version", help="Print version")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    dispatch = {
        "init": cmd_init,
        "status": cmd_status,
        "lookup": cmd_lookup,
        "version": cmd_version,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
