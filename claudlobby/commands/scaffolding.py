"""Source scaffolding commands; bot create does not compose or enroll."""

from __future__ import annotations

import logging
import re

from ._helpers import _resolve_paths

log = logging.getLogger("claudlobby")

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def cmd_new_bot(args):
    """Author one bot declaration through the existing stanza and voice owners."""
    import sys
    import yaml
    from ..command_result import CommandFailure, CommandOutput
    from ..context import generated_selectors, load_context, resolve_paths
    from ..newbot import (FleetYamlEditError, NewBotInputs, insert_bot_stanza,
                          interactive_collect, materialize_voice, render_stanza,
                          write_token_to_env)
    from ..paths import InvalidPathSelector

    if args.seed:
        raise CommandFailure("conflict", "seed fleet source cannot be edited")
    if args.json and args.interactive:
        raise CommandFailure("invalid_argument", "JSON bot creation requires complete flags")
    if args.voice and args.voice_text:
        raise CommandFailure("invalid_argument", "choose --voice or --voice-text")
    if args.interactive or (not args.name and not args.json and sys.stdin.isatty()):
        interactive = True
    else:
        interactive = False
        if not args.name or not args.expertise:
            raise CommandFailure("invalid_argument", "--name and --expertise are required")
        if not args.dry_run and not args.yes and (args.json or not sys.stdin.isatty()):
            raise CommandFailure("invalid_argument", "noninteractive creation requires --yes or --dry-run")

    try:
        fleet, _ = generated_selectors(fleet=args.fleet, seed=False)
        paths = resolve_paths(root=args.root, fleet=fleet, seed=False)
        context = load_context(paths, fleet=fleet)
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid root or fleet selector") from exc
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise CommandFailure("conflict", "fleet source cannot be loaded") from exc

    if interactive:
        try:
            inp = interactive_collect(paths)
        except (EOFError, KeyboardInterrupt) as exc:
            raise CommandFailure("invalid_argument", "interactive bot creation was interrupted") from exc
    else:
        def csv(value):
            return [item.strip() for item in value.split(",") if item.strip()] if value else None

        inp = NewBotInputs(
            name=args.name, expertise=csv(args.expertise) or [], voice=args.voice,
            mission=args.mission, model=args.model, effort=args.effort, account=args.account,
            mcp=csv(args.mcp), skills=csv(args.skills), guardrails=csv(args.guardrails),
            protocols=csv(args.protocols), resources=csv(args.resources),
            lessons=csv(args.lessons), integrations=csv(args.integrations),
            remote_control=False if args.no_remote_control else None,
            dangerously_skip_permissions=True if args.dangerously_skip_permissions else None,
            extra_flags=csv(args.extra_flags), scope_org=args.scope_org,
            scope_repos=csv(args.scope_repos),
            scope_snowflake_targets=csv(args.scope_snowflake_targets), team=args.team,
            telegram_handle=args.telegram_handle,
            token_env=args.token_env or (
                f"TELEGRAM_TOKEN_{args.name.upper().replace('-', '_')}"
                if args.telegram_handle else None),
            require_mention=(args.require_mention if args.require_mention is not None
                             else True if args.telegram_handle else None),
            chat_id=args.chat_id, startup_prompt=args.startup_prompt,
        )
        if args.voice_text:
            inp.voice_text = args.voice_text
            inp.voice = f"voices/{inp.name}.md"

    if not inp.name or not _SLUG_RE.fullmatch(inp.name) or not inp.expertise:
        raise CommandFailure("invalid_argument", "bot name or expertise is invalid")
    if inp.name in context.fleet.bots:
        raise CommandFailure("conflict", f"bot is already declared: {inp.name}")
    if inp.team and inp.team not in context.fleet.teams:
        raise CommandFailure("invalid_argument", f"team is not declared: {inp.team}")
    try:
        paths.assert_writable(paths.fleet_yaml)
        backup = paths.fleet_yaml.with_suffix(".yaml.bak")
        paths.assert_writable(backup)
        if paths.fleet_yaml.resolve() != paths.fleet_yaml or backup.resolve() != backup:
            raise CommandFailure("conflict", "fleet source or backup is redirected")
        if inp.voice_text:
            voice_path = paths.overlay_voices / f"{inp.name}.md"
            paths.assert_writable(voice_path)
            if voice_path.resolve() != voice_path or voice_path.exists() or voice_path.is_symlink():
                raise CommandFailure("conflict", "fleet voice source already exists")
        if inp.telegram_token:
            paths.assert_writable(paths.env_file)
            if paths.env_file.resolve() != paths.env_file:
                raise CommandFailure("conflict", "fleet token tier is redirected")
        stanza = render_stanza(inp)
        new_text = insert_bot_stanza(paths.fleet_yaml, stanza, team=inp.team)
        yaml.safe_load(new_text)
    except (ValueError, OSError, FleetYamlEditError, yaml.YAMLError) as exc:
        raise CommandFailure("conflict", "bot declaration cannot be authored in the selected fleet") from exc

    guidance = (f"Review {paths.fleet_yaml}, then stage with `claudlobby --root {paths.root} "
                f"config plan --release <release-id>` and activate the reviewed plan with "
                f"`claudlobby --root {paths.root} host activate <plan-id> "
                "--install-directory <native-user-unit-dir>` from an operator shell.")
    data = {"fleet": context.fleet.name, "bot": inp.name, "fleet_yaml": str(paths.fleet_yaml),
            "stanza": stanza, "dry_run": args.dry_run, "written": False,
            "voice_path": str(paths.overlay_voices / f"{inp.name}.md") if inp.voice_text else None,
            "next_step": guidance}
    if args.dry_run:
        return CommandOutput(data, lines=(stanza.rstrip(), "dry run: no source written", guidance))
    if not args.yes and input("\nWrite to fleet.yaml? [Y/n]: ").strip().lower() not in ("", "y", "yes"):
        raise CommandFailure("conflict", "bot creation declined; no source written")

    try:
        backup.write_text(paths.fleet_yaml.read_text())
        if inp.voice_text:
            materialize_voice(paths, inp.name, None, inp.voice_text)
        if inp.telegram_token:
            write_token_to_env(paths.env_file, inp.token_env, inp.telegram_token)
        paths.fleet_yaml.write_text(new_text)
    except OSError as exc:
        raise CommandFailure("unavailable", "bot source write did not complete; inspect authored files") from exc
    data["written"] = True
    return CommandOutput(data, lines=(stanza.rstrip(), f"updated {paths.fleet_yaml}", guidance))
