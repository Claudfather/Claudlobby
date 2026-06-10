"""Scaffolding commands: new-bot, new-skill, new-guardrail."""

from __future__ import annotations

import logging
import re

from ._helpers import _resolve_paths

log = logging.getLogger("claudlobby")

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def cmd_new_skill(args) -> int:
    """Interactive (or flag-driven) skill scaffolding."""
    from ..newskill import interactive_collect, render_skill

    paths = _resolve_paths(args)

    if args.interactive or not args.name:
        name, description, argument_hint = interactive_collect()
    else:
        name = args.name
        description = args.description or ""
        argument_hint = args.argument_hint
        if not description:
            log.error("--description is required in non-interactive mode")
            return 1

    if not _SLUG_RE.match(name):
        log.error(
            "name must be lowercase, start with a letter, only [a-z0-9_-]: %r", name
        )
        return 1

    # Determine target directory (overlay if fleet, else base)
    if paths.overlay_library:
        skill_dir = paths.overlay_library / "skills" / name
    else:
        skill_dir = paths.base_skills / name

    if skill_dir.exists():
        log.error("skill already exists: %s", skill_dir)
        return 1

    content = render_skill(name, description, argument_hint)

    if args.dry_run:
        print(f"\n=== Would create {skill_dir}/SKILL.md ===\n")
        print(content)
        return 0

    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)
    log.info("created %s/SKILL.md", skill_dir)
    log.info("next: edit %s/SKILL.md to flesh out the skill behavior", skill_dir)
    return 0


def cmd_new_guardrail(args) -> int:
    """Interactive (or flag-driven) guardrail scaffolding."""
    from ..newguardrail import interactive_collect, render_guardrail

    paths = _resolve_paths(args)

    if args.interactive or not args.name:
        name, title, description = interactive_collect()
    else:
        name = args.name
        title = args.title or name.replace("-", " ").title()
        description = args.description or ""
        if not description:
            log.error("--description is required in non-interactive mode")
            return 1

    if not _SLUG_RE.match(name):
        log.error(
            "name must be lowercase, start with a letter, only [a-z0-9_-]: %r", name
        )
        return 1

    # Determine target directory (overlay if fleet, else base)
    if paths.overlay_library:
        guardrail_path = paths.overlay_library / "guardrails" / f"{name}.md"
    else:
        guardrail_path = paths.base_guardrails / f"{name}.md"

    if guardrail_path.exists():
        log.error("guardrail already exists: %s", guardrail_path)
        return 1

    content = render_guardrail(name, title, description)

    if args.dry_run:
        print(f"\n=== Would create {guardrail_path} ===\n")
        print(content)
        return 0

    guardrail_path.parent.mkdir(parents=True, exist_ok=True)
    guardrail_path.write_text(content)
    log.info("created %s", guardrail_path)
    log.info("next: edit %s to add specific rules and examples", guardrail_path)
    return 0


def cmd_new_bot(args) -> int:
    """Interactive (or flag-driven) bot creation."""
    from ..newbot import (
        NewBotInputs,
        insert_bot_stanza,
        interactive_collect,
        materialize_voice,
        render_stanza,
    )

    paths = _resolve_paths(args)

    # Pick mode. If --name given without --interactive, run non-interactive.
    if args.interactive or not args.name:
        inp = interactive_collect(paths)
    else:
        # Non-interactive: build from flags.
        def _csv(s: str | None) -> list[str] | None:
            if not s:
                return None
            return [x.strip() for x in s.split(",") if x.strip()]

        inp = NewBotInputs(
            name=args.name,
            expertise=_csv(args.expertise) or [],
            voice=args.voice,
            mission=args.mission,
            model=args.model,
            effort=args.effort,
            account=args.account,
            mcp=_csv(args.mcp),
            skills=_csv(args.skills),
            guardrails=_csv(args.guardrails),
            protocols=_csv(args.protocols),
            resources=_csv(args.resources),
            lessons=_csv(args.lessons),
            integrations=_csv(args.integrations),
            remote_control=False if args.no_remote_control else None,
            dangerously_skip_permissions=False
            if args.no_dangerously_skip_permissions
            else None,
            extra_flags=_csv(args.extra_flags),
            scope_org=args.scope_org,
            scope_repos=_csv(args.scope_repos),
            scope_snowflake_targets=_csv(args.scope_snowflake_targets),
            team=args.team,
            telegram_handle=args.telegram_handle,
            token_env=args.token_env
            or (
                f"TELEGRAM_TOKEN_{args.name.upper().replace('-', '_')}"
                if args.name
                else None
            ),
            require_mention=args.require_mention
            if args.require_mention is not None
            else True,
            chat_id=args.chat_id,
            startup_prompt=args.startup_prompt,
        )
        # If --voice-text passed, write it now.
        if args.voice_text:
            inp.voice = materialize_voice(paths, inp.name, None, args.voice_text)

    # Validation: required fields
    if not inp.name:
        log.error("--name is required")
        return 1
    if not inp.expertise:
        log.error("at least one --expertise is required")
        return 1

    # Render the stanza
    stanza = render_stanza(inp)

    print("\n=== Stanza to be added to fleet.yaml ===\n")
    print(stanza)

    if inp.team:
        log.info("  → will also be added to team '%s'.workers", inp.team)

    if args.dry_run:
        log.info(
            "--dry-run: no changes written. Stanza above would be inserted into fleet.yaml."
        )
        return 0

    # Confirm
    if not args.yes:
        ans = input("\nWrite to fleet.yaml? [Y/n]: ").strip().lower()
        if ans and ans not in ("y", "yes"):
            log.info("Aborted.")
            return 1

    # Backup fleet.yaml
    backup = paths.fleet_yaml.with_suffix(".yaml.bak")
    backup.write_text(paths.fleet_yaml.read_text())
    log.info("  ✓ Backup: %s", backup)

    # Insert
    new_text = insert_bot_stanza(paths.fleet_yaml, stanza, team=inp.team)
    paths.fleet_yaml.write_text(new_text)
    log.info("  ✓ Updated %s", paths.fleet_yaml)

    # Auto-generate
    if args.auto_generate:
        log.info("=== Running `claudlobby generate --bot %s` ===", inp.name)
        from ..composer import compose_bot
        from ..config import load_fleet

        fleet, _md = load_fleet(paths.fleet_yaml)
        bot = fleet.bots.get(inp.name)
        if bot is None:
            log.error("bot '%s' not found in fleet.yaml after insertion", inp.name)
            return 1
        out_dir = compose_bot(bot, fleet, paths)
        log.info("  ✓ Composed to %s", out_dir)

    # Next steps
    log.info("=== Next steps ===")
    log.info("  1. Review %s", paths.fleet_yaml)
    if inp.token_env:
        env_set = (
            paths.env_file.is_file() and inp.token_env in paths.env_file.read_text()
        )
        if env_set:
            log.info("  2. Token already in .env ✓")
        else:
            log.info("  2. Add %s=<your-token> to %s", inp.token_env, paths.env_file)
    log.info("  3. Run: claudlobby validate")
    if not args.auto_generate:
        log.info("  4. Run: claudlobby generate --bot %s", inp.name)
    log.info("  5. Install service:")
    log.info(
        "     # Linux: sudo ln -sf %s/%s.service /etc/systemd/system/",
        paths.bot_runtime(inp.name),
        inp.name,
    )
    log.info(
        "     #        sudo systemctl daemon-reload && sudo systemctl enable --now %s.service",
        inp.name,
    )
    log.info(
        "     # macOS: ln -sf %s/%s.plist ~/Library/LaunchAgents/",
        paths.bot_runtime(inp.name),
        inp.name,
    )
    return 0
