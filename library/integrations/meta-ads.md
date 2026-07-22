---
title: Meta Ads
type: mcp
env_contract:
  META_ACCESS_TOKEN:
    description: Long-lived Meta Marketing API access token with the ads_read scope (a non-expiring System User token is strongly preferred). Gitignored — mapped to the server's META_ADS_ACCESS_TOKEN.
    tier: fleet
tool_grants:
  - "mcp__meta-ads__meta_ads_list_ad_accounts"
  - "mcp__meta-ads__meta_ads_get_ad_account_details"
  - "mcp__meta-ads__meta_ads_get_activities_by_adaccount"
  - "mcp__meta-ads__meta_ads_get_activities_by_adset"
  - "mcp__meta-ads__meta_ads_get_ad_by_id"
  - "mcp__meta-ads__meta_ads_get_ads_by_adaccount"
  - "mcp__meta-ads__meta_ads_get_ads_by_campaign"
  - "mcp__meta-ads__meta_ads_get_ads_by_adset"
  - "mcp__meta-ads__meta_ads_get_adset_by_id"
  - "mcp__meta-ads__meta_ads_get_adsets_by_ids"
  - "mcp__meta-ads__meta_ads_get_adsets_by_adaccount"
  - "mcp__meta-ads__meta_ads_get_adsets_by_campaign"
  - "mcp__meta-ads__meta_ads_get_campaign_by_id"
  - "mcp__meta-ads__meta_ads_get_campaigns_by_adaccount"
  - "mcp__meta-ads__meta_ads_get_adcreatives_by_adaccount"
  - "mcp__meta-ads__meta_ads_get_ad_creative_by_id"
  - "mcp__meta-ads__meta_ads_get_ad_creatives_by_ad_id"
  - "mcp__meta-ads__meta_ads_compute_image_crops"
  - "mcp__meta-ads__meta_ads_get_adaccount_insights"
  - "mcp__meta-ads__meta_ads_get_campaign_insights"
  - "mcp__meta-ads__meta_ads_get_adset_insights"
  - "mcp__meta-ads__meta_ads_get_ad_insights"
  - "mcp__meta-ads__meta_ads_get_ad_images"
  - "mcp__meta-ads__meta_ads_get_ad_previews"
  - "mcp__meta-ads__meta_ads_get_ad_video"
  - "mcp__meta-ads__meta_ads_get_image_by_hash"
  - "mcp__meta-ads__meta_ads_get_account_pages"
  - "mcp__meta-ads__meta_ads_search_pages_by_name"
  - "mcp__meta-ads__meta_ads_fetch_pagination_url"
  - "mcp__meta-ads__meta_ads_search_interests"
  - "mcp__meta-ads__meta_ads_get_interest_suggestions"
  - "mcp__meta-ads__meta_ads_search_behaviors"
  - "mcp__meta-ads__meta_ads_search_demographics"
  - "mcp__meta-ads__meta_ads_search_geo_locations"
  - "mcp__meta-ads__meta_ads_estimate_audience_size"
---

# Meta Ads

Read-only Meta (Facebook / Instagram) Ads access — the ad-spend ROI a fleet needs
to optimize paid acquisition: impressions, clicks, CTR, CPC, spend, conversions,
and ROAS across the whole hierarchy (ad account → campaign → ad set → ad), plus
creatives, audience/targeting catalogs, and account activity. Backed by the
`meta-ads-mcp-server` server (hashcott) —
<https://github.com/hashcott/meta-ads-mcp-server> (npm: `meta-ads-mcp-server`) —
launched with `npx` and pinned at `meta-ads-mcp-server@1.5.1`. Bots read
performance for any ad account the token can see, using a long-lived access
token: no OAuth dance, no browser. This puts a fleet's paid-media signal — which
campaigns return, which creatives convert, where spend is wasted — directly in
the hands of the bots that act on it, instead of one person reading Ads Manager.

**Server choice.** Picked over the rest of the field:

- Meta publishes no first-party Marketing API MCP.
- `pipeboard-co/meta-ads-mcp` (PyPI, `uvx`) is the more popular, company-backed
  option and reads `META_ACCESS_TOKEN` directly — but it is **write-heavy with no
  read-only mode**: ~9 mutation tools (`create_campaign`, `create_adset`,
  `create_ad`, budget/creative writes) are always registered, and its documented
  path steers you to a hosted broker. Wrong shape for a read-only reader.
- `gomarble-ai/facebook-ads-mcp-server` is the purest read surface (all
  get/list/insights, no writes) but is **not pinnable** — git-clone / Smithery
  only, no released version — and takes the token as a `--fb-token` CLI arg, which
  leaks it into the process args.
- `mikusnuz/meta-ads-mcp` (135 tools) and `oliverames/meta-mcp-server` (200+, incl.
  Conversions API) are full campaign-management servers with no read-only mode —
  far too broad and mutation-capable here.

`meta-ads-mcp-server` is the read-only-by-default, pinnable, env-auth option: its
35 read/util tools are always registered, while its 19 write tools stay
unregistered unless `META_ADS_ENABLE_WRITE_TOOLS` is set (which this fragment
never does). It authenticates from a single env var (so a **System User token**
reuses cleanly), runs first-class via `npx`, and is MIT. Trade-offs: it is a
young, single-maintainer package (pin the exact version and mirror/fork if you
need supply-chain assurance), and it hardcodes Graph API `v22.0` (see gotchas).
We pin `meta-ads-mcp-server@1.5.1` — the 35 granted tool names were verified
against that release's published source.

## Read-only by construction (two layers)

Read-only on two independent layers, so a bot cannot mutate an ad account even if
one layer is misconfigured:

1. **Server-side** — the fragment sets `META_ADS_ENABLE_WRITE_TOOLS=false` (also
   the server's default). The server's `isWriteToolsEnabled()` gate returns false,
   so the **19 write tools are never registered at all** — they do not exist on the
   wire. Those are the mutations: create / update / delete / pause / resume for
   campaigns, ad sets, and ads; `create_budget_schedule`; creative create/update;
   and `upload_ad_image`.
2. **Compositor-side** — the fragment declares `read_only_tools`, so claudlobby
   emits one exact `mcp__meta-ads__<tool>` allow per read tool and **never** a
   `mcp__meta-ads__*` wildcard. Even if someone later flipped the write flag on, the
   write tools are absent from `read_only_tools`, so a headless bot would keep
   prompting on them instead of running them unattended.

Never set `META_ADS_ENABLE_WRITE_TOOLS` truthy, and never add a write tool to a
bot's `tools.allow` — the whole point is a read-only ROI reader. Editing spend or
campaigns is a deliberate, separate, reviewed change.

## Auth model

- **One long-lived access token, `ads_read` scope.** A **System User token** is
  strongly preferred: it does not expire, so a bot never wedges on a dead token. A
  long-lived *user* token (~60 days) works but must be refreshed. The token only
  needs `ads_read` (and `read_insights` for some insight fields) — **not**
  `ads_management`, since writes stay off.
- **`META_ACCESS_TOKEN`** → the server's `META_ADS_ACCESS_TOKEN`: the raw token
  string. Keep it **outside git** — in the fleet `.env` only (gitignored). **Never
  commit it or print its contents.**
- **No ad-account env var.** Unlike a per-property model, the ad account is not
  pinned by env — each tool call takes an `act_id` argument
  (`act_XXXXXXXXX`). The token only sees ad accounts it has been assigned, which is
  the natural guardrail on which accounts a bot can read.
- **Rotate** by replacing the value at `META_ACCESS_TOKEN` in the `.env` and
  restarting the bot; revoke the old token in Meta Business Settings. A System User
  token avoids routine rotation entirely.

## Setup walkthrough (full, generalized)

1. **Have a Meta app with the Marketing API.** At
   <https://developers.facebook.com>, use (or create) a **Business**-type app and
   add the **Marketing API** product. For production ad accounts the app needs
   **Advanced Access** to `ads_read` (Standard/Development access only reaches
   accounts you admin and at a low rate ceiling).
2. **Mint a long-lived token with `ads_read`.** In **Business Settings → Users →
   System Users**, create (or pick) a System User, **Generate New Token** for your
   app, and select the `ads_read` scope (add `read_insights` too). A System User
   token is non-expiring — the recommended path. (Alternatively, exchange a
   short-lived user token for a ~60-day long-lived one via the Graph API, and plan
   to refresh it.)
3. **Assign the ad account to the token.** In **Business Settings → Accounts → Ad
   Accounts**, select the account and **Assign** the System User with at least
   *view performance* access. A token minted **before** the account was assigned
   will not see it — assign first, or regenerate the token after assigning.
4. **Find the ad-account id.** In **Ads Manager** or **Business Settings → Ad
   Accounts** — it is the numeric id shown for the account. Callers pass it as
   `act_<that-number>` (the `act_` prefix is required). This is a per-call tool
   argument, not an env var.
5. **(Optional) note the pixel/dataset id** in **Events Manager** if you plan to
   reason about conversions — but see gotchas: this server has no pixel-stats tool;
   conversion outcomes surface only inside the insights tools.
6. **Configure.** Set the one fleet `.env` var (gitignored — the real token lives
   only there):
   ```
   META_ACCESS_TOKEN=<your-long-lived-ads_read-token>
   ```
7. **Attach the MCP** — add `meta-ads` to a bot's `mcp:` list, then
   `claudlobby generate` (see *Equipping a bot*). The first `npx` run downloads the
   package — warm it once (`npx -y meta-ads-mcp-server@1.5.1 --help`, or
   `claudlobby warm-cache`) so first bot use isn't a cold download. **Verify** on
   first live use with `meta_ads_list_ad_accounts` (confirms the token sees the
   account), then a small `meta_ads_get_adaccount_insights` for the last 7 days.

## Common operations

The fragment grants **35 read-only tools**, grouped by job:

- **Accounts** — `meta_ads_list_ad_accounts` (every account the token can see),
  `meta_ads_get_ad_account_details`. Start here to confirm which `act_` ids are
  reachable.
- **Hierarchy** — campaigns (`meta_ads_get_campaigns_by_adaccount`,
  `meta_ads_get_campaign_by_id`), ad sets (`meta_ads_get_adsets_by_adaccount` /
  `_by_campaign` / `_by_ids`, `meta_ads_get_adset_by_id`), and ads
  (`meta_ads_get_ads_by_adaccount` / `_by_campaign` / `_by_adset`,
  `meta_ads_get_ad_by_id`). Walk the structure before pulling numbers.
- **Insights (the core ROI surface)** — `meta_ads_get_adaccount_insights`,
  `meta_ads_get_campaign_insights`, `meta_ads_get_adset_insights`,
  `meta_ads_get_ad_insights`. These return impressions, clicks, CTR, CPC, and
  spend, and — via the `actions` / `action_values` / `purchase_roas` fields —
  conversions and ROAS. Scope the window with the `date_preset` or `time_range`
  arguments; batch with `time_ranges` rather than looping single days.
- **Creatives & media** — `meta_ads_get_adcreatives_by_adaccount`,
  `meta_ads_get_ad_creative_by_id`, `meta_ads_get_ad_creatives_by_ad_id`,
  `meta_ads_get_ad_images`, `meta_ads_get_ad_previews`, `meta_ads_get_ad_video`,
  `meta_ads_get_image_by_hash`, `meta_ads_compute_image_crops` — to see which
  creative a winning ad ran.
- **Audiences & targeting catalogs** — `meta_ads_search_interests`,
  `meta_ads_get_interest_suggestions`, `meta_ads_search_behaviors`,
  `meta_ads_search_demographics`, `meta_ads_search_geo_locations`,
  `meta_ads_estimate_audience_size` (reach estimation, read-only).
- **Pages & activity** — `meta_ads_get_account_pages`,
  `meta_ads_search_pages_by_name`; `meta_ads_get_activities_by_adaccount` /
  `_by_adset` (change log). `meta_ads_fetch_pagination_url` follows a paging cursor.

Typical ROI flow: `meta_ads_list_ad_accounts` → `meta_ads_get_campaigns_by_adaccount`
→ `meta_ads_get_campaign_insights` (a date range) to find high-spend / low-ROAS
campaigns → drill with `meta_ads_get_adset_insights` / `meta_ads_get_ad_insights` →
`meta_ads_get_ad_creative_by_id` to see which creative is (or isn't) working.

## Gotchas

- **Conversions/ROAS come from insights, not a pixel tool.** This server exposes
  **no** pixel / dataset / custom-conversion tool. Purchase counts and ROAS are
  reachable only as `actions` / `action_values` / `purchase_roas` fields **inside**
  the four `*_insights` tools. If you need raw pixel-event or dataset-level stats,
  this integration does not cover them — use Events Manager or a dedicated tool.
- **Graph API `v22.0` is hardcoded** in the pinned server. Meta deprecates a
  version roughly two years after release, so expect to bump the server (and
  re-verify the tool list) before `v22.0` sunsets.
- **Scope is `ads_read`, plus Advanced Access for production.** A dev-tier app or a
  token missing `ads_read` returns permission errors; production ad accounts need
  the app to hold **Advanced Access** to `ads_read`. `ads_management` is not needed
  (and not wanted) here.
- **Assign the account before minting the token.** A System User token generated
  before the ad account was assigned will not see it — assign the account, then
  (re)generate. Symptom if skipped: `meta_ads_list_ad_accounts` is empty, or an
  `act_` id errors as not found.
- **`act_` prefix is required.** Pass `act_<numeric-id>`; a bare number errors.
- **Rate limits are per account and per app; insights are the heaviest calls.**
  Prefer `date_preset` or batched `time_ranges` over many single-day requests, and
  request Standard/Advanced access for real reporting volume — Development-tier apps
  throttle quickly.
- **`npx` / Node required.** The host needs Node + `npx` on `PATH`. Pre-warm the
  npx cache (`claudlobby warm-cache`) so first bot start isn't a 30–60s download.
- **Verify tool names on a version bump.** The 35 grants match `1.5.1`. If you bump
  the pin, re-confirm the tool list — a rename must update both `read_only_tools`
  (fragment) and `tool_grants` (this doc) together, or generation fails with a
  directional error.

## Failure modes

- **`OAuthException` code 190 ("invalid/expired token")** → the long-lived user
  token expired, or the token was revoked. Switch to a non-expiring System User
  token and update `META_ACCESS_TOKEN`.
- **Empty `meta_ads_list_ad_accounts`** → the token's user / System User has no ad
  account assigned. Assign the account in Business Settings, then regenerate.
- **`(#100)` / "does not exist, cannot be loaded" on an `act_` id** → wrong id,
  missing `act_` prefix, the account isn't assigned to the token, or the app lacks
  Advanced Access to `ads_read`.
- **`(#17)` / `(#80000)` rate-limit** → back off and widen date buckets; reduce
  parallel insights calls.
- **A read tool prompts instead of running** → it isn't in `read_only_tools` /
  `tool_grants`. Add it to both (mirrored) and regenerate.
- **`npx: command not found`** → host missing Node / `npx`.

## When NOT to use this

- **Creating, editing, pausing, or budgeting campaigns / ad sets / ads** → those
  are the gated write tools; this integration is read-only by design. Use Ads
  Manager or a deliberate, reviewed write path.
- **Raw pixel-event or dataset-level conversion stats** → not exposed here (see
  gotchas). Conversion *outcomes* are available via insights; event-level pixel
  data is not.
- **Organic Google Search performance** → the `google-search-console` integration.
- **Site traffic, sessions, acquisition channels, on-site behavior** → the
  `google-analytics` integration; on-site product events / funnels → `posthog`.
- **Orders / revenue** → the Shopify MCP is the source of truth for commerce
  numbers (Meta ROAS is Meta's *attributed* view, not booked revenue).

## Equipping a bot

In `fleet.yaml`, add the server to the bot's `mcp:` list:

```yaml
bots:
  - name: your-bot
    mcp: [github, meta-ads]
```

Set the one fleet `.env` var (gitignored — the real token lives only there):

```
META_ACCESS_TOKEN=<your-long-lived-ads_read-token>
```

Then `claudlobby generate`. The 35 read-only tools compose in as auto-allowed
`mcp__meta-ads__*` reads (the 19 write tools stay unregistered and prompt-gated, so
nothing mutating runs unattended). `claudlobby doctor` flags the `.env` var if
missing.
