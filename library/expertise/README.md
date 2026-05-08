# library/expertise/

Role scaffolding files that define what a bot **does** — its responsibilities, workflows, and domain knowledge. Each file describes a single area of expertise (e.g. `software-engineering`, `orchestration`, `code-review`).

## What belongs here

One `.md` file per expertise area. Each should cover:

- **Role summary** — what this expertise entails
- **Key responsibilities** — what the bot does day-to-day
- **Workflows** — standard operating procedures for common tasks
- **Boundaries** — what the bot should escalate vs handle alone

## Composition

Bots list one or more expertise areas in fleet.yaml: `expertise: [software-engineering, code-review]`. The compositor concatenates each file into the bot's CLAUDE.md. Voice/personality is separate — see `voices/`.

## Naming

Lowercase, hyphenated: `software-engineering.md`, `data-engineering.md`, `code-review.md`.
