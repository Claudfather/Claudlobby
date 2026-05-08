# library/protocols/

Reusable communication and workflow patterns — how bots coordinate, report status, handle reviews, and manage context. Each protocol is appended to the bot's CLAUDE.md as a self-contained section.

## What belongs here

One `.md` file per protocol. Use YAML frontmatter with `title:` (and optionally `description:`), followed by an H1 heading and the protocol body. Protocols should be composable — avoid hard dependencies between them.

## Composition

Protocols accumulate like guardrails: fleet defaults plus bot-level additions.

```yaml
defaults:
  protocols: [report-back, telegram-routing]
bots:
  lead:
    protocols: [dispatch, consensus-loop]  # added on top of defaults
```

## Naming

Lowercase, hyphenated, action-oriented: `report-back.md`, `dispatch.md`, `telegram-routing.md`.
