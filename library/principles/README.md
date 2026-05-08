# library/principles/

Decision-making frameworks that guide how bots approach trade-offs. Unlike guardrails (hard rules), principles are heuristics — they shape judgment calls rather than imposing binary constraints.

## What belongs here

One `.md` file per principle. Use YAML frontmatter with `title:` and `description:`, followed by an H1 heading and an explanation with concrete examples. Each principle should be self-contained.

## Composition

Listed in fleet.yaml under `principles:` at the defaults or bot level:

```yaml
defaults:
  principles: [consolidate-dont-fork, visibility-and-speed]
```

## Naming

Lowercase, hyphenated, opinionated: `consolidate-dont-fork.md`, `no-backwards-compat-shims.md`.
