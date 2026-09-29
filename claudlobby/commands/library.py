"""List installed library components and author skill/guardrail overlays."""

from __future__ import annotations

import re

from ..command_result import CommandFailure, CommandOutput


_SLUG = re.compile(r"[a-z][a-z0-9_-]*\Z")
_MARKDOWN = (
    ("expertise", "Expertise"), ("integrations", "Integrations"),
    ("protocols", "Protocols"), ("guardrails", "Guardrails"),
    ("resources", "Resources"), ("lessons", "Lessons"),
    ("post_actions", "Post-actions"),
)


def _paths(args):
    from ..context import resolve_paths
    from ..paths import InvalidPathSelector

    try:
        return resolve_paths(root=args.root, fleet=args.fleet, seed=args.seed)
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "library root or fleet selector is invalid") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise CommandFailure("unavailable", "library package or data root is unavailable") from exc


def _walk_markdown(paths, kind: str) -> list[dict]:
    found: dict[str, str] = {}
    for directory in paths.library_search_dirs(kind):
        if not directory.is_dir():
            continue
        source = "overlay" if paths.overlay_library and directory == paths.overlay_library / kind else "base"
        for path in sorted(directory.rglob("*.md")):
            if path.stem.lower().startswith("readme"):
                continue
            name = path.relative_to(directory).with_suffix("").as_posix()
            found.setdefault(name, source)
    return [{"name": name, "source": source} for name, source in sorted(found.items())]


def _walk_skills(paths) -> list[dict]:
    found: dict[str, str] = {}
    for directory in paths.library_search_dirs("skills"):
        if not directory.is_dir():
            continue
        source = "overlay" if paths.overlay_library and directory == paths.overlay_library / "skills" else "base"
        for child in sorted(directory.rglob("*")):
            if child.is_dir() and (child / "SKILL.md").is_file():
                found.setdefault(child.relative_to(directory).as_posix(), source)
    return [{"name": name, "source": source} for name, source in sorted(found.items())]


def _walk_voices(paths) -> list[dict]:
    found: dict[str, str] = {}
    for directory, source in ((paths.overlay_voices, "overlay"), (paths.base_voices, "base")):
        if directory and directory.is_dir():
            for path in sorted(directory.rglob("*.md")):
                found.setdefault(path.relative_to(directory).as_posix(), source)
    return [{"name": name, "source": source} for name, source in sorted(found.items())]


def _list(paths) -> CommandOutput:
    categories = {kind: _walk_markdown(paths, kind) for kind, _label in _MARKDOWN}
    categories["mcp"] = ([{"name": path.stem, "source": "base"}
                           for path in sorted(paths.base_mcp.glob("*.json"))]
                         if paths.base_mcp.is_dir() else [])
    categories["skills"] = _walk_skills(paths)
    categories["tools"] = [{"name": name, "source": "overlay" if overlay else "base"}
                           for name, overlay in sorted(paths.library_dir_names("tools", "tool.yaml").items())]
    categories["voices"] = _walk_voices(paths)
    labels = (*_MARKDOWN, ("mcp", "MCP fragments (base only)"),
              ("skills", "Skills"), ("tools", "Tools"), ("voices", "Voices"))
    lines = []
    for kind, label in labels:
        lines.append(f"{label}:")
        for item in categories[kind]:
            name = f"voices/{item['name']}" if kind == "voices" else item["name"]
            lines.append(f"  {name}{' (override)' if item['source'] == 'overlay' else ''}")
    overlay = None
    if paths.fleet_dir:
        overlay = (paths.fleet_dir.relative_to(paths.root).as_posix()
                   if paths.fleet_dir.is_relative_to(paths.root) else str(paths.fleet_dir))
    lines.append(f"[fleet overlay: {overlay}]" if overlay else
                 "[no fleet overlay — root mode. Use --fleet <name> for overlay mode.]")
    items = [{"kind": kind, **item} for kind, _label in labels for item in categories[kind]]
    return CommandOutput({"items": items, "next_cursor": None, "fleet_overlay": overlay},
                         lines=tuple(lines))


def _create(args, paths) -> CommandOutput:
    if args.json and args.interactive:
        raise CommandFailure("invalid_argument", "JSON library create cannot prompt")
    if args.kind == "skill" and args.title is not None:
        raise CommandFailure("invalid_argument", "--title applies only to guardrails")
    if args.kind == "guardrail" and args.argument_hint is not None:
        raise CommandFailure("invalid_argument", "--argument-hint applies only to skills")

    if args.interactive or (not args.name and not args.json):
        if args.kind == "skill":
            from ..newskill import interactive_collect
            name, description, argument_hint = interactive_collect()
            title = None
        else:
            from ..newguardrail import interactive_collect
            name, title, description = interactive_collect()
            argument_hint = None
    else:
        name, description = args.name, args.description
        title, argument_hint = args.title, args.argument_hint
        if not name or not description:
            raise CommandFailure("invalid_argument", "--name and --description are required without prompts")
    if not _SLUG.fullmatch(name):
        raise CommandFailure("invalid_argument", "name must be lowercase and use only letters, numbers, _ or -")
    if args.kind == "skill":
        from ..newskill import render_skill
        target = paths.overlay_library / "skills" / name / "SKILL.md"
        content = render_skill(name, description, argument_hint)
    else:
        from ..newguardrail import render_guardrail
        target = paths.overlay_library / "guardrails" / f"{name}.md"
        content = render_guardrail(name, title or name.replace("-", " ").title(), description)
    try:
        target = paths.assert_writable(target)
    except ValueError as exc:
        raise CommandFailure("conflict", "library destination escapes the writable overlay") from exc
    if target.exists() or (args.kind == "skill" and target.parent.exists()):
        raise CommandFailure("conflict", "library item already exists in this overlay")
    if not args.dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    data = {"kind": args.kind, "name": name, "path": str(target),
            "created": not args.dry_run, "preview": args.dry_run}
    if args.dry_run:
        data["content"] = content
    lines = ((f"Would create {target}:\n{content}" if args.dry_run else f"Created {target}"),)
    return CommandOutput(data, lines=lines)


def dispatch(args) -> CommandOutput:
    paths = _paths(args)
    return _list(paths) if args.public_command == "library.list" else _create(args, paths)
