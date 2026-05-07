---
title: Telegram bot group permissions setup
description: Fix BotFather privacy mode so bots can see all group messages, not just @mentions
---

Telegram bots default to server-side privacy mode that filters group messages before they reach the bot. Only @mentions and direct replies are delivered. To enable full group participation:

1. **Disable privacy mode** via @BotFather: `/setprivacy` → `Disable`
2. **Kick and re-add the bot** to the group — Telegram requires this for the privacy change to take effect (server-side quirk)
3. **Update access.json** at `~/.claude/channels/<channel-dir>/access.json` — change the group entry from `true` to `{ "requireMention": false, "allowFrom": [] }` if the bot should see all messages, or keep `requireMention: true` for worker bots that should only respond when tagged

After this, the bot sees ALL group messages. Set `requireMention: false` for manager bots (who need to see everything) and `requireMention: true` for workers (who respond only when dispatched or tagged).
