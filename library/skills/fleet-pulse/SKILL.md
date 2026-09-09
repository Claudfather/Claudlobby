---
name: fleet-pulse
description: "Run fleet-pulse.sh and act on findings — restart dead workers, flag stuck panes, protect WIP. Actionable fleet health in one command."
argument-hint: "[<bot-name>]"
---

# Fleet Pulse

Run external liveness checks against the fleet, summarize findings, and take corrective action per the fleet-observability decision table. Optionally scope to a single bot.

## How it works

`fleet-pulse.sh` runs outside the LLM — it checks tmux sessions, systemd services, pane freshness, and git WIP for every bot in the fleet. Results are recorded on the plane as fleet events (nothing lives in a file any more). This skill reads them through `claudlobby events`, presents a summary, and acts on them.

## Steps

1. **Generate fresh pulse data**

   ```bash
   $CLAUDLOBBY_ROOT/lib/fleet-pulse.sh $FLEET_NAME
   ```

2. **Read today's events**

   The events live on the plane, not in a file (F18 closure). Read them through the CLI:
   - `claudlobby --fleet $FLEET_NAME events --since 24h --json` (add `--bot <bot-name>` when an argument was given)
   - Parse each line as JSON: `{"ts": "...", "bot": "...", "type": "...", "source": "pulse", "data": {...}}` — the same row shape the ledgers had

3. **Summarize findings**

   Post a plain-text summary to Telegram (no parseMode). Format:

   ```
   Fleet pulse — <fleet-name> — <timestamp>

   <bot-name>: <event-type> — <one-line detail>
   <bot-name>: <event-type> — <one-line detail>
   ...

   Action taken: <list of actions>
   All clear: <list of healthy bots>
   ```

   If no events were emitted, report "All bots healthy" and stop.

4. **Take action per the decision table**

   Execute the matching action for each event type, then report what was done.

## Decision Table

| Event type | Action |
|------------|--------|
| `session_missing` | Re-enroll: `$CLAUDLOBBY_ROOT/lib/spin-up-bot.sh $BOT_DIR` |
| `service_down` | Re-enroll: `$CLAUDLOBBY_ROOT/lib/spin-up-bot.sh $BOT_DIR` |
| `pane_stuck` (>5 min) | Capture pane content (`tmux capture-pane -t <session> -p`), inspect for genuine stuck state. If confirmed stuck, restart the bot. If output shows active work, skip. |
| `wip_uncommitted` | Do NOT restart. Flag as task-in-flight. Check how long the WIP has been uncommitted — if >2 hours, flag to human as potentially stale. |

## Report Format

All findings and actions go to Telegram as plain text. One message per pulse run. Structure:

- Header line with fleet name and timestamp
- One line per event: `<bot>: <type> — <detail>`
- Actions taken section
- Healthy bots listed at the end

If scoped to a single bot, only report that bot's status.

## Rules

- Manager-only skill. Workers never read event logs or run pulse checks.
- Always run the bash script first to get fresh data. Never rely on stale event files alone.
- Never restart a bot with uncommitted WIP. The `wip_uncommitted` event is a protection signal.
- For `pane_stuck`, always inspect pane content before restarting — a long-running test or build is not stuck.
- Post findings to Telegram so the human has visibility, even when taking autonomous action.
- If the bash script fails (non-zero exit), report the error and stop. Do not act on stale data.
