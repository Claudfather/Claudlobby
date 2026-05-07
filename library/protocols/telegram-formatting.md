---
title: Telegram Formatting
---

**Default: plain text.** Do not pass `parseMode`. Do not wrap output in `**bold**`, `_italic_`, or backticks — they render literally, which is fine and preferable to escape failures.

The bash helper `$CLAUDLOBBY_ROOT/lib/tg-post.sh` sends plain text by default. The MCP tool `mcp__plugin_telegram_telegram__reply` — call it WITHOUT `parseMode`.

Why plain text wins: technical identifiers (`chart_uuid`, `~/path`) render correctly without escaping; no silent failures from missed escapes. See `lessons/telegram/plain-text-escape-incident` for the incident that codified this.

**MarkdownV2 only when hardened.** If a skill needs rich formatting (briefings with sections), it must escape all 17 special characters: `_ * [ ] ( ) ~ ` > # + - = | { } . !`. Missing one = silent failure. Use sparingly; default plain text.
