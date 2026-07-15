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

Wire config: shipped as the shared fragment `library/mcp/printify.json` — add `printify` to a bot's `mcp:` list and the compositor merges it into `.mcp.json`. It runs the published package (`npx -y printify-mcp`) and reads `PRINTIFY_API_KEY` + `PRINTIFY_SHOP_ID` (both fleet-tier `.env`). To get order-management tools, override it locally with a fork (below).

#### Forked MCP with Order Tools

The published `printify-mcp` package only exposes product/shop/blueprint tools. To get order-management tools (`list_orders`, `submit_order`, `get_order`, `send_order_to_production`, `calculate_shipping`, `cancel_order`), fork upstream `TSavo/printify-mcp` and add them yourself.

The `.mcp.json` points to the local build at `~/your-fork/printify-mcp/dist/index.js`. If making changes, edit `src/` files, run `npm run build`, then restart the bot.

**Periodically check:** whether upstream merged the order tools PR. If merged, switch back to the published package (`npx -y printify-mcp`) and drop the local fork.

#### Gotchas

- Printify fulfillment status doesn't always sync back to Shopify — monitor for orphaned unfulfilled orders in Shopify
- Order creation requires a `send_order_to_production` call after `submit_order` — two-step process
- Blueprint browsing returns large payloads — use specific blueprint IDs when possible rather than listing all
