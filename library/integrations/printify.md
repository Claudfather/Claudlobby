---
title: Printify
type: mcp
env_contract:
  PRINTIFY_API_KEY:
    description: Printify API key
    tier: fleet
  PRINTIFY_SHOP_ID:
    description: Printify shop ID
    tier: fleet
---

# Printify

### Printify MCP

Wire config: fleet-local (no shared `library/mcp/printify.json` — configure via local overlay or direct `.mcp.json`).

#### Forked MCP with Order Tools

The published `printify-mcp` package only exposes product/shop/blueprint tools. To get order-management tools (`list_orders`, `submit_order`, `get_order`, `send_order_to_production`, `calculate_shipping`, `cancel_order`), fork upstream `TSavo/printify-mcp` and add them yourself.

The `.mcp.json` points to the local build at `~/your-fork/printify-mcp/dist/index.js`. If making changes, edit `src/` files, run `npm run build`, then restart the bot.

**Periodically check:** whether upstream merged the order tools PR. If merged, switch back to the published package (`npx -y printify-mcp`) and drop the local fork.

#### Gotchas

- Printify fulfillment status doesn't always sync back to Shopify — monitor for orphaned unfulfilled orders in Shopify
- Order creation requires a `send_order_to_production` call after `submit_order` — two-step process
- Blueprint browsing returns large payloads — use specific blueprint IDs when possible rather than listing all

### API / developer reference

Full REST reference: **https://developers.printify.com/**. When the MCP tools are lossy (the fork's `list_products` can drop the product array from its formatted output), hit the API directly — auth is `Authorization: Bearer $PRINTIFY_API_KEY`, shop from `$PRINTIFY_SHOP_ID` (both fleet-tier `.env`).

Endpoints worth knowing:

- `GET /v1/shops/{shop_id}/products.json?limit=N&page=P` — list products (paginated; response carries `total` + `last_page`). Printify documents `limit` as **default 10, maximum 50**. **Payload gotcha:** each result is a full product object, so the response gets heavy well below that ceiling — `limit=100` is both over the documented max *and* slow enough to time out or truncate before the client parses it. Use `limit=20–30` with a longer timeout and paginate.
- `GET /v1/shops/{shop_id}/products/{product_id}.json` — one product with `variants[]`; each variant has `price` (retail, cents) and `cost` (production, cents), so **unit margin = `price − cost`**.
- `PUT /v1/shops/{shop_id}/products/{product_id}.json` — update; pass the `variants` array with a new `price` (cents) to re-price, then `POST …/products/{id}/publish.json` to push the change to Shopify.
- **Shopify linkage:** a synced Printify product carries the connected store's product id, which is what reconciles the two catalogs — but **confirm the field against a live response before coding to it.** Printify's public spec has no `external` field on the product schema at all; the likely home is `sales_channel_properties`, which the spec leaves untyped (array of bare `object`, example `[]`). The one `external.id` the spec does define belongs to the *outbound* publish-confirmation payload and is typed **string**, not numeric. A product can be **orphaned**: deleted in Printify but still live in Shopify at its last-synced price. It won't appear in the Printify products list, and Shopify becomes its sole source of truth (re-price it directly in Shopify — nothing will sync over the top).
