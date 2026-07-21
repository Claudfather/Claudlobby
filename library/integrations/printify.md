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
tool_grants:
  - "mcp__printify__get_blueprint"
  - "mcp__printify__get_blueprints"
  - "mcp__printify__get_defaults"
  - "mcp__printify__get_print_providers"
  - "mcp__printify__get_printify_status"
  - "mcp__printify__get_product"
  - "mcp__printify__get_variants"
  - "mcp__printify__how_to_use"
  - "mcp__printify__list_products"
  - "mcp__printify__list_shops"
---

# Printify

### Printify MCP

Wire config: shipped as the shared fragment `library/mcp/printify.json` — add `printify` to a bot's `mcp:` list and the compositor merges it into `.mcp.json`. It runs the published package (`npx -y printify-mcp`) and reads `PRINTIFY_API_KEY` + `PRINTIFY_SHOP_ID` (both fleet-tier `.env`). To get order-management tools, override it locally with a fork (below).

#### Forked MCP with Order Tools

The published `printify-mcp` package only exposes product/shop/blueprint tools. To get order-management tools (`list_orders`, `submit_order`, `get_order`, `send_order_to_production`, `calculate_shipping`, `cancel_order`), fork upstream `TSavo/printify-mcp` and add them yourself.

The `.mcp.json` points to the local build at `~/your-fork/printify-mcp/dist/index.js`. If making changes, edit `src/` files, run `npm run build`, then restart the bot.

**Periodically check:** whether upstream merged the order tools PR. If merged, switch back to the published package (`npx -y printify-mcp`) and drop the local fork.

#### Permissions

Read-only tools (`get_*`, `list_*`, `how_to_use`) are auto-allowed for every bot that attaches this MCP, so headless bots never stall on a read prompt. Mutations (`create_product`, `update_product`, `delete_product`, `publish_product`, `upload_image`, `generate_*`, `set_default`, `switch_shop`) always prompt; a bot that genuinely needs an unattended write gets it via fleet.yaml `tools.allow`. Split declared in `library/mcp/printify.json` `_permissions_contract` (see `library/mcp/README.md`). A fork override adding order tools should ship a local overlay fragment extending `tools`/`read_only_tools` (and mirror `tool_grants`) for its read-tier additions (`list_orders`, `get_order`, `calculate_shipping`).

#### Gotchas

- Printify fulfillment status doesn't always sync back to Shopify — monitor for orphaned unfulfilled orders in Shopify
- Order creation requires a `send_order_to_production` call after `submit_order` — two-step process
- Blueprint browsing returns large payloads — use specific blueprint IDs when possible rather than listing all
