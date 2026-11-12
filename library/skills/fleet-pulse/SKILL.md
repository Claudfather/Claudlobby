---
name: fleet-pulse
description: "Use when you manage a fleet and a worker looks dead, stuck or silent, and you want it recovered, not only reported. Runs the selected fleet pulse and acts on findings — restart dead workers, flag stuck panes, protect WIP. For the event schema or an `event list` read recipe, read the Event reference in `.claude/skills/fleet-pulse/SKILL.md`: invoking `/fleet-pulse` starts a pulse run."
argument-hint: "[<bot-name>]"
---

# Fleet Pulse

Run external liveness checks against the fleet, summarize findings, and take corrective action per the fleet-observability decision table. Optionally scope to a single bot.

## How it works

`claudlobby fleet pulse` admits the selected fleet and runs its private sweep. The sweep checks tmux sessions, supervised services, pane freshness, and git WIP, reports each of the fleet's own units it finds in the failed state once an episode, and records each bot's new Claude Code compactions as `compaction` events (read them with `claudlobby --json event list --type compaction`). Its completed result means the tick finished, not that all bots are healthy. Findings are recorded on the plane as fleet events. Read those events and the returned summary before acting.

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
| `service_down` | After checking this bot is meant to run, `claudlobby --json bot start BOT_ID` with its literal declared ID. Its `session` field says whether the tmux session is up. |
| `unit_missing` | Its unit file is gone and no stop was recorded. If it is meant to run, `claudlobby --json bot start BOT_ID`; if it was stopped on purpose, `claudlobby --json bot stop BOT_ID --reason TEXT` records that and the alert ends. |
| `pane_stuck` (>5 min) | Read `claudlobby --json bot session BOT_ID` and `claudlobby --json bot logs BOT_ID --lines 50` with its literal declared ID, and inspect for genuine stuck state. If confirmed stuck, restart the bot. If output shows active work, or the reads refuse or are unknown, skip and report it. Do not use `tmux` directly: bots run on the fleet's private socket. |
| `compaction` | Informational, no action. A `bot compact` you sent appears here once the pulse records it (`trigger: manual`, `pre_tokens` to `post_tokens`); `claudlobby --json fleet compactions record` records it at once. An `auto` one means the bot reached Claude Code's own limit. |
| `wip_uncommitted` | **Read `paths`, not `dirty_files`.** The count cannot tell a mid-edit from a virtualenv — `M lib/foo.py` and `?? .venv/` are both `1`. Any path that is source, config or content: do NOT restart, task in flight. Only artifact paths you recognise (`.venv/`, `node_modules/`, a build dir): not work in flight — say which paths you saw and why you judged them artifacts. `unchanged_for_s` past ~2h on a *source* path is stale WIP: flag to the human. Never read it as a licence to restart, because a brand-new source file is untracked too. |
| `browser_outside_render` | A browser has run more than 15 minutes outside a render scope. `owner` is the bot whose session started it, or null when no bot did. Nothing stops it: ask that bot what opened it, or tell the operator, and have it closed where it was opened; never by name. |
| `unit_failed` | One of the fleet's units is in the failed state: a job, or the alert a failed job sends. `data.detail` says what was read. Read its log (`journalctl --user -u <unit>`) and tell the operator; fixing the unit is theirs. |

`bot start` is manager-to-other only. Report supervised session recovery only
when `ok` is true, `data.state` is `running`, `data.native_outcome` is
`observed`, and `data.readiness` is `current_session_ready`, `bridge_ready`, or
`session_ready`. `current_session_ready` confirms an existing session, not
bridge delivery; `session_ready` is for a non-channel or intentionally
tokenless bot. If the command refuses or readiness is unknown, report the
exact failure and stop; do not invoke a raw launcher. A deliberately stopped
bot is not revived from an alert alone. A bot stopped through `bot stop` raises
no alert at all (#2243): the summary shows it as `stopped`, with who and since,
and the manager gets a reminder once it has been stopped for days.

## Report Format

All findings and actions go to Telegram as plain text. One message per pulse run. Structure:

- Header line with fleet name and timestamp
- One line per event: `<bot>: <type> — <detail>`
- Actions taken section
- Healthy bots listed at the end

If scoped to a single bot, only report that bot's status.

## Rules

- Manager-only skill. Workers never read event logs or run pulse checks, but a bot that composes the `fleet-observability` protocol, such as a monitor, may read the Event reference below, whatever its Configuration lines say about managers.
- Run the public pulse command first and require `ok: true` with `data.tick: completed`. Never treat that result alone as a healthy-fleet verdict.
- **This rule has to stay followable, which is why it names a test you can apply.** It was once "never restart a bot with uncommitted WIP" against an event that fired thousands of times a week, so it forbade restarting anyone — and managers stopped obeying it without ever deciding to (#1728). An instruction nobody can follow is an instruction nobody follows.
- Never restart a bot whose `wip_uncommitted` paths include anything you cannot name as a build artifact. The event is a protection signal — and judge it on its `paths`, never its count. **`dirty_untracked > 0` does not mean "just build artifacts":** an unadded new source file is untracked and is the case where losing the work is unrecoverable, since no copy of it exists anywhere.
- `unchanged_for_s` is a FLOOR: it counts from when the sweep first saw that exact status, not from when the edit landed. A small number is not evidence the WIP is fresh.
- For `pane_stuck`, always inspect the bot's session and log tail before restarting — a long-running test or build is not stuck.
- Post findings to Telegram so the human has visibility, even when taking autonomous action.
- If the pulse command fails, report the error and stop. Do not act on stale data.

## Event reference

Reference that the `fleet-observability` protocol points to, not a step of the pulse run above. The protocol keeps the guards.

### Event Schema

```json
{"ts": "...", "bot": "...", "type": "...", "source": "vitals|pulse|audit", "data": {...}}
```

- **ts** — ISO 8601 timestamp
- **bot** — bot identifier
- **type** — event classification (open-ended, match on what you care about)
- **source** — `vitals` (bot-emitted), `pulse` (external check), or `audit` (rolling code-audit sweep)
- **data** — type-specific payload (open object)

### Reading Events

Use `claudlobby event list` — never a hand-rolled loop over bot directories. This is **not** a fix
for a live break: run verbatim on an armed bot, the loop below still works, including for denied
siblings — confirmed directly, independently, by two different bots on two different sessions.
The mechanism (otis, `shared/planning/active/2026-08-27-deny-bypass-probe.md`): the permission
matcher resolves a variable **only if its value is assigned in the same command as a literal** —
`b=ravi; cat .../$b/...` is denied correctly (and `b=otis; cat .../$b/...` allowed correctly, both
resolved against the right bot). An expansion the matcher **cannot** statically resolve from the
command text — a glob-bound loop variable, `${RANDOM}`, `${PIPESTATUS[0]}` — makes it fail open,
regardless of where that expansion sits or whether the path beside it is literal. The loop below
binds `"$bot_dir"` from `"$BOTS_DIR"/*/`, a glob expansion depending on filesystem state at
runtime — unresolvable from the command text alone — which is why it evades where a same-command
literal assignment would not.

That is exactly the problem. **It works by accident**, on some permission-matcher blind spot, not
by design — and the same evasion is why it's silent in the reassuring direction if the matcher
is ever tightened: an event sweep that stops resolving would return no events, indistinguishable
from a healthy fleet, with no warning that anything changed. Separately from permissions entirely,
`claudlobby event list` is also just the better tool for this: it's the same door `brief.py`'s alerts
section already consumes, and it adds type/critical filtering and coverage-honesty disclosure a
hand-rolled loop doesn't have.

Tail today's events across the fleet:

```bash
claudlobby --json event list --limit 50
```

Scope to one bot — e.g. before dispatch, or cross-referencing a linked report:

```bash
claudlobby --json event list --bot "$BOT_NAME" --limit 20
```

Filter for actionable events:

```bash
claudlobby --json event list --critical --limit 200
```

### Retention

Event files older than 7 days are automatically reaped by both `bot-vitals.sh` (on each hook invocation) and `fleet-pulse.sh` (on each cron run). No archiving — build archive-to-claudron if trend analysis proves valuable later.

### Configuration

Opt-in per bot via fleet.yaml. Hooks go on all bots; this protocol goes on managers only.

```yaml
bots:
  manager-bot:
    protocols:
      - fleet-observability    # manager reads event logs
  worker-bot:
    hooks:
      PreToolUse:
        - command: "$CLAUDLOBBY_NATIVE_DIR/bot-vitals.sh"
      PostToolUse:
        - command: "$CLAUDLOBBY_NATIVE_DIR/bot-vitals.sh"
```

Workers emit events but never read them. Managers read events but hooks are optional on them (useful if the manager also does tool work).
