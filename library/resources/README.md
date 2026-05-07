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

## Composition

Each `<resource>.md` is appended verbatim under a `## Resources` section in the bot's CLAUDE.md, in the order listed in `fleet.yaml` `resources:` (and `defaults.resources:`).

```yaml
defaults:
  resources: [fleet-github-org]    # every bot gets this
bots:
  eng-1:
    resources: [project-warehouse, team-task-tracker]
```

## Example

`library/resources/team-task-tracker.md`:

```markdown
## Notion: Team Task Tracker

- **Database ID:** `<your-database-uuid>`
- **Data-source ID:** `<your-data-source-uuid>`
- **Required fields:** Title (text), Status (select), Owner (person), Sprint (select)

The Notion API splits *databases* from *queryable data sources* — both IDs are required: the database ID for `pages.create`, the data-source ID for `dataSources.query`. Pass the data-source ID, not the database ID, into `query_data_source`.
```

When a bot lists this in its `resources:`, the markdown above lands in the bot's CLAUDE.md verbatim.

## Templating

Resource files may use `{{BOT_NAME}}`, `{{FLEET_NAME}}`, `{{CLAUDLOBBY_ROOT}}` placeholders.
