# library/resources/

Shared **environment facts** that bots need to know about — non-secret identifiers, paths, and configuration that's specific to a fleet's environment but referenced by multiple bots.

## What belongs here

- **Database / warehouse identifiers** — Notion DB IDs (database + data-source), Snowflake account/warehouses/role, Neon connection strings (non-secret parts), Postgres schema names
- **Service / project IDs** — Railway project IDs, Vercel project slugs, Modal app names, Linear project keys
- **API endpoints / domains** — internal staging URLs, internal preview domains, MCP server URLs (non-secret)
- **Naming conventions / schema references** — "the metric naming pattern is `<domain>_<verb>_<unit>`", "data team Notion DB has these required fields"

## What does NOT belong here

- **Secrets** — tokens, passwords, private keys → `.env` (gitignored)
- **Capability / how-to** — that's expertise or protocols
- **Rules** — that's guardrails (`no-push-main`, `pii-protection`)

## Frontmatter (optional)

Unlike most of the library, frontmatter isn't required here — the two real files in this category disagree with each other. `timezone.md` has none and opens directly with an H1 (`# Timezone`); `frontmatter-schema.md` has full `title:`/`description:` frontmatter with a matching H1. Both compose cleanly either way: if you skip frontmatter, the compositor derives a title from the filename (e.g. `team-task-tracker.md` → "Team task tracker") and strips a leading H1 that matches it, exactly as it would strip a matching `title:`. Pick either shape, but make sure your leading heading (if you have one) matches whichever title — explicit or filename-derived — the compositor will use.

## Composition

Each `<resource>.md` is appended under a `## Resources` section in the bot's CLAUDE.md, in the order listed in `fleet.yaml` `resources:` (and `defaults.resources:`). The compositor wraps each file's body in `### <title>`; it isn't pasted in as a verbatim top-level blob.

```yaml
defaults:
  resources: [fleet-github-org]    # every bot gets this
bots:
  eng-1:
    resources: [project-warehouse, team-task-tracker]
```

## Example

`library/resources/timezone.md` (real content — no frontmatter; the filename-derived title "Timezone" matches the H1, so it composes cleanly):

```markdown
# Timezone

The host system clock may run in UTC. Always check the human's timezone from the bot's `TZ` environment variable (set in fleet.yaml `env:`), and convert all times before presenting.

When displaying times: use the human's local format (e.g., "2:30 PM ET" not "18:30 UTC"). When computing "today", "tomorrow", or "this week", run `TZ='$TZ' date` to anchor to the human's timezone, not the system clock.
```

When a bot lists `timezone` in its `resources:`, this body lands under a `### Timezone` heading in the bot's CLAUDE.md.

**Note:** `library/resources/frontmatter-schema.md` is an outlier in this category. Despite living here, it documents the frontmatter schema for the fleet's *shared documentation* corpus (`shared/knowledge/`, `shared/planning/`) — `protocols/shared-documentation.md` calls it "the Documentation Frontmatter Schema resource" — not an environment fact a bot needs. Use `timezone.md`, not `frontmatter-schema.md`, as the model for what belongs in `library/resources/`.

## Templating

Resource files may use `{{BOT_NAME}}`, `{{FLEET_NAME}}`, `{{CLAUDLOBBY_ROOT}}` placeholders.
