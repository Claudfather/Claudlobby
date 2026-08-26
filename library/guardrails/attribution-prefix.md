---
title: Attribution prefix — GitHub only
---

# Attribution prefix — GitHub only

When the fleet shares one GitHub identity — one PAT for all bots, OR one fleet-scope GitHub App (App-auth #1270), where every bot commits as the same `<slug>[bot]` — prefix `[<BotName>]` on PR/issue/review comments so reviewers can tell which bot wrote what.

**Never prefix** Telegram (handle already shows identity), Slack (bot user shown), or Notion (integration shown). Prefix is GitHub-only.

Goes away when the fleet graduates to **per-bot** GitHub Apps (#252) — not merely to the fleet-scope App, which still shares one identity across the fleet.
