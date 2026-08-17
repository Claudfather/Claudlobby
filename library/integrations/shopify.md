---
title: Shopify
env_contract:
  SHOPIFY_ACCESS_TOKEN:
    description: Shopify Admin API access token
    tier: fleet
    secret: true
  SHOPIFY_STORE_DOMAIN:
    description: Shopify store domain (e.g. mystore.myshopify.com)
    tier: fleet
    secret: false
tool_grants:
  - "mcp__shopify__getAbandonmentReport"
  - "mcp__shopify__getConversionReport"
  - "mcp__shopify__getCustomReport"
  - "mcp__shopify__getCustomer"
  - "mcp__shopify__getCustomerAnalytics"
  - "mcp__shopify__getFinancialSummary"
  - "mcp__shopify__getInventoryLevels"
  - "mcp__shopify__getInventoryReport"
  - "mcp__shopify__getMarketingReport"
  - "mcp__shopify__getOrder"
  - "mcp__shopify__getProduct"
  - "mcp__shopify__getProductAnalytics"
  - "mcp__shopify__getSalesReport"
  - "mcp__shopify__getShippingZones"
  - "mcp__shopify__getShopInfo"
  - "mcp__shopify__getTrafficReport"
  - "mcp__shopify__listAbandonedCheckouts"
  - "mcp__shopify__listBlogs"
  - "mcp__shopify__listCollections"
  - "mcp__shopify__listCompanies"
  - "mcp__shopify__listCustomers"
  - "mcp__shopify__listDiscounts"
  - "mcp__shopify__listDraftOrders"
  - "mcp__shopify__listFulfillmentOrders"
  - "mcp__shopify__listGiftCards"
  - "mcp__shopify__listLocations"
  - "mcp__shopify__listMarkets"
  - "mcp__shopify__listMetaobjects"
  - "mcp__shopify__listOrders"
  - "mcp__shopify__listPages"
  - "mcp__shopify__listPriceRules"
  - "mcp__shopify__listProducts"
  - "mcp__shopify__listThemes"
  - "mcp__shopify__listTransactions"
  - "mcp__shopify__listWebhooks"
---

# Shopify

### Shopify MCP

Wire config: `library/mcp/shopify.json` (uses `${SHOPIFY_ACCESS_TOKEN}`, `${SHOPIFY_STORE_DOMAIN}`).

#### Common Ops

- **Orders:** list, search (auto-allowed reads); fulfill, refund (prompted writes)
- **Products:** create, update, manage variants, set pricing
- **Customers:** lookup by email/name, order history
- **Collections:** smart (rule-based) and manual collections

#### Permissions

Read-only tools (`get*`, `list*`) are auto-allowed for every bot that attaches this MCP, so headless bots never stall on a read prompt. Mutations (`createProduct`, `updateProduct`, `createRefund`, `adjustInventory`, etc.) always prompt; a bot that genuinely needs an unattended write gets it via fleet.yaml `tools.allow`. Split declared in `library/mcp/shopify.json` `_permissions_contract` (see `library/mcp/README.md`).

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
