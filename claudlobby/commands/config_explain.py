"""Explain environment configuration from its existing declaration/tier owner."""

from __future__ import annotations

import re

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..context import BotNotFoundError, generated_selectors, load_context, resolve_paths
    from ..env_register import ResolverUnavailable, build, exits_nonzero, format_report
    import yaml

    if args.key is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.key):
        raise CommandFailure("invalid_argument", "KEY must be an environment variable name")
    try:
        fleet, bot = generated_selectors(fleet=args.fleet, bot=args.bot,
                                        include_bot=True, seed=args.seed)
        paths = resolve_paths(root=args.root, fleet=fleet, seed=args.seed)
        context = load_context(paths, fleet=fleet, bot=bot)
    except BotNotFoundError as exc:
        raise CommandFailure("not_found", "bot is not declared in the selected fleet") from exc
    except FileNotFoundError as exc:
        raise CommandFailure("not_found", "selected fleet configuration was not found") from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise CommandFailure("conflict", "configuration or requested scope is invalid") from exc
    try:
        register = build(context.fleet, paths, bot=bot, key=args.key)
    except KeyError as exc:
        raise CommandFailure("not_found", "key is neither declared nor assigned in the resolved tiers") from exc
    except ResolverUnavailable as exc:
        raise CommandFailure("unavailable", "environment tier resolver is unavailable",
                             hint="inspect claudlobby host env tiers --help") from exc

    data = {"fleet": context.fleet.name, "bot": register.bot, "key": args.key,
            "kind": "environment", "source": "authored_configuration",
            "tiers": [{"tier": tier, "path": path, "state": state}
                      for tier, path, state in register.tiers],
            "items": [row._asdict() for row in register.rows], "next_cursor": None,
            "undeclared": list(register.undeclared)}
    report = format_report(register)
    if exits_nonzero(register):
        raise CommandFailure("conflict", "an empty assignment shadows a nonempty upstream value",
                             data=data, hint=report)
    return CommandOutput(data, lines=(report,))
