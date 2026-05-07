---
title: "Lesson: Telegram — plain text only (2026-04-18 underscore-escape incident)"
---

On 2026-04-18 the fleet hit "Markdown escape hell" trying to render technical identifiers like `chart_uuid` in Telegram messages.

**What broke:**

- `parseMode: "Markdown"` (legacy, not MarkdownV2) treats `_` as italic delimiter.
- Escaping with `\_` does NOT work in legacy Markdown — the backslash renders literally.
- Result: `chart_uuid` displayed as `chart\_uuid` to the user.
- This affected nearly every technical reply that mentioned an identifier with underscores.

**The fix:**

Send plain text. Do NOT pass `parseMode`. Do NOT wrap things in `**bold**`, `_italic_`, or `` `backticks` `` — they render as literal characters, which is fine and preferable to the underscore-escape failure mode.

The bash helper `tg-post.sh` sends plain text by default (no `parse_mode`). The MCP plugin tool `mcp__plugin_telegram_telegram__reply` — call it WITHOUT `parseMode`.

**Why not MarkdownV2?**

MarkdownV2 escaping IS reliable (`\_` works there). But MarkdownV2 requires escaping ~17 special characters in every message; missing one causes the message to fail silently. For mixed prose + technical identifiers, the reliability of plain text wins. Content carries emphasis; formatting is noise.

**Implication for new bots:**

Default Telegram output to plain text. Use rich formatting only for skills that have been hardened with full MarkdownV2 escape coverage and tested.
