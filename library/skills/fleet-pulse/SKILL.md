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
| `wip_uncommitted` | **Read `paths`, not `dirty_files`.** The count cannot tell a mid-edit from a virtualenv — `M lib/foo.py` and `?? .venv/` are both `1`. Any path that is source, config or content: do NOT restart, task in flight. Only artifact paths you recognise (`.venv/`, `node_modules/`, a build dir): not work in flight — say which paths you saw and why you judged them artifacts. `unchanged_for_s` past ~2h on a *source* path is stale WIP: flag to the human. Never read it as a licence to restart, because a brand-new source file is untracked too. |

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
- **This rule has to stay followable, which is why it names a test you can apply.** It was once "never restart a bot with uncommitted WIP" against an event that fired thousands of times a week, so it forbade restarting anyone — and managers stopped obeying it without ever deciding to (#1728). An instruction nobody can follow is an instruction nobody follows.
- Never restart a bot whose `wip_uncommitted` paths include anything you cannot name as a build artifact. The event is a protection signal — and judge it on its `paths`, never its count. **`dirty_untracked > 0` does not mean "just build artifacts":** an unadded new source file is untracked and is the case where losing the work is unrecoverable, since no copy of it exists anywhere.
- `unchanged_for_s` is a FLOOR: it counts from when the sweep first saw that exact status, not from when the edit landed. A small number is not evidence the WIP is fresh.
- For `pane_stuck`, always inspect pane content before restarting — a long-running test or build is not stuck.
- Post findings to Telegram so the human has visibility, even when taking autonomous action.
- If the bash script fails (non-zero exit), report the error and stop. Do not act on stale data.
