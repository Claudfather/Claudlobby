# Printify: use the API; the MCP is deprecated

Use the Printify v1 REST API through this skill, `printify_api.sh`. It is the tool of the `printify` integration, and it covers the reads and the writes:

```yaml
skills: [printify]          # this skill: reads + draft-first writes
integrations: [printify]    # the guide, and the PRINTIFY_* env contract that doctor checks
```

**The Printify MCP server is deprecated.** `library/mcp/printify.json` stays in the library so that a bot that still lists `mcp: [printify]` keeps composing and running. Its read tools are allowed by the fragment's own `_permissions_contract`, its writes ask, and `config validate` warns `mcp-deprecated`, naming the bots that list it. The `printify` integration grants no `mcp__printify__` tool. List the skill and the integration instead, and nothing new should list the MCP.

## What the skill covers

- **Reads:** `status` (the real shop list, and which shop is current), `products [limit]`, `product <id>` (the full description, lossless), `orders [limit]` (no customer details), `order <id>`, and `raw <api/path.json>` for any other v1 GET, the catalog included (for example `raw catalog/blueprints/<id>/print_providers/<provider_id>/variants.json`).
- **Writes, draft-first:** `create` (a PNG to a new DRAFT product, with its tags and print placement), `migrate` (a product recreated on a new print provider as a DRAFT, with a coverage report) and `publish` (refuses without `--yes`). Run each with `--dry-run` first.
- **Not covered:** image generation, and updating or deleting a product. Make the PNG elsewhere and hand it to `create --png`.

When a read must be complete or trustworthy, this skill is authoritative: it returns the API's own JSON and surfaces the real HTTP error.

## Env contract

`PRINTIFY_API_KEY` (secret) and `PRINTIFY_SHOP_ID`, declared by `library/integrations/printify.md` and read from the fleet-tier `.env`. The deprecated MCP fragment declares the same two vars, so a bot moving off it keeps its `.env` as it is.

## Related
- `library/skills/printify/SKILL.md` — this skill: usage, permissions, and the draft-first rules.
- `library/integrations/printify.md` — the integration: env contract, permissions and the API reference.
- `library/mcp/printify.json` — the deprecated MCP fragment.
