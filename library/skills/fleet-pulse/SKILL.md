---
name: fleet-pulse
description: "Run the selected fleet pulse and act on findings — restart dead workers, flag stuck panes, protect WIP."
argument-hint: "[<bot-name>]"
---

# Fleet Pulse

Run external liveness checks against the fleet, summarize findings, and take corrective action per the fleet-observability decision table. Optionally scope to a single bot.

## How it works

`claudlobby fleet pulse` admits the selected fleet and runs its private sweep. The sweep checks tmux sessions, supervised services, pane freshness, and git WIP. Its completed result means the tick finished, not that all bots are healthy. Findings are recorded on the plane as fleet events. Read those events and the returned summary before acting.

## Steps

1. **Generate fresh pulse data**

   ```bash
   claudlobby --json fleet pulse
   ```

2. **Read today's events**

   The events live on the plane, not in a file (F18 closure). Read them through the CLI:
   - `claudlobby --json event list --since 24h --source pulse`; when an argument was given, add `--bot BOT_ID`
   - Read the single schema-1 result's `data.items`, `data.coverage`, and `data.next_cursor`. Each item names its stable `event_id`, `occurred_at`, `bot`, `type`, `source`, `severity`, and `data`; continue a nonempty cursor before claiming the window was fully inspected.

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

   If no events were emitted, report the observed summary and say that no new pulse events were found. Do not infer that every bot is healthy from an empty event list.

4. **Take action per the decision table**

   Execute the matching action for each event type, then report what was done.

## Decision Table

| Event type | Action |
|------------|--------|
| `session_missing` | After checking this bot is meant to run, `claudlobby --json bot start BOT_ID` with its literal declared ID. |
| `service_down` | After checking this bot is meant to run, `claudlobby --json bot start BOT_ID` with its literal declared ID. |
| `pane_stuck` (>5 min) | Read `claudlobby --json bot session BOT_ID` and `claudlobby --json bot logs BOT_ID --lines 50` with its literal declared ID, and inspect for genuine stuck state. If confirmed stuck, restart the bot. If output shows active work, or the reads refuse or are unknown, skip and report it. Do not use `tmux` directly: bots run on the fleet's private socket. |
| `wip_uncommitted` | **Read `paths`, not `dirty_files`.** The count cannot tell a mid-edit from a virtualenv — `M lib/foo.py` and `?? .venv/` are both `1`. Any path that is source, config or content: do NOT restart, task in flight. Only artifact paths you recognise (`.venv/`, `node_modules/`, a build dir): not work in flight — say which paths you saw and why you judged them artifacts. `unchanged_for_s` past ~2h on a *source* path is stale WIP: flag to the human. Never read it as a licence to restart, because a brand-new source file is untracked too. |

`bot start` is manager-to-other only. Report supervised session recovery only
when `ok` is true, `data.state` is `running`, `data.native_outcome` is
`observed`, and `data.readiness` is `current_session_ready`, `bridge_ready`, or
`session_ready`. `current_session_ready` confirms an existing session, not
bridge delivery; `session_ready` is for a non-channel or intentionally
tokenless bot. If the command refuses or readiness is unknown, report the
exact failure and stop; do not invoke a raw launcher. A deliberately stopped
bot is not revived from an alert alone.

## Report Format

All findings and actions go to Telegram as plain text. One message per pulse run. Structure:

- Header line with fleet name and timestamp
- One line per event: `<bot>: <type> — <detail>`
- Actions taken section
- Healthy bots listed at the end

If scoped to a single bot, only report that bot's status.

## Rules

- Manager-only skill. Workers never read event logs or run pulse checks.
- Run the public pulse command first and require `ok: true` with `data.tick: completed`. Never treat that result alone as a healthy-fleet verdict.
- **This rule has to stay followable, which is why it names a test you can apply.** It was once "never restart a bot with uncommitted WIP" against an event that fired thousands of times a week, so it forbade restarting anyone — and managers stopped obeying it without ever deciding to (#1728). An instruction nobody can follow is an instruction nobody follows.
- Never restart a bot whose `wip_uncommitted` paths include anything you cannot name as a build artifact. The event is a protection signal — and judge it on its `paths`, never its count. **`dirty_untracked > 0` does not mean "just build artifacts":** an unadded new source file is untracked and is the case where losing the work is unrecoverable, since no copy of it exists anywhere.
- `unchanged_for_s` is a FLOOR: it counts from when the sweep first saw that exact status, not from when the edit landed. A small number is not evidence the WIP is fresh.
- For `pane_stuck`, always inspect the bot's session and log tail before restarting — a long-running test or build is not stuck.
- Post findings to Telegram so the human has visibility, even when taking autonomous action.
- If the pulse command fails, report the error and stop. Do not act on stale data.
