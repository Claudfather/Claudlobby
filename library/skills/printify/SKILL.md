---
name: printify
description: "Use when a Printify read or write is needed: the real shop and its status, products with their full descriptions, orders, or a product created from a PNG, migrated onto a new print provider, or published. Calls the Printify v1 REST API directly through printify_api.sh, the tool of the printify integration. Reads run without a prompt; writes ask, create and migrate leave a DRAFT, and nothing publishes without an explicit publish --yes."
argument-hint: "[status|products [limit]|product <id>|orders [limit]|order <id>|raw <path>|create …|migrate …|publish …]"
tool_grants:
  - "Bash(.claude/skills/printify/printify_api.sh status *)"
  - "Bash(.claude/skills/printify/printify_api.sh products *)"
  - "Bash(.claude/skills/printify/printify_api.sh product *)"
  - "Bash(.claude/skills/printify/printify_api.sh orders *)"
  - "Bash(.claude/skills/printify/printify_api.sh order *)"
  - "Bash(.claude/skills/printify/printify_api.sh raw *)"
  - "Bash(.claude/skills/printify/printify_api.sh help *)"
---

# Printify

Direct reads **and draft-first writes** against the Printify v1 REST API. This skill is the tool of the `printify` integration (`library/integrations/printify.md`), which declares the env contract and carries the API reference. The Printify MCP server is deprecated; [mcp-vs-api.md](./mcp-vs-api.md) says what that means for a bot that still lists it.

The **read** doors are always safe and run without a prompt. The **write** doors (`create` / `migrate` / `publish`) return clean JSON, control the full product model, and surface the real HTTP error on failure. **Every write leaves the product as a DRAFT — nothing auto-publishes** — and each one asks before it runs.

```yaml
# in fleet.yaml, per bot:
skills: [printify]          # this tool: reads + draft-first writes
integrations: [printify]    # the guide, and the PRINTIFY_* env contract that doctor checks
```

## Environment

Uses the Printify env contract that `integrations: [printify]` declares, exported from the fleet-tier `.env`:

- `PRINTIFY_API_KEY` — Printify Personal Access Token / API key (JWT). Also honors `PRINTIFY_API_TOKEN`.
- `PRINTIFY_SHOP_ID` — Printify shop id (e.g. `1234567`).

`create` and `migrate` require `jq` (JSON build/parse). `publish` does not (its body is a literal), and the read doors keep their no-jq fallback.

## The tool

Run the co-located helper from your bot directory, where every session starts. `.claude/skills/printify/` is this skill's copy inside the bot, the same path whether the skill comes from a fleet overlay or the shared library:

```bash
.claude/skills/printify/printify_api.sh <command>
```

### Read doors (GET — always safe)

| Command | Returns | Notes |
|---------|---------|-------|
| `status` | real shop(s) + which is current | The real shop list: a failed call prints the real HTTP error. |
| `products [limit]` | product list: id, title, **description length**, tags, visible | — |
| `product <id>` | ONE product with its **full description** (lossless), tags, variant count | — |
| `orders [limit]` | recent orders: id, `#label`, status, created_at, line-item count | No customer details. |
| `order <id>` | one full order (raw JSON) | Includes the customer's details. |
| `raw <api/path.json>` | raw authenticated GET | escape hatch for any v1 endpoint |
| `help` | usage | — |

### Write doors (direct REST — DRAFT-first, never auto-publish)

| Command | Does | Required args | Notes |
|---------|------|---------------|-------|
| `create` | PNG → a new **DRAFT** product | `--png <path\|url> --title T --blueprint <id> --provider <id> --price <cents>` | opt: `--desc`, `--tags a,b`, `--position front`, `--enable-variants all\|id,id`, `--dry-run`. Uploads the image, builds the variant/print-area model, leaves the product **DRAFT** and prints its edit URL + real mockup URLs. |
| `migrate` | a product → recreate on a **new provider** as a DRAFT | `--product <id> --to-provider <id>` | opt: `--to-blueprint <id>`, `--price-map id:cents,…`, `--dry-run`. Maps source→target variants by shared id, prints a **coverage report** (retained vs dropped), recreates as **DRAFT** reusing the source image ids. **Never** touches/retires the source. |
| `publish` | push a DRAFT to Shopify | `--product <id> --yes` | **EXPLICIT + human-gated**: refuses without `--yes`. `create`/`migrate` never call it. opt: `--dry-run`. |

Always run `--dry-run` first: `create`/`migrate` print the exact request body (and, for migrate, the coverage delta) **without sending**. The script surfaces the **real HTTP error** on failure (401/404/422/…) and never fabricates data.

## Permissions

This skill's `tool_grants` allow the read commands (`status`, `products`, `product`, `orders`, `order`, `raw`, `help`) in the form above, so they run without a prompt. The write commands are not granted, so `create`, `migrate` and `publish` each ask first; a bot that must write unattended grants the exact command in fleet.yaml `tool_permissions.allow`.

- **Run it from the bot directory.** The grants name a path relative to it. After a `cd` elsewhere, go back to the bot directory by its absolute path first.
- **Write values out.** `"$BOT_DIR/.claude/skills/printify/printify_api.sh" <command>` runs from anywhere, but every call asks: Claude Code allows a command that contains a `$VARIABLE` only through an exact-text rule. The same goes for a `$VARIABLE` in an argument, so write a shop or product id literally.

## Draft-first & coverage — mandatory

- **Nothing auto-publishes.** `create` and `migrate` always leave the product a DRAFT. Publishing to Shopify is a separate, explicit, human-approved step — the `publish` door, which itself refuses without `--yes`. A new storefront product is a human-gated decision, so the tool cannot make it.
- **`migrate` reports, it does not decide.** When a target provider offers fewer variants than the source (e.g. a discontinued sticker provider → Printify Choice drops the whole Transparent surface), `migrate` prints the enumerated coverage loss and stops at a DRAFT. Whether to accept the drop is a **merchandising decision** for the product owner, not the tool's.
- **`migrate` never retires the source.** Deleting or unpublishing the old product is a separate, human-gated action.

## PII — mandatory

Order objects contain real customer PII (`address_to`: name, email, phone, address). The `orders` command is **PII-free by design** (it returns only id/label/status/created_at/line-item count). `order <id>` and `raw` return the **full** object including PII — never paste customer PII into chat, logs, or committed files; extract only what's needed and redact (`user_123`, `example@example.com`). See the PII/credential guardrail.

## Examples

```bash
# reads: run without a prompt
.claude/skills/printify/printify_api.sh status        # is Printify really connected, and to which shop?
.claude/skills/printify/printify_api.sh products 20   # catalog overview with description lengths
.claude/skills/printify/printify_api.sh product aaaaaaaaaaaaaaaaaaaaaaaa   # the full PDP copy
.claude/skills/printify/printify_api.sh orders 10     # recent order pipeline (no customer details)

# writes: each asks; always dry-run first (prints the exact request, sends nothing)
.claude/skills/printify/printify_api.sh create --png ./art.png --title "New Sticker" --blueprint 400 --provider 99 --price 500 --dry-run
.claude/skills/printify/printify_api.sh migrate --product <legacy_sticker_id> --to-provider 99 --dry-run   # coverage report: White retained, Transparent dropped

# live write leaves a DRAFT + prints the Printify edit URL and mockup URLs
.claude/skills/printify/printify_api.sh create --png ./art.png --title "New Sticker" --blueprint 400 --provider 99 --price 500

# publish is explicit + human-gated (refuses without --yes)
.claude/skills/printify/printify_api.sh publish --product <draft_id> --yes
```

## Instructions

1. **Use this skill for every Printify read and write.** Direct REST returns clean JSON and controls the full product model. The skill does not generate images: make the PNG elsewhere and hand it to `create --png`.
2. **Never fabricate** — if the script errors (401/404/422/network), report the real error; do not invent shop/product/order/ids.
3. **Redact PII** from any order output before sharing.
4. **Order-list caps at 10** per page (a Printify constraint); paginate with `raw "shops/<shop_id>/orders.json?limit=10&page=N"`, the shop id written out (from `status`).
5. **Writes are draft-first.** `create`/`migrate` leave a DRAFT and print the edit URL — report that URL + the mockup image(s) and stop. **Publishing is a distinct, human-approved step** (`publish --product <id> --yes`); never publish without an explicit human go. Always `--dry-run` before a live write.
6. **`migrate` is a reporter at a coverage cliff.** Surface the retained/dropped delta and let a human make the merchandising call; never auto-decide a coverage drop and never retire the source product.

$ARGUMENTS
