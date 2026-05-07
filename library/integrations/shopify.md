---
title: Shopify
env_contract:
  SHOPIFY_ACCESS_TOKEN:
    description: Shopify Admin API access token
    tier: fleet
  SHOPIFY_STORE_DOMAIN:
    description: Shopify store domain (e.g. mystore.myshopify.com)
    tier: fleet
---

### Shopify MCP

Wire config: `library/mcp/shopify.json` (uses `${SHOPIFY_ACCESS_TOKEN}`, `${SHOPIFY_STORE_DOMAIN}`).

#### Common Ops

- **Orders:** `mcp__shopify__*` — list, search, fulfill, refund
- **Products:** create, update, manage variants, set pricing
- **Customers:** lookup by email/name, order history
- **Collections:** smart (rule-based) and manual collections

#### Hidden / Unlisted Product Pattern

To make a product unlisted (buyable via direct link but not browsable in collections):

1. Set product type to `"Hidden"`
2. Add the tag `"hidden"`

This drops the product from smart collections that match on product type. Add an explicit rule to key collections: `TAG NOT_EQUALS hidden`.

The product remains accessible at `store.com/products/[handle]` and stays in any manual collections. Used for exclusive drops shared via direct link only.

#### Sales Channel Routing

When running a headless storefront alongside Shopify's native storefront, products must be published to the correct sales channel to appear. A custom sales channel (e.g., "API Store Manager") controls what the headless site shows independently of the Shopify Online Store channel.

#### Gotchas

- Shopify webhooks can't point to the store's own domain — use the deployment URL (e.g., `project.vercel.app/api/revalidate`) for revalidation hooks
- Printify fulfillment status doesn't always sync back to Shopify — monitor for orphaned unfulfilled orders
- Discount codes and inventory adjustments have audit-trail implications — flag the human before creating or modifying
