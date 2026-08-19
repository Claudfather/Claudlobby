---
title: Google Search Console
type: mcp
env_contract:
  GSC_SA_KEY_PATH:
    description: Absolute path to the Google service-account JSON key granted access to the Search Console property (gitignored). Reuse the google-analytics SA by pointing at the same key file.
    default_tier: fleet
    secret: true
tool_grants:
  - "mcp__google-search-console__list_properties"
  - "mcp__google-search-console__get_site_details"
  - "mcp__google-search-console__get_search_analytics"
  - "mcp__google-search-console__get_advanced_search_analytics"
  - "mcp__google-search-console__get_performance_overview"
  - "mcp__google-search-console__compare_search_periods"
  - "mcp__google-search-console__get_search_by_page_query"
  - "mcp__google-search-console__get_sitemaps"
  - "mcp__google-search-console__list_sitemaps_enhanced"
  - "mcp__google-search-console__get_sitemap_details"
  - "mcp__google-search-console__inspect_url_enhanced"
  - "mcp__google-search-console__batch_url_inspection"
  - "mcp__google-search-console__check_indexing_issues"
  - "mcp__google-search-console__get_creator_info"
  - "mcp__google-search-console__get_capabilities"
---

# Google Search Console

Read-only Google Search Console (GSC) access — the organic-search queries, pages,
countries, and devices that bring a site traffic from Google Search, plus
impressions / clicks / CTR / average position, sitemap status, and URL index
inspection. Backed by the `mcp-search-console` server (AminForou) —
<https://github.com/AminForou/mcp-gsc> (PyPI: `mcp-search-console`) — launched with
`uvx`. Bots read Search performance for any property the service account can see,
using a service-account key: no OAuth dance, no browser. This puts a fleet's SEO
signal — what people search to find the site, which pages rank, where impressions
aren't converting to clicks — directly in the hands of the bots that act on it,
instead of one person reading the Search Console UI.

**Server choice.** Picked over the rest of the field:

- Google publishes no first-party GSC MCP.
- `ahonn/mcp-server-gsc` (npm, service-account, clean `npx`) was the runner-up, but
  its published build exposes only `search_analytics` — no sitemaps, URL inspection,
  or property listing — so it fails the coverage bar.
- `sarahpark/google-search-console-mcp` is the purest read-only match (four tools,
  cannot write, standard `GOOGLE_APPLICATION_CREDENTIALS`) but is effectively
  unmaintained and ships build-from-source, not a ready-to-run artifact.

`mcp-search-console` is the most adopted and maintained GSC server (MIT), covers all
four required surfaces read-only, authenticates with a **service-account key file**
(so it reuses the same Google SA as `google-analytics`), and runs first-class via
`uvx`. Its five mutating tools are disabled by default. Trade-off: it is Python
(needs `uv`/`uvx`, same as `google-analytics`), and its property is passed per call
rather than pinned by env (see gotchas). We pin `mcp-search-console==0.3.2` (latest
on PyPI; the 15 granted tool names were verified against that release's source).

## Read-only by construction (two layers)

Read-only on two independent layers, so a bot cannot mutate Search Console even if
one layer is misconfigured:

1. **Server-side** — the fragment sets `GSC_ALLOW_DESTRUCTIVE=false` (also the
   server's default). The five mutating tools — `add_site`, `delete_site`,
   `submit_sitemap`, `delete_sitemap`, `manage_sitemaps` — are gated behind that flag
   and refuse to run. `GSC_SKIP_OAUTH=true` additionally forces headless
   service-account auth, so there is no interactive browser flow to stall a bot.
2. **Compositor-side** — the fragment declares `read_only_tools`, so claudlobby emits
   one exact `mcp__google-search-console__<tool>` allow per read tool and **never** a
   `mcp__google-search-console__*` wildcard. The five write tools (and
   `reauthenticate`) are known to the compositor but kept off the allow-set, so a
   headless bot keeps prompting on them instead of running them unattended.

Never set `GSC_ALLOW_DESTRUCTIVE=true`, and never add a write tool to a bot's
`tools.allow` — the whole point is a read-only SEO reader.

## Auth model

- **Service account, added as a user on the property.** A GCP service account with a
  JSON key, added to the **Search Console property's** users. No user OAuth. The
  **same service account used by `google-analytics`** works — but Search Console
  authorizes per-property, so you must add the SA's `client_email` to each property
  you want to read (it does **not** inherit GA4 access).
- **`GSC_SA_KEY_PATH`** → the server's `GSC_CREDENTIALS_PATH`: absolute path to the
  JSON key. Keep it **outside git** (e.g. `local/<fleet>/.secrets/<name>.json`,
  `chmod 600`). **Never commit the key or print its contents.** To reuse the GA4
  service account, point `GSC_SA_KEY_PATH` at the *same* file as `GA4_SA_KEY_PATH`.
- **No property env var.** Unlike GA4, the property is not pinned by env — each tool
  call takes a `site_url` argument (`https://example.com/` for a URL-prefix property,
  or `sc-domain:example.com` for a domain property). The SA only sees properties it
  was added to, which is the natural guardrail on which sites a bot can read.
- **Rotate** by replacing the key file at `GSC_SA_KEY_PATH` (path unchanged) and
  restarting the bot; revoke the old key in GCP IAM.

## Setup walkthrough (full, generalized)

1. **Verify the property in Search Console.** Go to
   <https://search.google.com/search-console> and click **Add property**. You pick a
   **property type**, then a **verification method** — this pair is the single
   biggest place first-timers get stuck, so choose deliberately:

   **Property type:**
   - **Domain property** (`sc-domain:example.com` — covers every subdomain and both
     `http`/`https`): the most complete for whole-site SEO, but it can **only** be
     verified by adding a `TXT` record to the domain's DNS. Choose it if you control
     DNS and want everything under one property.
   - **URL-prefix property** (`https://example.com/` — one exact origin): more
     verification options, and the path of least resistance when DNS is awkward.

   **Verification method — the wall.** For a URL-prefix property the offered methods
   are **not** equally reliable on a modern, consent-gated storefront:
   - **HTML `<meta>` tag (recommended here):** a `google-site-verification` meta in
     the page `<head>`. It renders server-side, so Google's verifier finds it in the
     raw HTML even when the rest of the page is client-rendered.
   - **HTML-file upload:** serve the supplied `google*.html` file at the site root.
     Fine for static hosts, but on a framework app you must actually route that exact
     file, or verification fails with **"file not found."**
   - **Google Analytics / Tag Manager:** **fails on a consent-gated site.** The
     verifier reads raw HTML, and a GA tag that injects only *after* cookie consent
     isn't there yet — so this reports failure even though GA is wired correctly. Use
     the meta tag instead.

   **Verify with the method you actually deployed.** The most common self-inflicted
   failure is deploying the **meta tag** but then clicking **Verify** under the
   **HTML-file** method (or the reverse): Google checks for the file, doesn't find it,
   and reports "not found." Deploy one method, then verify *that same* method.
2. **Enable the Search Console API** on the service account's GCP project
   (<https://console.cloud.google.com> → APIs & Services → Library → **Search Console
   API** → Enable, or `gcloud services enable searchconsole.googleapis.com`). This is
   a **separate** API from the GA4 Data / Admin APIs — enabling those does not enable
   this one, and a missing enable makes every call 403 with a service-disabled error.
3. **Get a service account + JSON key.** Reuse the one from `google-analytics`
   (recommended — one SA, one key), or create a new one (IAM & Admin → Service
   Accounts → Keys → Add key → JSON). Store the key gitignored
   (`local/<fleet>/.secrets/<name>.json`, `chmod 600`) — never commit it.
4. **Add the service account as a user on the property.** In Search Console, open
   **Settings** — the **gear icon at the *bottom-left* of the left sidebar**, below
   "Achievements" (it is easy to miss and is *not* in the top nav) — then **Users and
   permissions → Add user**. Paste the SA's `client_email`
   (`svc@your-project.iam.gserviceaccount.com`, found inside the JSON key) and set a
   permission level:
   - **Restricted** is enough for search-analytics reads and sitemap listing.
   - **Full** is required for **URL Inspection** (`inspect_url_enhanced` /
     `batch_url_inspection`). Add the SA as **Full** if you want URL inspection;
     otherwise those calls 403 while every other read still works.

   This grant is **per-property** and does **not** inherit GA4 access — the *same*
   service account must still be added here explicitly (see gotchas).
5. **Configure.** Set the one fleet `.env` var (gitignored — real path lives only
   there):
   ```
   GSC_SA_KEY_PATH=/abs/path/to/local/<fleet>/.secrets/<name>.json
   ```
   To reuse the GA4 service account, this is the *same* path as `GA4_SA_KEY_PATH`.
6. **Attach the MCP** — add `google-search-console` to a bot's `mcp:` list, then
   `claudlobby generate` (see *Equipping a bot*). The first `uvx` run downloads the
   package into the uv cache — warm it once (`uvx --from mcp-search-console==0.3.2
   mcp-search-console --help`, or `claudlobby warm-cache`) so first bot use isn't slow.
7. **Verify the connection.** On first live use, run `list_properties` to confirm the
   SA sees the property, then a small `get_search_analytics` for the last 7 days on
   its `site_url`. A successful authenticated response is the proof — see gotchas on
   why a fresh property returns few rows.

## Navigating the GCP Console (service-account setup)

Steps 2–3 happen in the Google Cloud Console
(<https://console.cloud.google.com>), which is easy to get lost in — everything
below is **per-project**, so watch the project selector.

**The service-account setup, as an ordered checklist.** Do these in order; skip or
misplace any one and you get a `403` whose message usually names the missing step
("API has not been used…", "permission denied"):

1. **Enable the API** — GSC needs the **Search Console API**
   (`searchconsole.googleapis.com`). This is *separate* from GA4's Data + Admin APIs.
   (PostHog and Meta Ads need none of this — just a token, no console at all.)
2. **Create or locate the service account**, and **download its JSON key**.
3. **Grant the SA on the property** — add its `client_email` under the property's
   *Users and permissions* (step 4). This is the step people miss.

**Where to click:**

- **Select the right project first** — the picker is the dropdown at the top-left,
  next to "Google Cloud." APIs, service accounts, and keys all live inside the
  selected project; doing a step in the wrong project is a common *silent* failure.
- **Enable an API:** APIs & Services → Library → search the name → click it →
  **Enable**.
- **Create a service account:** APIs & Services → Credentials → Create credentials →
  Service account.
- **Download its key:** click the service account → Keys → Add key → **JSON** →
  download. That JSON file *is* the secret — gitignore it, never commit it.
- **Confirm what's enabled:** APIs & Services → **Enabled APIs & services** lists
  everything currently on — the fastest way to check you didn't miss one.

## Common operations

The fragment grants **15 read-only tools**, grouped by job:

- **Properties & capabilities** — `list_properties` (every property the SA can see),
  `get_site_details`; `get_capabilities` / `get_creator_info` report what the server
  and account can do. Start here to confirm which sites are reachable.
- **Search analytics** — `get_search_analytics` is the workhorse: clicks,
  impressions, CTR, and average position by dimension (query, page, country, device,
  date) over a date range. `get_advanced_search_analytics` adds filtering (e.g.
  queries on one page, or one country's devices). `get_performance_overview` and
  `compare_search_periods` summarize and diff two windows; `get_search_by_page_query`
  pivots queries under a page.
- **Sitemaps** — `get_sitemaps` / `list_sitemaps_enhanced` list submitted sitemaps
  with status; `get_sitemap_details` drills into one (errors, warnings, last read).
- **Indexing** — `inspect_url_enhanced` reports a URL's crawl / index / coverage
  state (needs Full permission); `batch_url_inspection` does up to 10 URLs at once;
  `check_indexing_issues` surfaces problems across URLs.

Typical SEO flow: `get_search_analytics` (query dimension) to find high-impression /
low-CTR queries → `get_advanced_search_analytics` to see which page ranks for them →
`inspect_url_enhanced` to confirm that page is indexed cleanly.

## Gotchas

- **Install `mcp-search-console`, NOT `mcp-gsc`.** A separate, unmaintained PyPI
  package named `mcp-gsc` exists and is **not** this project. The fragment pins
  `mcp-search-console==0.3.2` — keep it that way.
- **Verification: the "Google Analytics" method fails on a consent-gated site.**
  Google's ownership verifier reads the **raw** server HTML. A GA tag that injects
  only *after* cookie consent isn't in that HTML, so the GA verification method
  reports failure even though GA is wired correctly. Use the **HTML `<meta>` tag** (it
  renders in the SSR `<head>`) or the **HTML-file** method — and **verify with the
  method you actually deployed** (deploying the meta tag but clicking Verify under
  HTML-file, or the reverse, is the classic "file not found" self-own).
- **Redirect + `curl` check: use `-L`.** If the site redirects root→www (or
  www→root), the verification tag must live on the **redirect target**, and Google
  follows the redirect to check it. When you sanity-check the deployed tag with
  `curl`, pass **`-L`** so it follows the redirect — a `-L`-less `curl` on the
  redirecting host returns a tiny "Redirecting…" stub with no tag, which *looks* like
  a broken or empty deploy when the tag is actually fine on the target.
- **The SA must be added to each property.** Search Console authorizes per-property
  and does **not** inherit Google Analytics access. Reusing the GA4 SA still requires
  adding its `client_email` as a user on every GSC property you want to read.
  Symptom if skipped: `list_properties` returns empty, or the property 403s.
- **URL Inspection needs Full permission.** Search-analytics and sitemap listing work
  with a Restricted user, but the URL Inspection API requires **Full** on the
  property. If `inspect_url_enhanced` 403s while other reads succeed, that's the cause
  — re-add the SA as Full.
- **Enable the Search Console API** (`searchconsole.googleapis.com`) on the SA's
  project. It is distinct from the GA4 Data / Admin APIs — a fleet already running
  GA4 still has to enable this one, or every call 403s with a service-disabled error.
- **`site_url` format matters.** URL-prefix properties need the exact prefix
  *including the trailing slash* (`https://example.com/`); domain properties use the
  `sc-domain:example.com` form (no scheme, no slash). A mismatch returns "not found"
  even when the SA has access.
- **`uvx` / `uv` + Python required** (same prerequisite as `google-analytics`). The
  host needs `uv` on PATH. The `npx` cache-warmer (`check-npx-cache.sh`) does **not**
  cover uvx packages — pre-warm the uv cache separately so first bot start isn't a
  cold download.
- **A freshly-verified property returns `200` but ~zero rows for 24–48h.** Search
  Console backfills a new property over a day or two. An authenticated `200` proves
  *access*; rows prove *data*, and the data lags. Access-proven ≠ data-yet — say so,
  rather than reporting the integration broken.
- **Verify tool names on a version bump.** The 15 grants match `0.3.2`. If you bump
  the pin, re-confirm the tool list — a rename must update both `read_only_tools`
  (fragment) and `tool_grants` (this doc) together, or generation fails with a
  directional error.

## Failure modes

- **Empty `list_properties`** → the SA was never added as a user on any property (or
  the wrong SA key). Add its `client_email` in Search Console → Users and permissions.
- **`403` on `inspect_url_enhanced` only** → the SA is Restricted, not Full. URL
  Inspection needs Full permission.
- **`403` service-disabled ("Search Console API has not been used…")** → enable
  `searchconsole.googleapis.com` on the SA's project; propagation takes a few minutes.
- **`site_url` "not found"** → wrong property form (missing trailing slash on a
  URL-prefix property, or missing `sc-domain:` on a domain property), or the SA can't
  see that property.
- **`uvx: command not found`** → host missing `uv` / `uvx`.
- **A read tool prompts instead of running** → it isn't in `read_only_tools` /
  `tool_grants`. Add it to both (mirrored) and regenerate.

## When NOT to use this

- **Submitting or deleting sitemaps / sites** → those are the gated write tools; this
  integration is read-only by design. Use the Search Console UI or a dedicated write
  path.
- **GA4 traffic, sessions, acquisition channels, on-site behavior** → use the
  `google-analytics` integration. GSC is *Google Search* performance (organic
  queries, rankings, impressions), not site analytics.
- **On-site product events / funnels** → the `posthog` integration.
- **Orders / revenue** → the Shopify MCP is the source of truth for commerce numbers.

## Equipping a bot

In `fleet.yaml`, add the server to the bot's `mcp:` list:

```yaml
bots:
  - name: your-bot
    mcp: [github, google-analytics, google-search-console]
```

Set the one fleet `.env` var (gitignored — the real path lives only there; reuse the
GA4 SA by pointing at the same key file):

```
GSC_SA_KEY_PATH=/abs/path/to/local/<fleet>/.secrets/<name>.json
```

Then `claudlobby generate`. The 15 read-only tools compose in as auto-allowed
`mcp__google-search-console__*` reads (the five write tools stay prompt-gated, so
nothing mutating runs unattended). `claudlobby doctor` flags the `.env` var if
missing.
