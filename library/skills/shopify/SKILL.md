---
name: shopify
description: "Shopify Admin API actuator built around the fields that lie. Answers the questions people actually ask — is anything unfulfillable, how much discounting is live, what is really in this collection, is the catalogue healthy — using the field that is SOUND rather than the obvious field that silently returns a wrong answer. Read-only by construction. Every trap it encodes was measured against a live store, not recalled."
argument-hint: "[health-check|catalog [--unfulfillable --fulfiller-ids <file>]|discounts|collections [handle]|orders [limit]|raw <path>]"
---

# Shopify

Direct reads against the Shopify Admin API. The calls are trivial — anyone can curl
Shopify. **The value is [traps.md](./traps.md)**: eight fields and endpoints that return a
confident, plausible, wrong answer, each one re-measured against a live store before
being written down.

If you read one thing here, read that. This file is the router.

## Why not the MCP

The Shopify MCP's `getProduct` is **broken** against the current Admin API — it requests
`ProductVariant.weight`, which no longer exists, so the call fails for every product:

```
Error fetching product: Field 'weight' doesn't exist on type 'ProductVariant'
```

Verified live. That is not a preference between tools; that door does not function. Its
read-only report/list tools are fine — use those freely. For product reads, come here.

## Environment

Nothing store-specific is baked in. Same contract style as the `printify` skill:

- `SHOPIFY_STORE_DOMAIN` — `myshop.myshopify.com` (also honors `SHOPIFY_SHOP_DOMAIN`)
- `SHOPIFY_ACCESS_TOKEN` — Admin API access token (also honors `SHOPIFY_ADMIN_ACCESS_TOKEN`)
- `SHOPIFY_API_VERSION` — optional, defaults to a recent version

```yaml
# in fleet.yaml, per bot:
skills: [shopify]
```

## The tool

```bash
"$BOT_DIR/.claude/skills/shopify/shopify_api.sh" <command>
```

### Doors

| Command | Answers | The trap it routes around |
|---------|---------|---------------------------|
| `health-check` | Is the catalogue sound? Emits a `warnings` array, not a dashboard | all of them, in one sweep |
| `catalog` | What is the inventory picture really? | reports `inventory_management`, never the `inventory_quantity` placeholder — [2](./traps.md) |
| `catalog --unfulfillable --fulfiller-ids <file>` | What can we not actually ship? | set difference against the fulfiller, because `fulfillment_service` is `manual` on everything — [1](./traps.md), [3](./traps.md) |
| `discounts` | How much discounting is live? | counts **codes**, not nodes — one node can hold thousands — [5](./traps.md) |
| `collections [handle]` | What is actually in this collection? | full cursor pagination; a bare `first:` truncates in silence — [4](./traps.md) |
| `webhooks` | Which critical topics are **not** registered? | absence is silent and total — a missing orders webhook is indistinguishable from no orders — [9](./traps.md) |
| `copy` | Which descriptions are defective? | matches the `.:` marker, which is invisible in API output and renders literally — [10](./traps.md) |
| `redirects` | Which 301s are broken, in **either** direction? | dead destinations *and* revived sources; the second is rarer and costs more — [11](./traps.md), [7](./traps.md) |
| `orphans` | What does nothing link to? | returns candidates with the question attached — "orphaned" has several correct answers, and deletion is usually the wrong one — [12](./traps.md) |
| `consent` | What is the marketing-consent split? | counts every page via the `Link` header; `customersCount` saturates at 10,000 — [13](./traps.md) |
| `orders [limit]` | Recent order pipeline | PII-light by default; full order objects carry customer data |
| `raw <path>` | Authenticated GET passthrough | escape hatch |

Each door encodes what the person who hit the underlying problem learned, and is reviewed
by them: `webhooks` — kenny · `copy` — todd · `redirects` — greg · `orphans` — saul ·
`consent` — kev. The query in each is trivial; **the trap is the product.** Read
[traps.md](./traps.md) before extending any of them.

### Read-only by construction

There is **no write door**, deliberately. The obvious one to add is a status flip, and
that is exactly the operation that silently 404s every redirect pointing at the product
and unpublishes it from all sales channels irreversibly ([traps.md 7](./traps.md)). That
belongs behind a human, not behind a convenience wrapper. `test.sh` asserts no
`PUT`/`DELETE`/`PATCH` verb exists in the script, so this stays true.

The single `POST` reaches the GraphQL endpoint, which is a read.

## The four answers worth memorising

1. **`fulfillment_service` cannot tell you the supplier.** It is `"manual"` on every
   variant — measured 4,804 of 4,804. Ask the fulfiller, not Shopify.
2. **`inventory_quantity` is not stock.** When `inventory_management` is `null` the
   number is meaningless and frequently non-zero anyway. Purchasability is
   `availableForSale`. The popular "everything is 9999" version of this trap is *false* —
   9999 was 7% of variants.
3. **A discount "count" is a node count.** 250 nodes carried 24,115 codes; one node held
   10,928. Sum `codesCount`.
4. **Unlisted is two fields, both load-bearing.** `product_type = "Hidden"` *and* the
   `hidden` tag. Half the convention means hidden on some surfaces and publicly
   browsable on others.

## Honesty guarantee

Surfaces the **real** HTTP error (401/403/404/429/5xx) and exits non-zero. GraphQL errors
arrive inside a `200`, so they are checked separately — treating any 200 as success is
how a failed query becomes "zero results", which is the failure mode this whole skill
exists to prevent.

Never fabricates. If it cannot see the store, it says so.

## PII

`orders` is deliberately PII-light (no customer name, email, phone or address). `raw`
returns whatever the endpoint returns, including PII — redact before sharing, and never
paste a full order object into chat, a log, or a committed file.

## Testing

```bash
bash test.sh          # hermetic: contract + trap logic from fixtures/, no creds needed
SHOPIFY_STORE_DOMAIN=… SHOPIFY_ACCESS_TOKEN=… bash test.sh   # adds a live smoke
```

The fixture tests pin the *behaviour* traps.md describes — e.g. that the naive 9999 rule
and the sound `inventory_management` rule return **different** answers on the same data.
An edit that quietly reverts a door to the obvious field turns them red.
