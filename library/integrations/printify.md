---
title: Printify
type: cli
env_contract:
  PRINTIFY_API_KEY:
    description: Printify API key
    default_tier: fleet
    secret: true
  PRINTIFY_SHOP_ID:
    description: Printify shop ID
    default_tier: fleet
    secret: false
---

# Printify

### The Printify API

Printify is reached through its v1 REST API. The tool is the `printify` skill, `library/skills/printify/printify_api.sh`. Equip both:

```yaml
skills: [printify]          # the tool: reads, and draft-first writes
integrations: [printify]    # this guide, and the PRINTIFY_* env contract that doctor checks
```

Run the tool from the bot directory, where every session starts:

```bash
.claude/skills/printify/printify_api.sh <command>
```

- **Reads:** `status` (the real shop list, and which shop is current), `products [limit]`, `product <id>` (with its full description), `orders [limit]` (no customer data), `order <id>`, and `raw <api/path.json>` for any other v1 GET.
- **Writes, draft-first:** `create` (a PNG to a new draft product), `migrate` (a product recreated on a new print provider, as a draft) and `publish` (pushes a draft to the store, and refuses without `--yes`). `create` and `migrate` never publish. Run each with `--dry-run` first: it prints the request and sends nothing.

`library/skills/printify/SKILL.md` has the full usage. Auth is `Authorization: Bearer $PRINTIFY_API_KEY`; the shop is `$PRINTIFY_SHOP_ID`. Both live in the fleet-tier `.env`.

#### Permissions

The skill's `tool_grants` allow its reads in the form above, so they run without a prompt. Its writes are not granted and ask first; a bot that must write unattended grants the exact command in fleet.yaml `tool_permissions.allow`. The grants name a path relative to the bot directory, so run the tool from there. `"$BOT_DIR/.claude/skills/printify/printify_api.sh" <command>` runs it from anywhere, but every call asks: Claude Code allows a command that contains a `$VARIABLE` only through an exact-text rule.

#### Gotchas

- Printify fulfillment status doesn't always sync back to Shopify — monitor for orphaned unfulfilled orders in Shopify
- An order is created in two steps: submit it, then send it to production
- Blueprint browsing returns large payloads — use specific blueprint IDs when possible rather than listing all

### The MCP server (deprecated)

`library/mcp/printify.json`, the `@tsavo/printify-mcp` server, is deprecated. A bot that still lists `mcp: [printify]` composes as before: the server starts, its read tools are allowed by the fragment's own `_permissions_contract`, its writes ask, and `config validate` warns `mcp-deprecated`. This integration grants no `mcp__printify__` tool. To move a bot to the API, replace `mcp: [printify]` with the two lines above.

### API / developer reference

Full REST reference: **https://developers.printify.com/**. `raw` reaches the GETs below. The skill has no update door, so the `PUT` below is a direct API call with the same Bearer auth.

Endpoints worth knowing:

- `GET /v1/shops/{shop_id}/products.json?limit=N&page=P` — list products (paginated; response carries `total` + `last_page`). Printify documents `limit` as **default 10, maximum 50**. **Payload gotcha:** each result is a full product object, so the response gets heavy well below that ceiling — `limit=100` is both over the documented max *and* slow enough to time out or truncate before the client parses it. Use `limit=20–30` with a longer timeout and paginate.
- `GET /v1/shops/{shop_id}/products/{product_id}.json` — one product with `variants[]`; each variant has `price` (retail, cents) and `cost` (production, cents), so **unit margin = `price − cost`**.
- `PUT /v1/shops/{shop_id}/products/{product_id}.json` — update; pass the `variants` array with a new `price` (cents) to re-price, then `POST …/products/{id}/publish.json` to push the change to Shopify.
- **Shopify linkage:** a synced Printify product carries the connected store's product id, which is what reconciles the two catalogs — but **confirm the field against a live response before coding to it.** Printify's public spec has no `external` field on the product schema at all; the likely home is `sales_channel_properties`, which the spec leaves untyped (array of bare `object`, example `[]`). The one `external.id` the spec does define belongs to the *outbound* publish-confirmation payload and is typed **string**, not numeric. A product can be **orphaned**: deleted in Printify but still live in Shopify at its last-synced price. It won't appear in the Printify products list, and Shopify becomes its sole source of truth (re-price it directly in Shopify — nothing will sync over the top).
