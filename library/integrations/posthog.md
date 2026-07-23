---
title: PostHog
type: mcp
env_contract:
  POSTHOG_API_KEY:
    description: PostHog Personal API Key (MCP server preset, read-only scopes) — gitignored, format phx_XXXXXXXX
    tier: fleet
  POSTHOG_HOST:
    description: PostHog MCP endpoint host (mcp.posthog.com US / mcp-eu.posthog.com EU) — the MCP proxy host, not the app host
    tier: fleet
  POSTHOG_PROJECT_ID:
    description: Numeric PostHog project ID queries are pinned to (bare number, e.g. 12345)
    tier: fleet
tool_grants:
  - "mcp__posthog__organizations-get"
  - "mcp__posthog__organization-details-get"
  - "mcp__posthog__projects-get"
  - "mcp__posthog__insights-get-all"
  - "mcp__posthog__insight-get"
  - "mcp__posthog__insight-query"
  - "mcp__posthog__query-run"
  - "mcp__posthog__query-generate-hogql-from-question"
  - "mcp__posthog__dashboards-get-all"
  - "mcp__posthog__dashboard-get"
  - "mcp__posthog__event-definitions-list"
  - "mcp__posthog__property-definitions"
  - "mcp__posthog__properties-list"
---

# PostHog

Read-only product-analytics access via the **official PostHog MCP server**
(<https://github.com/PostHog/mcp>) — the hosted proxy at `mcp.posthog.com`, run
locally through `mcp-remote`. Bots pull funnels, trends, insights, event/property
definitions, and run ad-hoc **HogQL/SQL** against a single **pinned** PostHog
project using a read-scoped Personal API Key: no OAuth dance, no browser. This
unlocks the funnel data PostHog already captures so a fleet can query it directly
instead of one person reading a dashboard.

**Server choice.** PostHog's own MCP is the clear pick over community forks: it is
the source of truth, tracks the API, and — critically for a headless fleet — offers
first-class **read-only** and **project-pinning** controls (see below). The original
`PostHog/mcp` repo was archived in Jan 2026 and folded into the PostHog monorepo;
the hosted endpoint it published (`mcp.posthog.com`) remains the supported way in,
launched via the standard `mcp-remote` shim. We pin `mcp-remote@0.1.38` (the same
version the `granola` fragment already runs, so the global-binary cache is shared).
Trade-off: the *server* itself is PostHog-hosted and rolls forward — you pin the
transport (`mcp-remote`) and the tool set, not the server build.

## Read-only by construction (two layers)

This integration is read-only on **two independent layers**, so a bot cannot mutate
PostHog even if one layer is misconfigured:

1. **Server-side** — the endpoint is requested with `?mode=tools&readonly=true`.
   `readonly=true` makes the server register *only* read tools; every create/update/
   delete tool (`insight-create-from-query`, `dashboard-update`, `switch-project`, …)
   is excluded from the session entirely. Pinning `project_id` additionally drops the
   write-only `switch-project` / `switch-organization` tools — the project context is
   supplied by the URL instead.
2. **Compositor-side** — the fragment declares `read_only_tools`, so claudlobby emits
   one exact `mcp__posthog__<tool>` allow per read tool and **never** a
   `mcp__posthog__*` wildcard. Any tool not on the read list keeps prompting, so a
   headless bot can never silently invoke something outside the allow-set.

`mode=tools` registers each tool individually (the alternative, `mode=cli`, ships a
single opaque exec meta-tool — deliberately avoided here because it would collapse
layer 2 into one all-tunnelling grant).

## Auth model

- **Personal API Key, read scopes.** Create a key in PostHog → **Settings → Personal
  API keys** using the **"MCP server"** preset, then narrow its scopes to read-only
  (Insights: Read, Query: Read, Dashboards: Read, etc.). Scope it to the project(s)
  the fleet should see.
- **`POSTHOG_API_KEY`** is sent as `Authorization: Bearer <key>`. Keep it **outside
  git** (fleet `.env` only). **Never commit or print it.**
- **`POSTHOG_PROJECT_ID`** pins the project. **`POSTHOG_HOST`** selects the MCP
  endpoint host. All three are fleet-tier `.env` vars.
- **Rotate** by issuing a new key in PostHog, updating `POSTHOG_API_KEY` in the
  fleet `.env`, and restarting the bot; delete the old key in PostHog.

## Setup walkthrough (full, generalized)

1. **Pick the endpoint host.** `POSTHOG_HOST=mcp.posthog.com` for US Cloud (default),
   or `mcp-eu.posthog.com` for EU Cloud. The proxy also auto-routes to your account's
   region, so the US host usually works either way — set the EU host for data
   residency. (Self-hosted PostHog: the proxy can target a custom instance via the
   server's `POSTHOG_BASE_URL`; add it to the fragment `env` if you self-host.)
2. **Create a read-only Personal API Key** (PostHog → Settings → Personal API keys →
   "MCP server" preset). Restrict its scopes to read and to the intended project.
   Copy the `phx_…` value.
3. **Find the numeric project ID.** You don't have to hunt for it or ask anyone —
   the key can discover it. Once the MCP is wired, `projects-get` returns every
   project the key can see with its numeric ID; out-of-band, `GET /api/projects/`
   against the data host (see the host note in gotchas) does the same. In the UI it's
   under PostHog → Settings → Project (the URL and settings show a bare number, e.g.
   `12345`). This is **not** the `phc_…` client Web-analytics token, and **not** the
   `phx_…` key — it is the internal project ID.
4. **Set the three fleet `.env` vars** (gitignored — real values live only there):

   ```
   POSTHOG_API_KEY=phx_XXXXXXXXXXXXXXXXXXXX
   POSTHOG_HOST=mcp.posthog.com
   POSTHOG_PROJECT_ID=12345
   ```

5. **Wire the MCP** — add `posthog` to a bot's `mcp:` list, `claudlobby generate`
   (see *Equipping a bot*). First `npx` run downloads `mcp-remote` into the cache —
   `claudlobby warm-cache` warms it so the first bot use isn't slow.
6. **Verify it's connected *and* capturing.** These are two different questions —
   check both:
   - **Connected?** Run a trivial read (e.g. `projects-get`) to confirm auth + project
     pinning, and that the pinned tool names still match (the hosted server rolls
     forward — see gotchas).
   - **Actually capturing?** A connected key proves nothing about whether events are
     flowing. Pull the most recent events and look for fresh timestamps + the events
     you expect (`$pageview`, `$autocapture`, your custom events): via the MCP,
     `query-run` with HogQL `SELECT event, timestamp FROM events ORDER BY timestamp
     DESC LIMIT 5`; out-of-band, `GET /api/projects/<id>/events/?limit=5`. Recent rows
     = capturing. Empty or stale = not capturing yet (tag not firing, wrong
     project/key, or consent-gated with no consented visits yet).

## Common operations

The fragment grants **13 read-only tools**, grouped by job:

- **Workspace / context** — `organizations-get`, `organization-details-get`,
  `projects-get`: confirm which org/project the session is pinned to and what's
  available.
- **Insights** — `insights-get-all` (list saved insights), `insight-get` (fetch one),
  `insight-query` (run a saved insight and get its results). Saved **funnels** and
  **trends** are insights — read them here.
- **Ad-hoc queries** — `query-run` is the workhorse: run **HogQL/SQL** for custom
  funnels, trends, retention, and event rollups the saved insights don't cover.
  `query-generate-hogql-from-question` drafts HogQL from a natural-language question
  when you don't know the schema yet.
- **Dashboards** — `dashboards-get-all`, `dashboard-get`: read dashboard definitions
  and the insights on them.
- **Events / properties** — `event-definitions-list`, `property-definitions`,
  `properties-list`: discover the exact event and property names to reference in a
  HogQL query before you write it.

Typical flow: `event-definitions-list` / `property-definitions` to find field names →
`query-run` (HogQL) for the funnel/trend, or `insight-query` to read a saved one.

## Gotchas

- **Project ID ≠ Web-analytics token.** `POSTHOG_PROJECT_ID` is the bare numeric
  project ID (e.g. `12345`), **not** the `phc_…` client token wired into the site's
  snippet, and **not** the `phx_…` personal API key. Passing the wrong one fails or
  targets nothing.
- **Read-only mode is required, not optional, here.** The URL hardcodes
  `readonly=true`. If you ever remove it, writes would re-appear server-side — the
  compositor's exact-allow list still blocks them from auto-running, but don't rely on
  one layer. Keep `readonly=true` in the fragment.
- **Region — and two different hosts.** The **MCP proxy host** is `mcp.posthog.com`
  (US) / `mcp-eu.posthog.com` (EU); the proxy auto-routes by account, but set the
  matching one for EU data residency. Distinct from it is the **data-API host** you
  hit for an *out-of-band* REST check (e.g. the capture check in setup):
  `us.posthog.com` (US) / `eu.posthog.com` (EU). If you logged in at app.posthog.com
  you're on **US**, so try `us.posthog.com` first and fall back to `eu.posthog.com` —
  the wrong data host returns a 401 or a redirect, not your data.
- **Hosted server rolls forward → verify tool names on first connect.** The pinned
  tool list here matches the published schema, but because the server is
  PostHog-hosted it can add/rename tools. Tool names are documented as stable; still,
  confirm on first live use, and if PostHog renames one, update both
  `read_only_tools` (fragment) and `tool_grants` (this doc) together — they must
  mirror exactly or generation fails with a directional error.
- **`npx`/Node required.** `mcp-remote` is a Node shim; the host needs Node on PATH.
  A globally-installed `mcp-remote` is used automatically if present (`_global_binary`).

## Failure modes

- **`401 Unauthorized`** → `POSTHOG_API_KEY` is wrong, revoked, or lacks the scope for
  the tool called. Re-issue with the "MCP server" preset and read scopes.
- **Empty results / wrong project** → `POSTHOG_PROJECT_ID` points at a project the key
  can't see, or the wrong project. Confirm with `projects-get`.
- **A read tool prompts instead of running** → it isn't in `read_only_tools` /
  `tool_grants`. Add it to both (mirrored) and regenerate.
- **Connection refused / transport errors** → `mcp-remote` version or Node issue, or
  the wrong endpoint host. Confirm `POSTHOG_HOST` and that Node is on PATH.

## When NOT to use this

- **Writing to PostHog** (creating insights/dashboards/flags) → out of scope by
  design; this integration is read-only. Use the PostHog UI/API directly.
- **Orders / revenue that already live in Shopify** → the Shopify MCP is the source of
  truth for commerce numbers; PostHog is for product-analytics behavior, funnels, and
  on-site events.
- **Realtime GA-style traffic reports** → PostHog covers product events and funnels;
  for GA4 sessions/channels use the `google-analytics` integration.

## Equipping a bot

In `fleet.yaml`, add the server to the bot's `mcp:` list:

```yaml
bots:
  - name: your-bot
    mcp: [github, shopify, google-analytics, posthog]
```

Set the three fleet `.env` vars (gitignored — real values live only there):

```
POSTHOG_API_KEY=phx_XXXXXXXXXXXXXXXXXXXX
POSTHOG_HOST=mcp.posthog.com
POSTHOG_PROJECT_ID=12345
```

Then `claudlobby generate`. The 13 read-only tools compose in as auto-allowed
`mcp__posthog__*` reads (no write tools, so nothing prompts unattended).
`claudlobby doctor` flags any of the three `.env` vars if missing.
