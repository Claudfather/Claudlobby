# library/permissions/

Despite the name, this is **not** where Claude Code tool permissions come from. The one file here, `access.json.template`, documents the shape of a bot's **Telegram** channel access policy — who can DM it, which groups it's active in, and whether group messages require an @mention.

## What belongs here

`access.json.template` shows the fields the Telegram plugin reads from `~/.claude/channels/telegram-<handle>/access.json` at boot: `dmPolicy`, `allowFrom`, `groups.<chat_id>.requireMention`, `pending`.

**It's reference-only — nothing reads this file.** `composer.py::compose_access_json` builds each bot's live `access.json` from scratch out of `bot.telegram`, `fleet.telegram_group_chat_id`, and `fleet.human_telegram_id`; it never opens `access.json.template` from disk (confirmed: no reference to the filename anywhere in `claudlobby/`). The template just happens to match the shape the compositor generates. Treat it as documentation, not an input. For hand-editing a *live* access.json (e.g. flipping `requireMention` after a BotFather privacy-mode change), see `library/lessons/telegram-bot-group-setup.md`.

**Real Claude Code tool permissions** — which tools a bot can use automatically vs. which need human approval — are composed from two entirely different places:
- `permissions:` YAML frontmatter (`allow_all`, `bash_allow`, `allow`, `deny`) in `library/expertise/*.md`
- `_permissions_contract.tools` in `library/mcp/*.json`

Both are merged by `composer.py::compose_settings_local` into the bot's `.claude/settings.local.json`. Nothing in this directory feeds that pipeline.

One more wrinkle: `permissions` is also wired as a generic composable library section, the same mechanism as `guardrails/`, `principles/`, `protocols/`, etc. — a bot's `permissions:` list in fleet.yaml would load `.md` files from here (title/description frontmatter + matching H1, per the repo-wide library convention) into a `## Permissions` section of its composed CLAUDE.md. In practice this has never been used: no fleet example references it, and `access.json.template`'s `.json` extension makes it invisible to that loader anyway (it only ever looks for `.md` files). So today this category is, functionally, empty.

## Composition

Nothing here is composed automatically. If a `.md` file existed in this directory and a bot's fleet.yaml listed it under `permissions:`, it would render as prose in that bot's CLAUDE.md — but no such file exists yet. `access.json.template` is not consumed by any code path; copy it into `local/<fleet>/` by hand only if you want a shape reference while writing Telegram access policy.

## Naming

Use descriptive names: `access.json.template` for the Telegram access-policy template.
