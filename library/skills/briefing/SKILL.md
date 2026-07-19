---
name: briefing
description: "Use when a scheduled briefing timer fires `/briefing <slot>`, or the user asks for an ad-hoc status summary. Reads the bot's `fleet.yaml` briefing config (BRIEFING_* env) and consolidates its equipped data sources into a mobile-first Telegram update, leading with what needs attention."
argument-hint: "[slot — morning | midday | evening | <configured slot>]"
---

# Briefing

A consolidated status briefing delivered to Telegram, personalized per bot by the `bots.<bot>.briefing` stanza in `fleet.yaml`. A bot equips briefings in config alone; this skill reads that config at runtime and renders it. **Never fork this skill per fleet** — personalization lives in config, not in copies of the skill.

## Trigger

Triggered by the composed briefing timers (the `briefing:` stanza in `fleet.yaml`) via `lib/briefing-trigger.sh`, which delivers `/briefing <slot>` into the bot's own session — see `documentation/fleet-yaml-schema.md`. Never hand-install cron for this.

Run `/briefing <slot>` by hand any time for an ad-hoc summary.

## Configuration — read this first

`bot.conf` (sourced at session start) carries this bot's personalization. Read these before assembling anything:

| Variable | Meaning | Example |
|----------|---------|---------|
| `BRIEFING_SLOTS` | slots this bot is equipped for (space-separated) | `morning evening` |
| `BRIEFING_SOURCES` | data sources to pull, by integration name | `github gmail google-calendar` |
| `BRIEFING_SECTIONS_<SLOT>` | ordered sections for a slot — **`<SLOT>` is upper-cased** | `BRIEFING_SECTIONS_MORNING="overnight calendar overdue"` |

**Config-driven, with defaults.** To read the section list, upper-case the dispatched slot (`/briefing morning` → `$BRIEFING_SECTIONS_MORNING`). If that variable is set, render exactly those sections, in order; if unset, fall back to the slot's canonical default below — a bot equipped with zero personalization still gets a sensible briefing.

## Per-slot focus

Canonical defaults, used when `BRIEFING_SECTIONS_<SLOT>` is unset:

| Slot | Focus | Default sections |
|------|-------|------------------|
| `morning` | what happened overnight + what needs attention today | `overnight calendar overdue` |
| `midday` | progress so far + what's left this afternoon | `progress afternoon` |
| `evening` | the day's wrap + tomorrow's setup | `wrap tomorrow` |

Custom slot names are supported: their focus is entirely their configured `BRIEFING_SECTIONS_<SLOT>` list (fall back to the `morning` shape if a custom slot declares no sections).

Section vocabulary — what each renders (drawing from whichever equipped source holds the data):

- **overnight** — activity since the last briefing: new PRs/issues, unread mail, alerts
- **calendar** — today's events, next meeting, conflicts
- **overdue** / **due** — tasks past due or due today
- **progress** — what has moved since the morning briefing
- **afternoon** — what is still open for the rest of the day
- **wrap** — what got done, notable events
- **tomorrow** — tomorrow's first events and anything needing prep tonight

## Data sources

Pull **only** the sources named in `BRIEFING_SOURCES`, and **only** those whose integration is actually equipped on this bot. Each source is an equipped integration / MCP server — e.g. `github`, `gmail`, `google-calendar`, `notion`, `slack` — named as it appears in the bot's `integrations`/`mcp` config. What each provides is self-evident from its name; the section vocabulary above defines how that data is grouped for display.

**Skip-if-absent.** If a listed source's integration isn't reachable on this bot (its MCP server isn't equipped), skip it silently in the body and name it in **one footer line**: `(skipped: <sources> — not equipped)`. Never fabricate a section for a source you couldn't reach.

## Mobile format

Delivered to a phone via Telegram — optimize for a glance:

- **Lead with what needs attention.** The first line is the single most important thing (an overdue item, a failing check, a calendar conflict). If nothing needs attention, say so in one line.
- **Tight sections.** One short header per section, 1–5 bullets, no paragraph over ~40 words. Drop an empty section entirely rather than printing "nothing."
- **Plain text.** Do **not** pass `parseMode` — send plain per the `telegram-formatting` protocol.
- **Scannable order:** attention-needing first, then time-bound (calendar), then FYI. End with the skip footer if any source was absent.

Deliver through the bot's normal Telegram reply path.

## Instructions

1. Resolve the slot from `$ARGUMENTS`; default to `morning` when none is given.
2. Read `BRIEFING_SOURCES` and the slot's `BRIEFING_SECTIONS_<SLOT>` (upper-cased slot) from the environment — the slot's canonical defaults apply when the section variable is unset (see Configuration).
3. For each configured section, pull from the mapped source — skipping any source not equipped on this bot.
4. Assemble in mobile format: lead with what needs attention, tight sections, plain text.
5. Deliver to Telegram, appending the skip footer if any listed source was absent.

$ARGUMENTS
