---
title: Meta Business (Instagram)
type: mcp
# No env_contract here on purpose: both vars are declared by the paired
# library/mcp/meta-business.json fragment, and library/integrations/README.md
# says to add env_contract only for vars a fragment does not already declare.
# Repeating them makes required_vars return each twice.
tool_grants:
  - "mcp__meta-business__get_profile_info"
  - "mcp__meta-business__get_account_pages"
  - "mcp__meta-business__get_account_insights"
  - "mcp__meta-business__validate_access_token"
  - "mcp__meta-business__get_media_posts"
  - "mcp__meta-business__get_media_insights"
  - "mcp__meta-business__get_content_publishing_limit"
  - "mcp__meta-business__get_comments"
  - "mcp__meta-business__get_conversations"
  - "mcp__meta-business__get_conversation_messages"
  - "mcp__meta-business__search_hashtag"
  - "mcp__meta-business__get_hashtag_media"
  - "mcp__meta-business__get_stories"
  - "mcp__meta-business__get_mentions"
  - "mcp__meta-business__business_discovery"
---

# Meta Business (Instagram)

Read-only Instagram Graph API access for an Instagram Professional (Business or
Creator) account — profile and account insights, published media and its
performance, comments, hashtag and mention discovery, and **direct-message
conversations**. Backed by `@mcpware/instagram-mcp`
(<https://github.com/mcpware/instagram-mcp>), launched with `npx` and pinned at
`@mcpware/instagram-mcp@1.0.4`.

The DM surface is the reason this fragment exists: `get_conversations` and
`get_conversation_messages` let a bot read the inbox of a business account, so
customer questions, order chases and sales conversations become fleet-legible
instead of living in one person's phone.

## Requires the Facebook Login path — not Instagram Login

Meta offers two ways to reach an Instagram Professional account, and **this
server only supports the older one**:

| | Instagram API with **Facebook Login** | Instagram API with **Instagram Login** |
|---|---|---|
| host | `graph.facebook.com` | `graph.instagram.com` |
| linked Facebook Page | **required** | not required |
| conversations via | `<page-id>/conversations` | `<ig-id>/conversations` |
| scopes | `instagram_manage_messages`, … | `instagram_business_manage_messages`, … |

`@mcpware/instagram-mcp@1.0.4` hardcodes `graph.facebook.com` (**zero**
occurrences of `graph.instagram.com` in `dist/`), and `getConversations` resolves
the inbox by calling `me/accounts` for Facebook Pages. Under Instagram Login there
are no Pages, so the DM tools cannot work at all — the token is for the wrong host
and the lookup has nothing to find. Its README also still cites the pre-2025 scope
names, which is consistent with it targeting the older path.

**So equipping this fragment commits you to creating and linking a Facebook
Page.** If you would rather use Instagram Login — newer, simpler, no Page — this
is the wrong server and the fragment needs re-pointing at `graph.instagram.com`
against one that supports it.

## App Review is probably not required

Meta's conversations documentation draws the line by *ownership*, not by feature:

> Advanced Access if your app serves Instagram professional accounts you don't
> own or manage … Standard Access if your app serves Instagram professional
> accounts you own or manage and have added to your app in the App Dashboard.

A fleet reading **its own** business inbox is the second case, so Standard Access
should cover it and App Review should not be on the critical path. Advanced Access
— and the App Review, screencast and Business Verification that come with it — is
for serving *other people's* accounts.

Verify against the live console before planning around either answer; this was
established from documentation, not from a working credential.

## Auth model

**Two** env vars, both fleet-tier, declared in the paired fragment.
`INSTAGRAM_ACCESS_TOKEN` is the credential; `INSTAGRAM_BUSINESS_ACCOUNT_ID`
identifies the account and maps to the server's own `INSTAGRAM_ACCOUNT_ID`.

Deliberately **not** reusing `META_ACCESS_TOKEN` from `meta-ads.json`: that token
carries `ads_read` for the Marketing API, this one carries Instagram permissions,
and collapsing them into one credential widens the blast radius of a leak for no
benefit.

**No app secret.** The server reads exactly three env vars
(`INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_ACCOUNT_ID`, `INSTAGRAM_API_VERSION`) — the
strings `FACEBOOK`, `APP_SECRET` and `refresh` appear **zero** times in its
`dist/`. It has no token-refresh path. An earlier draft of this fragment passed
`FACEBOOK_APP_ID` + `FACEBOOK_APP_SECRET` "for refresh"; that was wrong, and it
would have put the highest-value Meta credential — an app secret can mint tokens —
into a third-party process that never reads it.

**Refreshing is an operator task, not a server capability.** Long-lived tokens
last ~60 days; renew them yourself against the Graph API before expiry.

## Server choice

Picked over a crowded and uneven field. The decisive split is not feature set — it
is **how the server authenticates**:

- **Session-cookie scrapers** — `kynuxdev/mcp-instagram-dm`,
  `trypeggy/instagram_dm_mcp` and several like them advertise *"cookie-based
  authentication — no API keys, no OAuth."* That phrasing means driving
  Instagram's private endpoints with a harvested session cookie. It violates
  Instagram's ToS (suspension is the routine outcome, not the edge case), hands a
  third-party package a credential with **full account authority, no scopes and no
  revocation short of a password reset**, and breaks whenever Meta changes an
  internal endpoint. These are the servers that appear to delete the App Review
  wait. They do not — they convert a scheduling problem into an account-loss
  problem. **Rejected on the merits.**
- `jlbadano/ig-mcp` — legitimate Graph API server with the right DM surface, but
  distributed as source rather than a pinnable published package.
- SetSmart — commercial, official Messaging API. Reasonable if you want a vendor
  on the hook; not evaluated here.
- `@mcpware/instagram-mcp` — **chosen**: official Graph API tokens, published and
  pinnable on npm, 23 tools with a clean read/write boundary that maps directly
  onto this repo's `read_only_tools` split.

**Disclosure: the chosen server derives from the rejected one.** Its README
describes it as a *"TypeScript rewrite of `jlbadano/ig-mcp` (Python)"*. So these
are not two independently-engineered options — it explains the identical DM
surface, and it means the pick is a transcription of `ig-mcp` that happens to be
published and pinnable. Pinnability is the real reason to prefer it, not
independent engineering quality.

## Read-only posture — one layer, not two

**Read this before assuming parity with `meta-ads`.** That integration is
read-only on *two* independent layers: the server itself refuses to register its
write tools unless `META_ADS_ENABLE_WRITE_TOOLS` is set, *and* the compositor
grants only reads.

This server has **no known server-side write-disable switch**. So there is exactly
one layer here — the compositor's. The eight write tools (`publish_media`,
`publish_carousel`, `publish_reel`, `post_comment`, `reply_to_comment`,
`delete_comment`, `hide_comment`, `send_dm`) *are* registered and callable.

What the single layer buys: they are absent from `read_only_tools`, so no allow
pattern is composed for them and no server wildcard is ever emitted. A bot that
tries one hits a permission prompt. Unattended, that means it **hangs rather than
posts** — which is the safety property, and it is weaker than the tool not
existing. Treat `send_dm` in particular as one config mistake away, not two.

A bot that genuinely needs to reply to customers gets `send_dm` explicitly via
fleet.yaml `tools.allow`, and should carry `confirm-before-send` when it does.

## Customer DMs are customer PII

An inbox contains names, shipping addresses, order numbers, complaints and
payment chatter. `pii-protection` already forbids customer PII in runtime output,
and a bot that reads DMs and reports into a Telegram group runs straight at that
rule.

Compose `pii-protection` on any bot equipping this fragment, and hold the line in
practice: **summarise, never quote**; identifiers redacted to last 4; no buyer
names or addresses in chat, briefings or logs. Decide this before the first token
exists, not after the first briefing leaks an address.

## Common operations

- **Find who bought something** — `get_conversations`, then
  `get_conversation_messages` on the match. Note the caveat below: the order
  record in Shopify/Printify is usually the better source of truth for a sale.
- **Inbox triage** — `get_conversations` for the open threads; summarise, don't quote.
- **Account health** — `get_account_insights`, `get_media_insights`.
- **Reputation sweep** — `get_comments`, `get_mentions`.
- **Token check** — `validate_access_token`, cheapest way to tell expiry from a real fault.

## Gotchas

- **Check your access level before assuming a wait.** See "App Review is probably
  not required" above: reading an inbox you own should fall under Standard Access.
  Advanced Access — with App Review, a screencast and Business Verification — is
  for serving accounts you don't own. Don't plan a multi-week rollout without
  first confirming which case you're in.
- **~60-day token expiry.** Long-lived tokens are not permanent. Refresh before
  expiry or the whole fragment goes dark at once; `validate_access_token`
  distinguishes an expired token from a broken call.
- **Business/Creator account required**, historically with a linked Facebook Page.
  Meta changes these prerequisites often — confirm against their live docs rather
  than this file.
- **DM history is not an archive.** The Messaging API is built for ongoing
  customer conversations, not searching back through old sales. A months-old
  purchase may simply not be reachable.
- **`send_dm` has a messaging window.** Meta restricts unprompted outbound; a
  reply outside the allowed window is refused regardless of permissions.
- **`get_conversations` silently picks the first Page.** Called without
  `page_id`, `client.js` fetches `me/accounts` and takes `pages[0].id`. On a
  business with more than one Page that is a **wrong-inbox read** with no error.
  Always pass `page_id` explicitly. (It also throws `No Facebook pages found`
  when `me/accounts` is empty — that fires before any Advanced Access check, so
  don't read it as a permissions problem.)
- **The fragment straddles two Graph API versions.** `client.js` defaults to
  `v19.0` (Jan 2024, at or past Meta's ~2-year sunset) while the DM path
  hardcodes `v22.0`. The fragment therefore pins `INSTAGRAM_API_VERSION=v22.0`
  to match the DM path rather than ship a sunset default. **Unverified against
  the live API** — no credential existed to test it. Check Meta's current
  version before going live and re-pin if needed.
- **Reads are cached for 300s.** `validate_access_token` correctly opts out, so
  it stays a truthful liveness check, but other reads can be up to five minutes
  stale. Don't build a "did it just arrive?" loop on them.
- **The client throttles by sleeping, not erroring.** It enforces ~200 calls/hr
  with a timer, so a bulk read stalls silently instead of failing fast. A sweep
  that seems hung may just be waiting.
- **The token travels in the query string** on every request, including POSTs —
  so it can land in any intermediary or proxy log.

## Failure modes

- `190` / `OAuthException` → token expired or revoked. Refresh; do not retry.
- `10` / `200` permission errors → the permission is not on the token, almost
  always `instagram_manage_messages` still pending App Review.
- `4` / `17` rate limits → Graph API throttling; back off, don't parallelise harder.
- Empty `get_conversations` → often Advanced Access missing rather than an empty
  inbox. Check `validate_access_token` before concluding there are no threads.

## When NOT to use this

- **To answer "who bought this?"** — reach for the order record first.
  `library/integrations/shopify.md` and `printify.md` carry the buyer, the amount
  and the address as structured data. DM archaeology is slower, less reliable and
  drags PII through the bot for a fact the store already knows.
- **For personal Instagram accounts** — the API covers Professional accounts only.
  There is no compliant path to a personal inbox, which is exactly what the
  cookie-scraper servers exist to sell you.

## Supply chain

`@mcpware/instagram-mcp@1.0.4`: single maintainer, ~51 KB unpacked, ~541
downloads/month as of 2026-08-01 — comparable adoption to `meta-ads-mcp-server`
(~818/month), which this repo already ships.

Read the release history precisely, because "4 published versions" flatters it.
npm's `time` map shows **five** versions — `1.0.0` through `1.0.4` — all inside
about twenty hours on 2026-03-18/19, with `1.0.3` **unpublished roughly 100
seconds after it went out**. `1.0.4` has been untouched since. So: one publish
burst, one retraction, then ~4.5 months of silence — not steady iteration.

Pin the exact version; mirror or fork if you need real supply-chain assurance
before handing it a business token.

**Source-verified — the tool surface.** The 23 tool names were checked against the
published `1.0.4` tarball, not just the project's README: `dist/index.js`
registers exactly those 23 and no others. The read/write split was verified the
same way — `dist/client.js` has exactly **15 GET-issuing tool methods**, matching
`read_only_tools` one-for-one, while the 8 write methods issue `POST`/`DELETE`.
(Count the methods, not `"GET"` literals: a naive grep returns 17 because two are
cache lookups inside `request()`.) The three reads whose names don't look like
reads (`validate_access_token` → `GET me`, `search_hashtag` →
`GET ig_hashtag_search`, `business_discovery` → `GET <account-id>`) are signed off
in `tests/test_readonly_mcp_grants.py::VERIFIED_NONSTANDARD_READS`.

**The env contract was NOT verified in the first draft, and was wrong.** It named
`INSTAGRAM_BUSINESS_ACCOUNT_ID` as the server's own variable (it reads
`INSTAGRAM_ACCOUNT_ID`) and passed two `FACEBOOK_*` vars the package never reads.
Because the server constructs its client lazily and returns config errors as tool
*content*, it would have started clean, advertised all 23 tools, and failed every
single call — while naming a variable that was set correctly. Caught in review.

The lesson worth keeping: the tool names were source-checked and the env names
were taken from a README, and both were written up in the same confident voice.
Verify env vars against `dist/`, the same as tool names.

Re-verify both on any version bump — a renamed tool composes a grant that
silently never matches, and a renamed env var breaks everything at once.
