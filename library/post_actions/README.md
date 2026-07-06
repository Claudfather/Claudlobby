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

No `post_actions/*.md` files exist in the library yet. The mechanism is fully wired —
`config.py` (`post_actions:` field), `composer.py` (composes them into a `## Post-actions`
section), `validator.py`, `commands/core.py` (`claudlobby list-library`), and
`fleet.yaml.example` all support it — but nobody has committed real content, so there's
no file to point to as a working sample yet. The block below is illustrative only: it
shows the shape a file would need, not something present in the repo today.

`library/post_actions/post-restart-announce.md` (hypothetical):

```markdown
## Post-restart announce

When your session (re)starts — after idling, a restart, or keepalive recovery — post a
one-line Telegram update summarizing what state you're picking up: what you were last
working on, and whether anything needs the human's attention.

Keep it to one line. This is a presence signal, not a status report — if there's real
news, send that as a separate message.
```

Note that `lib/pre-stop-handoff.sh` — the script a `pre-stop-handoff.md` post_action would
naturally wrap — already exists and runs today, invoked directly via the bot's systemd
`ExecStop` (see the script's own header comment). A `post_actions/pre-stop-handoff.md`
file would add bot-facing prose on top of that existing mechanism; it just hasn't been
written yet.

## Naming

Use action-imperative naming: `pre-stop-handoff`, `daily-wrap-up`, `post-restart-announce`.
