---
title: Meta Business (Instagram)
type: mcp
env_contract:
  INSTAGRAM_ACCESS_TOKEN:
    description: Long-lived Meta Graph API token for an Instagram Professional account (~60-day expiry, must be refreshed). DM tools require instagram_manage_messages (Advanced Access, App Review).
    tier: fleet
  INSTAGRAM_BUSINESS_ACCOUNT_ID:
    description: Numeric id of the Instagram Professional account the bot acts for.
    tier: fleet
  FACEBOOK_APP_ID:
    description: Meta app id the token was issued under; required for token refresh.
    tier: fleet
  FACEBOOK_APP_SECRET:
    description: Meta app secret, paired with FACEBOOK_APP_ID for token refresh.
    tier: fleet
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

## Auth model

Four env vars, all fleet-tier (see the contract above). `INSTAGRAM_ACCESS_TOKEN`
is the credential; `FACEBOOK_APP_ID` + `FACEBOOK_APP_SECRET` exist so the server
can refresh it. Deliberately **not** reusing `META_ACCESS_TOKEN` from
`meta-ads.json`: that token carries `ads_read` for the Marketing API, this one
carries Instagram permissions, and collapsing them into one credential widens the
blast radius of a leak for no benefit.

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

- **Advanced Access gates every DM tool.** `instagram_manage_messages` is not
  granted on request — it requires Meta App Review, measured in days to weeks.
  Standard Access covers insights, media and comments immediately, so phases 1–2
  of a rollout work long before DMs light up. Nothing in claudlobby shortens this.
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

`@mcpware/instagram-mcp@1.0.4`: single maintainer, 4 published versions, ~51 KB
unpacked, ~541 downloads/month as of 2026-08-01 — comparable adoption to
`meta-ads-mcp-server` (~818/month), which this repo already ships. Young and
small, same as that one. Pin the exact version; mirror or fork if you need real
supply-chain assurance before handing it a business token.

**Source-verified.** The 23 tool names were checked against the published `1.0.4`
tarball, not just the project's README: `dist/index.js` registers exactly those 23
and no others. The read/write split was verified the same way — `dist/client.js`
has exactly 15 `GET` call-sites, matching `read_only_tools` one-for-one, while the
8 write tools issue `POST`/`DELETE`. The three reads whose names don't look like
reads (`validate_access_token` → `GET me`, `search_hashtag` →
`GET ig_hashtag_search`, `business_discovery` → `GET <account-id>`) are signed off
in `tests/test_readonly_mcp_grants.py::VERIFIED_NONSTANDARD_READS`.

Re-verify on any version bump — a tool renamed between releases composes a grant
that silently never matches.
