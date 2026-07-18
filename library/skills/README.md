# library/skills/

Slash-command-style action packages. Each skill is a directory with a `SKILL.md` (the prompt) and optional helper files. The compositor symlinks selected skills into the bot's `.claude/skills/` directory.

## What belongs here

One directory per skill. Each must contain at least `SKILL.md` — the prompt Claude Code executes when the user invokes `/<skill-name>`. Helper scripts, templates, or config files go alongside it.

```
library/skills/
├── _telegram-formatting.md
├── dispatch/
│   └── SKILL.md
├── fleet-status/
│   └── SKILL.md
└── prs/
    └── SKILL.md
```

## SKILL.md frontmatter

`SKILL.md` uses Claude Code's own native frontmatter — `name:`, `description:`, `argument-hint:` — **not** the library-wide `title:`/`description:` convention used elsewhere in `library/`:

```yaml
---
name: status
description: "Manager self-diagnostic. Reports session health, MCP connectivity, tmux fleet state, and fleet-state ledger."
argument-hint: "[full|mcp|telegram]"
---

# Status
```

The H1 is a human-cased version of `name` (`status` → `# Status`, `fleet-pulse` → `# Fleet Pulse`), but unlike every other library category it is **not** stripped or demoted — `composer.py::link_skills` only creates a filesystem symlink into the bot's `.claude/skills/`; it never reads or parses `SKILL.md` content, so it never passes through `load_library_item`/`_demote_headings`. Claude Code reads the file natively at invocation time, H1 and all.

## Grant contract (`tool_grants:`)

A skill may declare the tools and commands it invokes as an additive grant contract in `SKILL.md` frontmatter, alongside the native fields:

```yaml
---
name: dispatch
description: "..."
tool_grants:
  - "mcp__github__*"      # an mcp__ glob (trailing * only)
  - "Bash(tmux *)"        # a scoped Bash(<command pattern>) grant
  - "Read"                # a bare tool name
---
```

Each entry is one of three shapes (the grant grammar): an `mcp__<server>__*` glob, a `Bash(<cmd> *)` pattern, or a bare CamelCase tool name. The compositor validates each entry's shape and warns on a malformed grant or an over-broad bare `Bash` (declare a scoped `Bash(<cmd> *)` instead). For a multi-file skill the contract lives on `SKILL.md` only — sibling files in the folder are ignored.

## Shared partials (leading underscore)

A flat `_<name>.md` file (leading underscore, no directory) at the top of `library/skills/` is a shared partial — prose meant to be pulled in by reference from inside one or more `SKILL.md` bodies, not an invocable skill itself. The only current example, `_telegram-formatting.md`, uses the library-wide `title:`/`description:` frontmatter (unlike its `SKILL.md` siblings) and is referenced via a relative markdown link, e.g. in `status/SKILL.md`: `` [_telegram-formatting.md](../_telegram-formatting.md) ``. This works specifically because skills are symlinked rather than copied (see Composition below) — the symlink at `<bot>/.claude/skills/status/SKILL.md` still points back into `library/skills/status/SKILL.md`, so the relative `../_telegram-formatting.md` resolves back to the shared library file. `fleet.yaml` never lists `_telegram-formatting` itself; it's pulled in implicitly by whichever skills link to it.

## Composition

Skills are symlinked, not copied — edits to `library/skills/<name>/SKILL.md` propagate live to every bot using that skill. Listed in fleet.yaml: `skills: [dispatch, fleet-status, prs]`.

## Naming

Lowercase, hyphenated directory names matching the slash command: `fleet-status/` → `/fleet-status`. A leading underscore (`_name.md`) marks a shared partial rather than an invocable skill (see Shared partials above).
