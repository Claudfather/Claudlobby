# library/expertise/

Role scaffolding files that define what a bot **does** — its responsibilities, workflows, and domain knowledge. Each file describes a single area of expertise (e.g. `software-engineering`, `orchestration`, `code-review`).

## What belongs here

One `.md` file per expertise area. Each should cover:

- **Role summary** — what this expertise entails
- **Key responsibilities** — what the bot does day-to-day
- **Workflows** — standard operating procedures for common tasks
- **Boundaries** — what the bot should escalate vs handle alone
- **Quality gates** — hard rules that are never violated (newer files carry a dedicated section; older files predate the bullet — backfill opportunistically when touching them)

## The bright line: expertise vs voice

- **Expertise** = domain knowledge, responsibilities, workflows, boundaries. "What you do."
- **Voice** (in `voices/`) = personality, tone, humor, character. "Who you sound like."

These are independent axes. Any expertise can pair with any voice — an `seo` expertise works with any voice or none at all. If your expertise file includes personality traits, humor style, or communication mannerisms, it's leaking voice into expertise — fix it.

## Frontmatter: `permissions:`, not `title:`/`description:`

Expertise files don't use the library-wide `title:`/`description:` frontmatter. Instead, each carries a `permissions:` block that `composer.py::_resolve_expertise_permissions` reads to build Layer 2 of the bot's `.claude/settings.local.json` tool permissions:

```yaml
---
permissions:
  allow_all: true                                # broad tool access, or:
  allow: [Read, Grep, Glob, Bash, WebFetch, WebSearch]
  deny: [Write, Edit, NotebookEdit]               # deny wins over allow at this layer
  bash_allow: [git, gh, npm, npx, pip, python]    # becomes Bash(<cmd> *) allow patterns
---
```

All shipped expertise files use this schema (see `software-engineering.md`, `code-review.md`, etc.) — none use `title:`/`description:`.

## H1 convention: `# {{BOT_NAME}} — <Role>`

Each file opens with an H1 of the form `# {{BOT_NAME}} — <Role>` (e.g. `# {{BOT_NAME}} — Engineer`, `# {{BOT_NAME}} — Manager / Orchestrator`). `loader.py::parse_expertise_file` always strips this H1 out of the composed body, but when a bot's *first*-listed expertise file has one, the `<Role>` half becomes that bot's `title_label` — which the template uses to build the bot's own top-level heading in its composed CLAUDE.md (`# <bot-name> — <Role>`). Expertise areas listed after the first also get their H1 stripped, but their `title_label` is discarded; only the first one wins.

## Composition

Bots list one or more expertise areas in fleet.yaml: `expertise: [software-engineering, code-review]`. The compositor concatenates each file's body (H1 and `permissions:` frontmatter stripped) into the bot's CLAUDE.md, and merges every listed file's `permissions:` block into the bot's tool permissions. Voice/personality is separate — see `voices/`.

## Naming

Lowercase, hyphenated: `software-engineering.md`, `data-engineering.md`, `code-review.md`.
