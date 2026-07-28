# Printify: MCP vs direct API — when to use which

Printify is exposed to the fleet as **two first-class tools that share one env contract** (`PRINTIFY_API_KEY` + `PRINTIFY_SHOP_ID`, fleet-tier in `local/<fleet>/.env`):

| Tool | What it is | Declare in fleet.yaml |
|------|-----------|-----------------------|
| **`printify` skill** | This skill → `printify_api.sh`, direct v1 REST **reads and draft-first writes** (create / migrate / publish). | `skills: [printify]` |
| **`printify` MCP** | `library/mcp/printify.json` → the `printify-mcp` server (Node). Exposes ~19 `mcp__printify__*` tools; its one non-redundant capability here is Replicate **image generation**. | `mcp: [printify]` |

A bot can declare **either or both**. Both read the same two env vars, so there is exactly one secret contract to manage.

## Decision guide

**Use the `printify` skill (direct REST) for:**
- **Real shop / connection status.** The MCP historically fabricated `Mock Shop 1` on a transient init failure and reported `Connected: Yes` (fixed in printify-mcp PR #1, but the API read is the ground truth). `printify_api.sh status` is the real shop, always.
- **Lossless product descriptions.** The MCP's `get_product` / `list_products` responses are **lossy** — they drop the product `description`. Copy/PDP work needs the full description → use `printify_api.sh product <id>`.
- **Orders** — quick, PII-controlled reads of the order pipeline.
- **Product writes — create, migrate, publish.** Direct REST returns clean JSON ids, sets `tags` and image geometry the MCP can't, and never touches the MCP's mock-shop bug. Writes are **draft-first**: `create`/`migrate` leave a DRAFT and print the edit URL; `publish` is explicit and `--yes`-gated. (The MCP `create_product` hardcodes image geometry, sets no tags, and returns Markdown you'd have to regex ids out of — all write-path blockers.)
- **Any endpoint the MCP doesn't expose** — `printify_api.sh raw <path>`.
- **Non-MCP bots.** Because `PRINTIFY_*` is fleet-tier, the REST skill works on a bot that has NOT declared the `printify` MCP. No MCP server process needed.

**Use the `printify` MCP for:**
- **Replicate image generation** (`generate_and_upload_image`) — the genuinely unique MCP capability. A decoupled design-gen flow calls it and hands the resulting PNG to `printify create --png`.
- **Rich tool ergonomics** inside an agent flow where structured MCP tool calls beat shelling out, and the mock-shop bug is not on the path.

## Rule of thumb

> **Reads that must be complete or trustworthy → REST skill. Product writes → REST skill (draft-first). Image generation → MCP.**

The REST skill is the actuator (trustworthy reads + honest, draft-first writes); the MCP is retained for image-gen and rich agent ergonomics. When they disagree on a read, the REST API is authoritative. When writing, prefer REST — it returns clean ids and controls the full product model.

## Env contract (shared)

Both resolve `${PRINTIFY_API_KEY}` / `${PRINTIFY_SHOP_ID}` at runtime from the process env, sourced from `local/<fleet>/.env` by `start-bot.sh`. Nothing about the contract is duplicated — declaring the MCP, the skill, or both draws on the same two vars.

## Related
- `library/skills/printify/SKILL.md` — this REST actuator (reads + draft-first writes).
- `library/mcp/printify.json` — the MCP fragment (fleet overlay points at the fixed local fork build; the shared default still points at the upstream `npx printify-mcp` and is being corrected upstream).
- printify-mcp PR #1 — removed the mock-shop fabrication + made `Connected` honest.
