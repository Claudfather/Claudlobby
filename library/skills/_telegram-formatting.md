---
title: Telegram Formatting (Shared Partial)
description: Telegram output formatting rules for skills — plain text default, MarkdownV2 escape guidance.
---

# Telegram Formatting

**Default: plain text.** Do not pass `parseMode`. Do not wrap output in `**bold**`, `_italic_`, or backticks — they render literally, which is fine and preferable to escape failures.

Technical identifiers (`chart_uuid`, `~/path`) render correctly without escaping; no silent failures from missed escapes.

**MarkdownV2 only when hardened.** If a skill needs rich formatting (briefings with sections), it must escape all 17 special characters: `_ * [ ] ( ) ~ ` > # + - = | { } . !`. Missing one = silent failure. Use sparingly; default plain text.
