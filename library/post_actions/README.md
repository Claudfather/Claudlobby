# library/post_actions/

Composable lifecycle hooks — actions to take when the bot's session ends, restarts, or transitions between states. Declared per-bot via `post_actions:` in fleet.yaml.

## What belongs here

- **Pre-stop handoff** — "before this session ends, write a handoff note for the next session" (see `lib/pre-stop-handoff.sh`)
- **Post-restart announce** — "when you come back from a restart, briefly summarize what state you're in"
- **Session retrospective** — "every N hours, log a one-line activity summary to state/fleet-state.json"
- **Daily wrap-up** — "at end-of-day, post a Telegram summary of the day's work"

## What does NOT belong here

- **Imperative scripts** — those go in `lib/` (e.g., `lib/pre-stop-handoff.sh`); a post_action references them in prose
- **Rules** — guardrails
- **Capability** — expertise

## Composition

Each `<action>.md` is appended under a `## Post-actions` section in the bot's CLAUDE.md, in the order listed in `fleet.yaml` `post_actions:`.

These instructions tell the bot **what to do at each lifecycle moment** — the scheduling and triggering itself happens via the bot's service unit, cron, or in-session timer (handled by `lib/`).

## Example

`library/post_actions/pre-stop-handoff.md`:

```markdown
## Pre-stop handoff

Before your session ends (signal: SIGTERM, or `/restart`), do these in order:

1. Run `$CLAUDLOBBY_ROOT/lib/pre-stop-handoff.sh` — writes a structured
   `handoff.json` capturing your current task, blockers, and recent work.
2. Post a one-line Telegram update: "Restarting — handoff saved."
3. Exit cleanly within 30s. The supervisor will SIGKILL after that.

The next session starts by reading `handoff.json` (your `STARTUP_PROMPT`
includes this), so anything you don't capture here is lost.
```

## Naming

Use action-imperative naming: `pre-stop-handoff`, `daily-wrap-up`, `post-restart-announce`.
