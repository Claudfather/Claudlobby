---
name: printify
description: "Direct Printify v1 REST actuator — the reads the MCP is weak at (real shop/status, lossless descriptions, orders) AND the draft-first writes the MCP is buggy/limited at (create a product from a PNG, migrate a discontinued product onto a new provider, publish). Shares the PRINTIFY_API_KEY + PRINTIFY_SHOP_ID contract with the printify MCP. Writes leave a DRAFT and never auto-publish."
argument-hint: "[status|products [limit]|product <id>|orders [limit]|order <id>|raw <path>|create …|migrate …|publish …]"
---

# Printify

Direct reads **and draft-first writes** against the Printify v1 REST API — the sanctioned actuator that complements the `printify` MCP for everything the MCP does poorly. See [mcp-vs-api.md](./mcp-vs-api.md) for the full "which tool when" guide.

The **read** doors are unchanged and always safe. The **write** doors (`create` / `migrate` / `publish`) are direct-REST because the MCP is weak exactly on the write path (Markdown-only returns, hardcoded image geometry, no tags, and a mock-shop fabrication bug in some builds). Direct REST returns clean JSON, controls the full product model, and inherits this script's honesty guarantee (it surfaces the real HTTP error, never fabricates). **Every write leaves the product as a DRAFT — nothing auto-publishes.**

**One env contract, two tools.** This skill and `library/mcp/printify.json` both read the same fleet-tier vars, so a bot can declare either or both:

```yaml
# in fleet.yaml, per bot:
skills: [printify]     # this REST actuator (reads + draft-first writes)
mcp:    [printify]     # the MCP (optional — only if you also want the MCP tools, e.g. Replicate image-gen)
```

## Environment

Uses the fleet Printify contract (already exported from `local/<fleet>/.env`, no setup needed):

- `PRINTIFY_API_KEY` — Printify Personal Access Token / API key (JWT). Also honors `PRINTIFY_API_TOKEN`.
- `PRINTIFY_SHOP_ID` — Printify shop id (e.g. `1234567`).

`create` and `migrate` require `jq` (JSON build/parse). `publish` does not (its body is a literal), and the read doors keep their no-jq fallback.

## The tool

Run the co-located helper (resolves via the skill symlink, so the path is the same whether this skill is a fleet overlay or promoted to the shared library):

```bash
"$BOT_DIR/.claude/skills/printify/printify_api.sh" <command>
```

### Read doors (GET — always safe)

| Command | Returns | Why (vs MCP) |
|---------|---------|--------------|
| `status` | real shop(s) + which is current | The MCP faked this with "Mock Shop 1" on a transient failure. This is the **real** shop, never a mock. |
| `products [limit]` | product list: id, title, **description length**, tags, visible | The MCP list drops the description entirely. |
| `product <id>` | ONE product with its **full description** (lossless), tags, variant count | The MCP `get_product` truncates/drops the description — this is the headline gap. |
| `orders [limit]` | recent orders: id, `#label`, status, created_at, line-item count | The MCP's order coverage is thin. |
| `order <id>` | one full order (raw JSON) | — |
| `raw <api/path.json>` | raw authenticated GET | escape hatch for any v1 endpoint |
| `help` | usage | — |

### Write doors (direct REST — DRAFT-first, never auto-publish)

| Command | Does | Required args | Notes |
|---------|------|---------------|-------|
| `create` | PNG → a new **DRAFT** product | `--png <path\|url> --title T --blueprint <id> --provider <id> --price <cents>` | opt: `--desc`, `--tags a,b`, `--position front`, `--enable-variants all\|id,id`, `--dry-run`. Uploads the image, builds the variant/print-area model, leaves the product **DRAFT** and prints its edit URL + real mockup URLs. |
| `migrate` | a product → recreate on a **new provider** as a DRAFT | `--product <id> --to-provider <id>` | opt: `--to-blueprint <id>`, `--price-map id:cents,…`, `--dry-run`. Maps source→target variants by shared id, prints a **coverage report** (retained vs dropped), recreates as **DRAFT** reusing the source image ids. **Never** touches/retires the source. |
| `publish` | push a DRAFT to Shopify | `--product <id> --yes` | **EXPLICIT + human-gated**: refuses without `--yes`. `create`/`migrate` never call it. opt: `--dry-run`. |

Always run `--dry-run` first: `create`/`migrate` print the exact request body (and, for migrate, the coverage delta) **without sending**. The script surfaces the **real HTTP error** on failure (401/404/422/…) and never fabricates data.

## Draft-first & coverage — mandatory

- **Nothing auto-publishes.** `create` and `migrate` always leave the product a DRAFT. Publishing to Shopify is a separate, explicit, human-approved step — the `publish` door, which itself refuses without `--yes`. A new storefront product is a human-gated decision, so the tool cannot make it.
- **`migrate` reports, it does not decide.** When a target provider offers fewer variants than the source (e.g. a discontinued sticker provider → Printify Choice drops the whole Transparent surface), `migrate` prints the enumerated coverage loss and stops at a DRAFT. Whether to accept the drop is a **merchandising decision** for the product owner, not the tool's.
- **`migrate` never retires the source.** Deleting or unpublishing the old product is a separate, human-gated action.

## PII — mandatory

Order objects contain real customer PII (`address_to`: name, email, phone, address). The `orders` command is **PII-free by design** (it returns only id/label/status/created_at/line-item count). `order <id>` and `raw` return the **full** object including PII — never paste customer PII into chat, logs, or committed files; extract only what's needed and redact (`user_123`, `example@example.com`). See the PII/credential guardrail.

## Examples

```bash
S="$BOT_DIR/.claude/skills/printify/printify_api.sh"

# reads
"$S" status                         # is Printify really connected, and to which shop?
"$S" products 20                    # catalog overview with description lengths
"$S" product aaaaaaaaaaaaaaaaaaaaaaaa | jq -r .description   # the full PDP copy
"$S" orders 10                      # recent order pipeline (no PII)

# writes — always dry-run first (prints the exact request, sends nothing)
"$S" create --png ./art.png --title "New Sticker" --blueprint 400 --provider 99 --price 500 --dry-run
"$S" migrate --product <spoke_sticker_id> --to-provider 99 --dry-run   # coverage report: White retained, Transparent dropped

# live write leaves a DRAFT + prints the Printify edit URL and mockup URLs
"$S" create --png ./art.png --title "New Sticker" --blueprint 400 --provider 99 --price 500

# publish is explicit + human-gated (refuses without --yes)
"$S" publish --product <draft_id> --yes
```

## Instructions

1. **Prefer this over the MCP** for real shop/status verification, lossless descriptions, order reads, and **all product writes** (create/migrate/publish) — direct REST returns clean JSON and controls the full model. The MCP's one non-redundant capability for this fleet is Replicate image generation (a separate design-gen flow that hands a PNG to `create`).
2. **Never fabricate** — if the script errors (401/404/422/network), report the real error; do not invent shop/product/order/ids.
3. **Redact PII** from any order output before sharing.
4. **Order-list caps at 10** per page (a Printify constraint); paginate with `raw "shops/$PRINTIFY_SHOP_ID/orders.json?limit=10&page=N"` for more.
5. **Writes are draft-first.** `create`/`migrate` leave a DRAFT and print the edit URL — report that URL + the mockup image(s) and stop. **Publishing is a distinct, human-approved step** (`publish --product <id> --yes`); never publish without an explicit human go. Always `--dry-run` before a live write.
6. **`migrate` is a reporter at a coverage cliff.** Surface the retained/dropped delta and let a human make the merchandising call; never auto-decide a coverage drop and never retire the source product.

$ARGUMENTS
