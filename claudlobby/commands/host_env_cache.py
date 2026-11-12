"""Host env-tier inspection and MCP cache warming through their existing owners."""

from __future__ import annotations

from pathlib import Path

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..context import load_context, resolve_paths

    try:
        paths = resolve_paths(root=Path(args.root).expanduser() if args.root else None,
                              fleet=args.fleet, seed=args.seed)
        if args.public_command == "host.env.tiers":
            from ..env_tiers import ResolverUnavailable, read_tiers

            try:
                if args.bot:
                    load_context(paths, bot=args.bot)
                tiers = read_tiers(paths, bot_name=args.bot)
            except ResolverUnavailable as exc:
                raise CommandFailure("unavailable", f"env tier resolver unavailable: {exc}") from exc
            rows = [{"tier": tier.tier, "path": str(tier.path) if tier.path else None,
                     "state": tier.state} for tier in tiers]
            return CommandOutput({"tiers": rows, "fleet": args.fleet, "bot": args.bot},
                                 lines=tuple(f"{row['tier']}\t{row['path'] or '-'}\t{row['state']}"
                                             for row in rows))

        from .core import cmd_warm_cache

        context = load_context(paths)
        summary: dict = {}
        rc = cmd_warm_cache(args, paths=paths, fleet=context.fleet, summary=summary)
        if rc:
            raise CommandFailure("unavailable", "MCP cache warm did not complete for every package",
                                 data=summary)
        packages = summary.get("packages", [])
        count = len(packages)
        action = "identified" if args.dry_run else "processed"
        return CommandOutput(summary, lines=(f"{action} {count} MCP package(s); "
                                             f"{len(summary.get('unreadable', []))} unreadable server(s)",))
    except (ValueError, FileNotFoundError) as exc:
        raise CommandFailure("invalid_argument", f"invalid host scope or fleet declaration: {exc}") from exc
