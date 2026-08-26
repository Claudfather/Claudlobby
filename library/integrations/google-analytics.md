---
title: Google Analytics
type: mcp
env_contract:
  GA4_SA_KEY_PATH:
    description: Absolute path to the GA4 service-account JSON key file (gitignored)
    default_tier: fleet
    secret: true
  GA4_PROPERTY_ID:
    description: Numeric GA4 property ID of the store property (not the G-XXXX measurement ID)
    default_tier: fleet
    secret: false
tool_grants:
  - "mcp__google-analytics__get_ga4_data"
  - "mcp__google-analytics__search_schema"
  - "mcp__google-analytics__get_property_schema"
  - "mcp__google-analytics__list_metric_categories"
  - "mcp__google-analytics__list_dimension_categories"
  - "mcp__google-analytics__get_troubleshooting_guide"
---

# Google Analytics

Read-only GA4 reporting via the **Google Analytics 4 Data API**. Backed by the
`google-analytics-mcp` server (surendranb) — <https://github.com/surendranb/google-analytics-mcp>
(PyPI + npm: `google-analytics-mcp`) — launched with `uvx`. Bots pull
sessions / users / channels / on-site behavior for a single **pinned** GA4 property
using a service-account key: no OAuth dance, no browser.

**Server choice:** picked over Google's official `analytics-mcp`
(<https://github.com/googleanalytics/google-analytics-mcp> — experimental, pre-1.0,
no property-ID pinning) and the archived `ruchernchong/mcp-server-google-analytics`.
It is the most actively maintained of the field, runs first-class via `uvx`/`npx`,
authenticates with a service-account **key file**, and — critically — pins the
property via a `GA4_PROPERTY_ID` env var so a bot can never query the wrong property.
Trade-off: no realtime report (batch only). If you need realtime, use Google's
official server instead.

## Auth model

- **Service account, Viewer role.** A GCP service account with a JSON key, granted
  **Viewer** on the GA4 *property*. No user OAuth.
- **`GA4_SA_KEY_PATH`** → the server's `GOOGLE_APPLICATION_CREDENTIALS`: absolute path
  to the JSON key. Keep it **outside git** (e.g. `local/<fleet>/.secrets/<name>.json`,
  `chmod 600`). **Never commit the key or print its contents.**
- **`GA4_PROPERTY_ID`** → the server's `GA4_PROPERTY_ID`: the numeric property ID
  (bare number). Both are fleet-tier `.env` vars.
- **Rotate** by replacing the key file at `GA4_SA_KEY_PATH` (path unchanged) and
  restarting the bot; revoke the old key in GCP IAM.

## Setup walkthrough (full, generalized)

1. **GCP project.** Use an existing project or create one (`your-project`). This is the
   project the service account and API quota live in.
2. **Enable BOTH GA4 APIs on that project** (APIs & Services → Library, or
   `gcloud services enable`):
   - `analyticsdata.googleapis.com` — **GA4 Data API** (required for reporting).
   - `analyticsadmin.googleapis.com` — **GA4 Admin API** (required to *discover /
     verify* the numeric property ID in step 5, and for property metadata).

   > **Gotcha (bites everyone):** if an API is not enabled, every call 403s with
   > `SERVICE_DISABLED` — *"… API has not been used in project N … before or it is
   > disabled."* It's easy to enable only one of the two, or to enable them on a
   > different project than the service account uses. Enable **both**, on the SA's
   > project; enabling propagates within a few minutes.
3. **Create a service account** in that project (IAM & Admin → Service Accounts),
   e.g. `svc@your-project.iam.gserviceaccount.com`.
4. **Download its JSON key** (Keys → Add key → JSON). Store it gitignored
   (`local/<fleet>/.secrets/<name>.json`, `chmod 600`) — never commit it.
5. **Add the service account as Viewer on the GA4 property** (GA4 Admin → Property
   Access Management → add `svc@your-project.iam.gserviceaccount.com` with the
   **Viewer** role). This is a *GA4 property* grant, separate from GCP IAM.
6. **Get the numeric property ID — and make sure it's the RIGHT property.** This is
   the #1 GA4 wall: an account often holds several look-alike properties (a real
   store property and a dead one auto-created by a link-in-bio tool), and the wrong
   Measurement ID gets wired first. The property ID is **not** the `G-XXXXXXXXXX`
   measurement ID (see gotchas). Find and confirm it:
   - **Read the ID:** GA4 **Admin → Property Settings → Property details** shows the
     numeric property ID, or list every property via the Admin API once enabled:
     `GET https://analyticsadmin.googleapis.com/v1beta/accountSummaries`
     (each `propertySummaries[].property` = `properties/<numericId>`).
   - **Confirm it's the store's, by data stream:** GA4 **Admin → Data Streams →** the
     web stream **→** its **Measurement ID**. The right property is the one whose
     stream Measurement ID equals the `G-XXXXXXXXXX` **actually installed on the live
     store** (view the storefront's gtag, or check the theme / tag config). Via the
     API: GET `…/v1beta/{property}/dataStreams` and match `webStreamData.measurementId`.
   - **Still unsure?** Query the Data API with the `hostName` dimension — the real
     property reports your storefront domain; the junk one reports the link-in-bio
     domain.
7. **Set the two `.env` vars** (fleet `.env`, gitignored): `GA4_SA_KEY_PATH`,
   `GA4_PROPERTY_ID`.
8. **Wire the MCP** — add `google-analytics` to a bot's `mcp:` list, `claudlobby
   generate` (see *Equipping a bot*). The first `uvx` run downloads the package into
   the uv cache — warm it once so first bot use isn't slow.

## Navigating the GCP Console (service-account setup)

Steps 1–5 span the Google Cloud Console (<https://console.cloud.google.com>) and
the GA4 Admin UI, both easy to get lost in — everything in the Cloud Console is
**per-project**, so watch the project selector.

**The service-account setup, as an ordered checklist.** Do these in order; skip or
misplace any one and you get a `403` whose message usually names the missing step
("API has not been used…", "permission denied on the property"):

1. **Enable the API(s)** — GA4 needs **two**: the **Data API**
   (`analyticsdata.googleapis.com`) for reporting *and* the **Admin API**
   (`analyticsadmin.googleapis.com`) to discover/verify the property ID. Enabling one
   and missing the other is the classic trap. (GSC needs the Search Console API;
   PostHog and Meta Ads need none of this — just a token, no console at all.)
2. **Create the service account**, and **download its JSON key**.
3. **Grant the SA on the property** — GA4 **Admin → Property Access Management**, add
   its `client_email` as **Viewer**. This is a GA4-property grant, *not* GCP IAM, and
   it's the step people miss.

**Where to click (Cloud Console):**

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

The server exposes **6 read-only tools**:

- **`get_ga4_data`** — the workhorse: run a report with dimensions, metrics, date
  ranges, and optional filters (e.g. last-28-day `sessions` / `activeUsers`, or
  `sessionDefaultChannelGroup` × `conversions`).
- **`search_schema`** — keyword-search 200+ GA4 dimension/metric API names. Use this
  *before* building a report so you don't guess field names.
- **`get_property_schema`** — list the dimensions/metrics available on THIS property
  (including custom ones).
- **`list_metric_categories`** / **`list_dimension_categories`** — browse the catalog
  by category (User, Session, Revenue, Event / Geography, Traffic Source, Device).
- **`get_troubleshooting_guide`** — self-healing guide for IAM / setup / filter-syntax
  errors.

Typical flow: `search_schema` or `get_property_schema` to find exact field names →
`get_ga4_data` for the report.

## Gotchas

- **Property ID ≠ Measurement ID.** The Data API wants the *numeric property ID*
  (e.g. `123456789`). The `G-XXXXXXXXXX` wired into the storefront gtag is the
  *data-stream measurement ID* — a different identifier. Passing `G-…` to the API
  fails.
- **Wrong-property / same-name trap.** A store often has more than one GA4 property
  with a nearly identical display name — e.g. a real store property AND a junk
  secondary property auto-created by a link-in-bio tool (hoo.be, Linktree, etc.),
  both under the same account ("Property of …"). **Display name is not reliable.**
  Confirm the property by its data stream's `measurementId` matching your store's
  `G-XXXXXXXXXX` (Admin API `dataStreams`, or GA4 Admin → Data Streams). If you can
  query the Data API but aren't sure, run `get_ga4_data` with the `hostName`
  dimension — the store property reports your storefront domain; the junk one reports
  the link-in-bio domain. Pinning the wrong property = a bot reporting on junk
  traffic that looks plausible. The mirror-image failure is wiring the wrong
  `G-XXXXXXXXXX` into the *storefront itself* — analytics then looks configured but
  the intended property never populates; confirm the installed measurement ID
  against the stream you pinned.
- **Both APIs must be enabled** (see setup). The Admin API is easy to forget because
  *reporting* only needs the Data API — but you need Admin to *find / verify* the
  property ID.
- **Prefer `uvx` over `npx`.** The package is Python (`requires-python >=3.10`); the
  `npx` form is a Node shim that bootstraps Python under the hood and doesn't declare
  the Python prereq. `uvx --from google-analytics-mcp==2.8.1 ga4-mcp-server` is
  explicit and hermetic. Host needs `uv`/`uvx` + Python ≥3.10.
- **Consent-gating → near-zero data is normal right after go-live.** GA4 collection
  fires only after cookie consent, and only once the stream is live — a fresh
  property returns small/empty reports. A successful authenticated `200` is the proof
  the integration works, not the row count.
- **The gtag is client-injected — don't verify it from raw HTML, and expect a
  backfill lag.** The `G-XXXXXXXXXX` tag loads client-side (and only post-consent),
  so it is **not** in the page's raw HTML — "View Source" or a plain `curl` won't
  find it, and that absence does *not* mean it's broken. To actually prove the tag
  fires, watch a **Realtime** view during a consented visit (open the store, accept
  cookies): GA4 UI **Reports → Realtime**, or Google's official realtime MCP — **this
  batch server has no realtime tool** (it is Data-API only). Historical `get_ga4_data`
  reports then lag ~24–48h while GA backfills, which is normal, not a failure.

## Failure modes

- **`403 SERVICE_DISABLED`** (*"… API has not been used in project … or it is
  disabled"*) → the Data and/or Admin API isn't enabled on the project. Enable it,
  wait a few minutes.
- **`403 PERMISSION_DENIED`** (not service-disabled) → the service account lacks
  Viewer on the property, or you're querying a property it can't see. Re-check
  Property Access Management and that `GA4_PROPERTY_ID` is a property the SA was
  granted.
- **Empty / mismatched data** → likely the wrong property (same-name trap), or
  genuinely no traffic yet.
- **`uvx: command not found` / Python errors** → host missing `uv` or Python ≥3.10.

## When NOT to use this

- **Realtime dashboards** → Google's official `analytics-mcp` (`run_realtime_report`).
- **Editing GA4 config** (streams, custom dimensions) → this server is read-only by
  design; use the GA4 Admin UI/API directly.
- **Orders / revenue that already live in Shopify** → the Shopify MCP's sales and
  conversion reports are the source of truth for commerce numbers; GA4 is for
  traffic, acquisition channels, and on-site behavior.

## Equipping a bot

In `fleet.yaml`, add the server to the bot's `mcp:` list:

```yaml
bots:
  - name: your-bot
    mcp: [github, shopify, google-analytics]
```

Set the two fleet `.env` vars (gitignored — real values live only there):

```
GA4_SA_KEY_PATH=/abs/path/to/local/<fleet>/.secrets/<name>.json
GA4_PROPERTY_ID=123456789
```

Then `claudlobby generate`. The 6 read-only tools compose in as auto-allowed
`mcp__google-analytics__*` reads (no write tools, so nothing prompts unattended).
`claudlobby doctor` flags either `.env` var if missing.
