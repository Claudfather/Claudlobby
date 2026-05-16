"""`claudlobby new-bot` — interactive bot creation.

Two modes:

  Interactive:    `claudlobby new-bot`               (prompts for each field)
  Non-interactive: `claudlobby new-bot --name X ...` (all flags up front)

Both modes share the same backend. Always:
  1. Builds a BotConfig stanza
  2. Walks through @BotFather + chat-id setup if needed
  3. Edits fleet.yaml (in-place, preserving comments)
  4. Optionally runs `claudlobby generate --bot <name>`
  5. Prints next-step instructions

YAML editing: text-based, not round-tripped through PyYAML. Inserts
the new stanza as a properly-indented block at the end of `bots:`.
Preserves all comments and formatting.
"""

from __future__ import annotations
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

from .config import load_fleet
from .paths import Paths


# ----------------------------------------------------------------------
# Stanza assembly
# ----------------------------------------------------------------------


@dataclass
class NewBotInputs:
    """Everything we need to render a bot stanza."""

    name: str
    expertise: list[str]
    voice: str | None = None
    mission: str | None = None
    model: str | None = None
    effort: str | None = None
    account: str | None = None
    mcp: list[str] | None = None
    skills: list[str] | None = None
    guardrails: list[str] | None = None
    protocols: list[str] | None = None
    resources: list[str] | None = None
    lessons: list[str] | None = None
    integrations: list[str] | None = None
    remote_control: bool | None = None
    dangerously_skip_permissions: bool | None = None
    extra_flags: list[str] | None = None
    scope_org: str | None = None
    scope_repos: list[str] | None = None
    scope_snowflake_targets: list[str] | None = None
    team: str | None = None
    telegram_handle: str | None = None
    token_env: str | None = None
    require_mention: bool | None = None
    chat_id: str | None = None
    startup_prompt: str | None = None


def _yaml_list(items: list[str]) -> str:
    """`['a','b']` → `[a, b]` for compact YAML."""
    return "[" + ", ".join(items) + "]"


def _yaml_str(s: str) -> str:
    """Quote a string for YAML if it contains chars that would confuse the parser."""
    if any(c in s for c in ":#&*!|>'%@`,{}[]"):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def render_stanza(inp: NewBotInputs) -> str:
    """Render a bot stanza as 4-space-indented YAML text."""
    lines: list[str] = []
    lines.append(f"    {inp.name}:")
    lines.append(f"      expertise: {_yaml_list(inp.expertise)}")
    if inp.voice:
        lines.append(f"      voice: {inp.voice}")
    if inp.mission:
        lines.append(f"      mission: {_yaml_str(inp.mission)}")
    if inp.scope_org or inp.scope_repos or inp.scope_snowflake_targets:
        lines.append("      scope:")
        if inp.scope_org:
            lines.append(f"        org: {inp.scope_org}")
        if inp.scope_repos:
            lines.append(f"        repos: {_yaml_list(inp.scope_repos)}")
        if inp.scope_snowflake_targets:
            lines.append(
                f"        snowflake_targets: {_yaml_list(inp.scope_snowflake_targets)}"
            )
    if inp.account and inp.account != "default":
        lines.append(f"      account: {inp.account}")
    if inp.model:
        lines.append(f"      model: {inp.model}")
    if inp.effort:
        lines.append(f"      effort: {inp.effort}")
    if inp.remote_control is False:
        lines.append("      remote_control: false")
    if inp.dangerously_skip_permissions is False:
        lines.append("      dangerously_skip_permissions: false")
    if inp.extra_flags:
        lines.append(f"      extra_flags: {_yaml_list(inp.extra_flags)}")
    for label, vals in [
        ("skills", inp.skills),
        ("mcp", inp.mcp),
        ("integrations", inp.integrations),
        ("guardrails", inp.guardrails),
        ("protocols", inp.protocols),
        ("resources", inp.resources),
        ("lessons", inp.lessons),
    ]:
        if vals:
            lines.append(f"      {label}: {_yaml_list(vals)}")
    if any(
        [
            inp.telegram_handle,
            inp.token_env,
            inp.require_mention is not None,
            inp.chat_id,
        ]
    ):
        lines.append("      telegram:")
        if inp.telegram_handle:
            lines.append(f"        handle: {inp.telegram_handle}")
        if inp.token_env:
            lines.append(f"        token_env: {inp.token_env}")
        if inp.require_mention is not None:
            lines.append(f"        require_mention: {str(inp.require_mention).lower()}")
        if inp.chat_id:
            lines.append(f'        chat_id: "{inp.chat_id}"')
    if inp.startup_prompt:
        lines.append(f"      startup_prompt: {_yaml_str(inp.startup_prompt)}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# fleet.yaml editing — text-based, comment-preserving
# ----------------------------------------------------------------------


class FleetYamlEditError(Exception):
    pass


def insert_bot_stanza(
    fleet_yaml_path: Path, stanza_text: str, team: str | None = None
) -> str:
    """Insert a new bot stanza into fleet.yaml. Returns the diff for display.

    Strategy: find the `bots:` block, find its end (next top-level or fleet-level
    key at the same indent), insert the stanza just before that boundary.
    """
    if not fleet_yaml_path.is_file():
        raise FleetYamlEditError(f"fleet.yaml not found at {fleet_yaml_path}")

    text = fleet_yaml_path.read_text()
    lines = text.splitlines(keepends=True)

    # Find `bots:` line — must be at fleet-level (2-space indent under `fleet:`).
    bots_idx = None
    for i, line in enumerate(lines):
        if re.match(r"^  bots:\s*$", line):
            bots_idx = i
            break
    if bots_idx is None:
        raise FleetYamlEditError("could not find `  bots:` block in fleet.yaml")

    # Find end of bots: — first line at column 0-2 that's a new fleet-level key
    # (or end of file). Bot stanzas are indented at 4+ spaces.
    end_idx = len(lines)
    for j in range(bots_idx + 1, len(lines)):
        line = lines[j]
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        leading = len(line) - len(stripped)
        if leading <= 2:  # back to fleet-level → end of bots:
            end_idx = j
            break

    # Trim trailing blank lines from the bots block when computing insertion point.
    insert_at = end_idx
    while insert_at > bots_idx + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1

    # Compose insertion: blank line before the new stanza for readability.
    before = "".join(lines[:insert_at])
    after = "".join(lines[insert_at:])
    block = "\n" + stanza_text
    new_text = (
        before.rstrip()
        + "\n"
        + block
        + ("\n" if not after.startswith("\n") else "")
        + after.lstrip("\n")
    )

    # Optionally append to a team's workers list.
    if team:
        new_text = _add_to_team(new_text, team, _bot_name_from_stanza(stanza_text))

    return new_text


def _bot_name_from_stanza(stanza_text: str) -> str:
    """`    eng-1:\\n      ...` → `eng-1`."""
    m = re.match(r"^\s+([a-zA-Z0-9_-]+):\s*$", stanza_text.split("\n", 1)[0])
    if not m:
        raise FleetYamlEditError(
            f"can't parse bot name from stanza: {stanza_text[:80]!r}"
        )
    return m.group(1)


def _add_to_team(text: str, team: str, bot_name: str) -> str:
    """Append `bot_name` to `teams.<team>.workers:` list (compact form)."""
    lines = text.splitlines(keepends=True)
    in_teams = False
    in_target = False
    for i, line in enumerate(lines):
        if re.match(r"^  teams:\s*$", line):
            in_teams = True
            continue
        if in_teams and re.match(r"^  \w+:\s*$", line):
            in_teams = False
        if in_teams and re.match(rf"^    {re.escape(team)}:\s*$", line):
            in_target = True
            continue
        if in_target:
            m = re.match(r"^(\s+workers:\s*)\[([^\]]*)\]\s*$", line)
            if m:
                workers = [w.strip() for w in m.group(2).split(",") if w.strip()]
                if bot_name not in workers:
                    workers.append(bot_name)
                lines[i] = f"{m.group(1)}[{', '.join(workers)}]\n"
                return "".join(lines)
            if re.match(r"^    \w+:\s*$", line) and not line.startswith("      "):
                # next team — not found
                break
    # Not found / no compact workers list — leave as-is, warn caller via stderr.
    log.warning(
        "could not add '%s' to team '%s' (not found or non-compact format)",
        bot_name,
        team,
    )
    return "".join(lines)


# ----------------------------------------------------------------------
# Voice file creation
# ----------------------------------------------------------------------


def maybe_create_voice(
    paths: Paths, name: str, voice_arg: str | None, voice_text: str | None
) -> str | None:
    """Return the voice path (relative to root) to put in fleet.yaml, or None.

    - voice_arg = explicit path → use as-is
    - voice_text = inline text → write to voices/<name>.md, return that path
    - Both None → no voice
    """
    if voice_arg:
        return voice_arg
    if voice_text:
        voice_path = paths.base_voices / f"{name}.md"
        voice_path.parent.mkdir(parents=True, exist_ok=True)
        voice_path.write_text(
            f"---\nname: {name.title()}\n---\n\n{voice_text.strip()}\n"
        )
        return f"voices/{name}.md"
    return None


# ----------------------------------------------------------------------
# .env management — token write
# ----------------------------------------------------------------------


def write_token_to_env(env_path: Path, token_env: str, token: str) -> None:
    """Append/update TOKEN_ENV=token in .env. Creates file if missing."""
    if env_path.is_file():
        text = env_path.read_text()
    else:
        text = ""
    pattern = re.compile(rf"^{re.escape(token_env)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(f"{token_env}={token}", text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"{token_env}={token}\n"
    env_path.write_text(text)
    env_path.chmod(0o600)


# ----------------------------------------------------------------------
# Interactive prompts
# ----------------------------------------------------------------------


def _ask(prompt: str, default: str | None = None, allow_empty: bool = True) -> str:
    """Prompt with optional default. Empty input returns default."""
    suffix = f" [{default}]" if default else ""
    while True:
        v = input(f"{prompt}{suffix}: ").strip()
        if v:
            return v
        if default is not None:
            return default
        if allow_empty:
            return ""
        print("  (required)")


def _ask_yn(prompt: str, default: bool) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        v = input(f"{prompt}{suffix}: ").strip().lower()
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False


def _ask_pick(prompt: str, options: list[str], multi: bool = True) -> list[str]:
    """Show numbered options; user picks comma-separated indices or 'none'."""
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        print(f"  {i:>2}. {opt}")
    while True:
        v = (
            input("  pick (comma-separated numbers, 'none', or 'all'): ")
            .strip()
            .lower()
        )
        if v in ("", "none"):
            return []
        if v == "all":
            return list(options)
        try:
            picks = []
            for tok in v.split(","):
                idx = int(tok.strip()) - 1
                if 0 <= idx < len(options):
                    picks.append(options[idx])
            if not multi and len(picks) > 1:
                print("  (single selection)")
                continue
            return picks
        except ValueError:
            print("  (numbers only, e.g. '1,3,5')")


def _list_dir(p: Path, ext: str = ".md") -> list[str]:
    if not p.is_dir():
        return []
    return sorted(
        f.stem for f in p.glob(f"*{ext}") if not f.name.lower().startswith("readme")
    )


def _list_skills(p: Path) -> list[str]:
    if not p.is_dir():
        return []
    return sorted(d.name for d in p.iterdir() if d.is_dir())


def interactive_collect(paths: Paths) -> NewBotInputs:
    """Walk the user through every field. Returns a populated NewBotInputs."""
    print("\n=== claudlobby new-bot — interactive ===\n")

    name = _ask("Bot name (lowercase, no spaces, e.g. 'eng-1')", allow_empty=False)
    while not re.match(r"^[a-z][a-z0-9_-]*$", name):
        print("  (must be lowercase, start with a letter, only [a-z0-9_-])")
        name = _ask("Bot name", allow_empty=False)

    expertise = _ask_pick(
        "Expertise areas (which library/expertise/<name>.md files to compose):",
        _list_dir(paths.base_expertise),
        multi=True,
    )
    if not expertise:
        log.warning(
            "at least one expertise is required to generate. Continuing anyway."
        )

    voice_text = None
    voice_arg = None
    print("\nVoice (personality overlay, optional):")
    print("  1. Pick existing voice file from voices/")
    print("  2. Write a new voice (paste a paragraph)")
    print("  3. Skip — use bare expertise")
    choice = _ask("Choice (1/2/3)", default="3")
    if choice == "1":
        existing = (
            sorted(p.relative_to(paths.root) for p in paths.base_voices.rglob("*.md"))
            if paths.base_voices.is_dir()
            else []
        )
        existing_names = [str(p) for p in existing]
        if existing_names:
            picks = _ask_pick("Pick voice file:", existing_names, multi=False)
            voice_arg = picks[0] if picks else None
        else:
            print("  (no existing voices found)")
    elif choice == "2":
        print("  Paste a voice description (1-3 sentences). Hit Enter twice to finish.")
        lines = []
        empty_count = 0
        while empty_count < 1:
            line = input()
            if line == "":
                empty_count += 1
                if lines and empty_count >= 1:
                    break
                continue
            empty_count = 0
            lines.append(line)
        voice_text = "\n\n".join(lines)

    mission = _ask("Mission (one-paragraph charter)")
    model = _ask("Model (opus/sonnet/haiku)", default="opus") or None
    effort = _ask("Effort (max/default)", default="max") or None
    account = _ask("Account key (default/work/etc)", default="default")

    print()
    mcp = _ask_pick("MCP servers:", _list_dir(paths.base_mcp, ".json"))
    skills = _ask_pick("Skills:", _list_skills(paths.base_skills))
    guardrails = _ask_pick("Guardrails:", _list_dir(paths.base_guardrails))
    protocols = _ask_pick("Protocols:", _list_dir(paths.base_protocols))
    resources = _ask_pick("Resources:", _list_dir(paths.base_resources))
    lessons = _ask_pick("Lessons:", _list_dir(paths.base_lessons))

    print()
    remote_control = _ask_yn("--remote-control?", default=True)
    dangerously = _ask_yn("--dangerously-skip-permissions?", default=True)

    print("\nScope (optional — leave blank to skip):")
    scope_org = _ask("  Org (e.g. 'your-github-org')") or None
    scope_repos_str = _ask("  Repos (comma-separated)")
    scope_repos = [r.strip() for r in scope_repos_str.split(",") if r.strip()] or None

    team_picked = None
    try:
        fleet = load_fleet(paths.fleet_yaml)
        team_options = list(fleet.teams.keys())
    except (FileNotFoundError, ValueError, yaml.YAMLError) as e:
        log.warning("could not load fleet.yaml for team listing: %s", e)
        team_options = []
    if team_options:
        print()
        print("Add to team (or skip):")
        picks = _ask_pick("Existing teams:", team_options + ["(skip)"], multi=False)
        if picks and picks[0] != "(skip)":
            team_picked = picks[0]

    print("\n=== Telegram setup ===\n")
    handle_default = f"{name}_bot"
    handle = _ask("Telegram bot @handle (without @)", default=handle_default)
    token_env_default = f"TELEGRAM_TOKEN_{name.upper().replace('-', '_')}"
    token_env = _ask("Env var name for the token", default=token_env_default)
    require_mention = _ask_yn("Require @-mention to respond?", default=True)
    chat_id = _ask("Chat ID (override default group, optional)") or None

    print("\nWalk through @BotFather setup now?")
    print("  - Open https://t.me/BotFather in Telegram")
    print("  - Send: /newbot")
    print("  - Bot name: <whatever you want displayed>")
    print(f"  - Bot username: {handle}  (must end in '_bot' and be globally unique)")
    print("  - Copy the token BotFather replies with")
    do_token = _ask_yn("Have a token to paste now?", default=True)
    if do_token:
        token = _ask(
            "  Paste token (format: <numbers>:<letters_and_numbers>)", allow_empty=False
        )
        # Save to .env
        write_token_to_env(paths.env_file, token_env, token)
        log.info("  ✓ Saved %s to %s", token_env, paths.env_file)

    startup_prompt = (
        _ask("Startup prompt (sent once when the bot boots; leave blank for default)")
        or None
    )

    return NewBotInputs(
        name=name,
        expertise=expertise,
        voice=voice_arg,
        mission=mission or None,
        model=model,
        effort=effort,
        account=account,
        mcp=mcp or None,
        skills=skills or None,
        guardrails=guardrails or None,
        protocols=protocols or None,
        resources=resources or None,
        lessons=lessons or None,
        remote_control=False if not remote_control else None,
        dangerously_skip_permissions=False if not dangerously else None,
        scope_org=scope_org,
        scope_repos=scope_repos,
        team=team_picked,
        telegram_handle=handle or None,
        token_env=token_env,
        require_mention=require_mention,
        chat_id=chat_id,
        startup_prompt=startup_prompt,
    )


# Helper that the CLI uses to defer voice file creation until after stanza is built.
def materialize_voice(
    paths: Paths, name: str, voice_arg: str | None, voice_text: str | None
) -> str | None:
    return maybe_create_voice(paths, name, voice_arg, voice_text)
