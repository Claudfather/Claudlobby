# library/skills/

Slash-command-style action packages. Each skill is a directory with a `SKILL.md` (the prompt) and optional helper files. The compositor symlinks selected skills into the bot's `.claude/skills/` directory.

## What belongs here

One directory per skill. Each must contain at least `SKILL.md` — the prompt Claude Code executes when the user invokes `/<skill-name>`. Helper scripts, templates, or config files go alongside it.

```
library/skills/
├── dispatch/
│   └── SKILL.md
├── fleet-status/
│   └── SKILL.md
└── prs/
    └── SKILL.md
```

## Composition

Skills are symlinked, not copied — edits to `library/skills/<name>/SKILL.md` propagate live to every bot using that skill. Listed in fleet.yaml: `skills: [dispatch, fleet-status, prs]`.

## Naming

Lowercase, hyphenated directory names matching the slash command: `fleet-status/` → `/fleet-status`.
