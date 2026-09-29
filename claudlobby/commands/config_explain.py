"""Explain environment tiers and supported fleet/bot scalar origins without values."""

from __future__ import annotations

import re

from ..command_result import CommandFailure, CommandOutput


def _config_field(key, bot):
    from ..config import BotConfig, FleetConfig

    if key.startswith("fleet.defaults."):
        field = key.removeprefix("fleet.defaults.")
        if field in BotConfig.__dataclass_fields__:
            raise CommandFailure("unavailable", "provenance unsupported for this configuration declaration")
        raise CommandFailure("not_found", "configuration key is not a declared model field")
    if key.startswith("fleet."):
        field = key.removeprefix("fleet.")
        scope = "fleet"
    elif key.startswith("bot."):
        field = key.removeprefix("bot.")
        scope = "bot"
    elif bot and key in BotConfig.__dataclass_fields__:
        field, scope = key, "bot"
    elif key in FleetConfig.__dataclass_fields__:
        field, scope = key, "fleet"
    else:
        return None
    model = BotConfig if scope == "bot" else FleetConfig
    if field not in model.__dataclass_fields__:
        raise CommandFailure("not_found", "configuration key is not a declared model field")
    if scope == "bot" and not bot:
        raise CommandFailure("invalid_argument", "bot configuration requires --bot BOT")
    return scope, field


def dispatch(args) -> CommandOutput:
    from ..context import BotNotFoundError, generated_selectors, load_context, resolve_paths
    from ..env_register import ResolverUnavailable, build, exits_nonzero, format_report
    import yaml

    if args.key is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", args.key):
        raise CommandFailure("invalid_argument", "KEY must be an environment name or fleet/bot field")
    try:
        fleet, bot = generated_selectors(fleet=args.fleet, bot=args.bot,
                                        include_bot=True, seed=args.seed)
        paths = resolve_paths(root=args.root, fleet=fleet, seed=args.seed)
        context = load_context(paths, fleet=fleet, bot=bot)
    except BotNotFoundError as exc:
        raise CommandFailure("not_found", "bot is not declared in the selected fleet") from exc
    except FileNotFoundError as exc:
        raise CommandFailure("not_found", "selected fleet configuration was not found") from exc
    except RuntimeError as exc:
        raise CommandFailure("unavailable", "installed library package is unavailable",
                             hint="build and select a sealed release with packaged resources") from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise CommandFailure("conflict", "configuration or requested scope is invalid") from exc
    config_field = _config_field(args.key, bot) if args.key else None
    if config_field:
        from ..config import scalar_config_origin

        scope, field = config_field
        try:
            source, declaration = scalar_config_origin(
                paths.fleet_yaml, context.merged_defaults, field,
                bot=bot if scope == "bot" else None)
        except NotImplementedError as exc:
            raise CommandFailure("unavailable", "provenance unsupported for this configuration field") from exc
        effective = getattr(context.fleet.bots[bot] if scope == "bot" else context.fleet, field)
        key = f"{scope}.{field}"
        state = "unset" if effective is None else "set"
        data = {"fleet": context.fleet.name, "bot": bot if scope == "bot" else None,
                "key": key, "kind": "configuration", "source": source,
                "declaration": declaration, "state": state, "next_cursor": None}
        return CommandOutput(data, lines=(f"{key}: {state} from {declaration or 'built-in default'}",))
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
