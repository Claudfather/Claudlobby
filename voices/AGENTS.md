# voices/

Personality overlays. A voice defines **how** a bot communicates — tone, humor, mannerisms, character traits. It never defines **what** a bot does.

## The bright line

- **Voice** = personality, communication style, character. "Who you sound like."
- **Expertise** (in `library/expertise/`) = domain knowledge, responsibilities, workflows. "What you do."

These are independent axes. Any voice can pair with any expertise. A `conspiracy-theorist` voice works on an SEO bot, an advertising bot, or an engineer. An `agreeable-workhorse` voice works on a site-copy bot or a data pipeline bot. If your voice file references a specific domain or skill, it's leaking expertise into personality — fix it.

## What belongs here

One `.md` file per persona. Each should define:

- **Tone and mannerisms** — how the bot talks, what makes it distinctive
- **Character traits** — personality constants that don't change by context
- **Boundaries** — what the persona never does (e.g., "never offensive or cruel")

Use `{{BOT_NAME}}` as the character name so the voice works for any bot.

## What does NOT belong here

- Domain knowledge (SEO tactics, email frameworks, code review checklists)
- Workflow instructions (how to audit a site, how to write a PR)
- Tool usage patterns (which MCP servers to call, which CLI commands to run)
- References to specific brands, products, or companies

All of the above belong in `library/expertise/`, `library/skills/`, or fleet-specific `local/` overlays.

## Naming

Lowercase, hyphenated, descriptive of the persona archetype: `conspiracy-theorist.md`, `agreeable-workhorse.md`, `stoner-creative.md`. Not the bot's name — the personality type.

## Composition

Bots reference a voice in fleet.yaml: `voice: voices/conspiracy-theorist.md`. The compositor injects it into the bot's CLAUDE.md under a "Voice" section. A bot gets exactly one voice (or none).
