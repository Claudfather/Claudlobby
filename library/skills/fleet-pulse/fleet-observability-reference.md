# Fleet observability reference

Reference that the `fleet-observability` protocol points to: the event writers and their marker files, payload fields, detection mechanisms, history, reasons, and the critical-types table, word for word under the protocol's own section names. The protocol keeps every rule and every command a manager runs; this file adds none.

## Event Sources

| Writer | Script | Runs when | Source field |
|--------|--------|-----------|--------------|
| Bot vitals | `claudlobby/_runtime_scripts/bot-vitals.sh` | Every tool call (Claude Code hook) | `vitals` |
| Fleet pulse | `claudlobby fleet pulse` (private `claudlobby/_runtime_scripts/fleet-pulse.sh` sweep) | Cron (every 5 min) | `pulse` |
| Keepalive | `claudlobby/_runtime_scripts/keepalive.sh` | Every keepalive run (60s timer) | `keepalive` — `bot.heartbeat` / `bot.session_up` samples and `keepalive_*` and `bridge_heal` events on the plane, plus the `data/.idle` and `data/.last-no-turn` markers, and `data/.limit` while a usage limit holds the bot (#996) |

The idle marker is a special case: keepalive touches `data/.idle` when it classifies a pane as IDLE and removes it on BUSY. Fleet-pulse compares `.idle` mtime vs `.last-tool-call` mtime to determine idle state without parsing panes. keepalive also touches `data/.last-no-turn` on every tick that sees no turn running (IDLE, HELD or LIMIT) and never removes it, and `activity_stuck` times a stall from the later of `.last-no-turn` and `.last-tool-call`.

## Where to Read

- `claudlobby --json event list` — Its single schema-1 result has bounded `data.items`, `data.next_cursor`, and `data.coverage`; each item names an `event_id`, `occurred_at`, `bot`, `type`, `source`, and `data`.

## Decision Table

Each row in the protocol keeps what its event means and what the manager does; the rest of the row is here.

- `activity_stuck` — Uses marker-file mtime comparison, not pane regex.
- `usage_limit_held` — `reset` and `reset_epoch` name when it was due; `resume` says whether keepalive's one resume is `off`, `armed`, or already `tried`. While the menu is up, typed text goes to the menu, not the bot, so the framework's own sends refuse rather than type into it.
- `pane_stuck` — Note: a live spinner animates the pane, so an animated-but-hung bot shows up as `activity_stuck`, not `pane_stuck`.
- `crash_loop` — Before #1769 this read as "boot in flight" indefinitely and paged no one.
- `browser_outside_render` — `owner` names the bot whose session started it (a login CLI, an Artifact preview, `claudlobby plane open`, a render run without the render profile); `owner: null` means no bot did (the desktop session, or a bot session that has since ended). The payload also carries `pid`, `age_s`, `processes`, `pss_mib` and its cgroup `unit`.
- `unit_failed` — `data.surface` and `data.detail` say what was read: systemd's `ActiveState=failed` with its `Result`, or launchd's last exit status.
- `compaction_sent` — It carries the request, the host compaction slot's lease and each guard's reading.
- `compaction_proven` — The host's compaction slot is free again.
- `compaction_skipped` — `request: kept` while a guard says not now (a PreCompact hook that refuses, the slot held or load1 at 40, a screen that changed under the send lock); `request: dropped` when it ended unsent (2 hours old, an assignment delivered since it, a compaction since it, a close's context under the floor).

### A bot stopped through `bot stop` raises no alert (#2243)

The stop door removes the unit file and records the stop, who made it and an optional `--reason`, in the bot's `data/.stopped`. fleet-pulse reads that record and stays silent: no row, no push, no escalation.

Once it has been stopped `fleet_pulse.stopped_remind_days` days (default 3, `0` off), the manager gets one reminder push, then one every as many days: never a page. `bot start` removes the record. A record left beside an installed unit (the unit came back through another door, such as `spin-up-bot.sh` or a hand install, or a start or a stop failed partway) is from a stop that is over: the sweep removes it once it is 15 minutes old, so a later loss of that unit pages `unit_missing`, and one the sweep cannot remove is pushed to the manager as `stop_record_kept` until it is gone. The plane's `bot_stopped` and `bot_started` rows are the audit trail; the sweep never reads them, so a stop made while the plane is down stays quiet.

## Active Notifications (push)

Reading events at decision points is the default, but silent stalls — the reason `activity_stuck` exists — are exactly the case where a manager *can't* rely on remembering to poll. So `fleet-pulse.sh` also **pushes** a one-line note into your tmux session for the findings that need you (`crash_loop`, `session_missing`, `service_down`, `unit_missing`, `bridge_down`, `input_held`, `usage_limit_held`, `activity_stuck`, `pane_unknown`, `overdue_dispatch`, `browser_outside_render` once per browser, and `worker_unassigned` where `OBSERVABILITY_UNASSIGNED_CHECK=1` arms it), debounced to once per episode:

```
[FLEET-PULSE] <bot> activity_stuck — no tool calls for 11400s while not idle (likely hung mid-task)
```

**An alert about you, the fleet manager, also posts to the fleet's alert channel**, once an episode (#997). Its push lands in your own pane, and when your pane is the one that cannot act, nobody else in the fleet sees it. The post goes through the escalation's chat and sender. The reminder about a bot stopped on purpose posts nothing.

**Not yet captured via hooks:** several fleet-health signals are not derivable from the Claude Code PreToolUse/PostToolUse hook payload.

- **`rate_limit`** — a limit that **trips** is recorded (#996). Claude Code ends the turn with a `StopFailure` hook (`error: rate_limit`), `usage-limit-hook.sh` records `usage_limit_hit` with the limit line and the reset it printed, keepalive reads the held bot as `LIMIT` rather than `IDLE`, and fleet-pulse pages `usage_limit_held` if it is still held after the reset. Before #996 a tripped limit announced nothing: every bot then ran with `--remote-control`, so Claude Code armed neither its own resume nor its limit menu, and the pane sat at an empty prompt that read as idle. Only the fleet manager runs with it by default now (#2325); a worker shows the limit menu, which keepalive answers only on its wait label and no framework send types into. Measured on this host: `history_size` is **0** on every bot pane while `history-limit` reads `2000`, and `capture-pane -S -` returns exactly `pane_height` lines.
- **`context_warning`** — Measured across all 21 bots on this host: one rendered `98% context used` while twenty rendered nothing, panes structurally identical. A bot under no pressure and one that was at `100%` minutes ago present identically.
- **`mcp_error`** — a failing tool call (including an MCP server returning `isError`) fires the `PostToolUseFailure` hook event rather than `PostToolUse`, and only that event carries an error field. The `bot-vitals.sh` hook is wired to Pre/PostToolUse, so no `mcp_error` event is produced.

### Never sweep panes to establish absence

A quiet pane is one instant for one bot, not a history, and "checked all N panes, nothing there" reads as authoritative when it is not.

So a clean sweep can never distinguish *fine* from *already past it*, and it is the reading that gets acted on, because absence looks like an all-clear.

## Reading Events

The per-type loop: (That loop is over **type strings**, never bot paths — the command text names no bot directory, so it is unaffected by path-scoped deny rules regardless of arming.)

### Critical types

| Critical type | Raised by | Recorded against |
|---------------|-----------|------------------|
| `session_missing`, `service_down`, `unit_missing`, `bridge_down`, `crash_loop`, `activity_stuck`, `input_held`, `usage_limit_held`, `pane_unknown`, `overdue_dispatch` | fleet-pulse, per bot | the bot |
| `rc_timeout` | `start-bot.sh`, once per (re)start | the bot |
| `script_error` | a runtime script's ERR trap | its bot; the fleet or the host for a script with none |
| `bridge_down`, `channel_refused`, `reload_failed`, `restart_failed`, `keepalive_failed`, `alert_target_refused`, `alert_pair_unreachable`, `fleet_alert` | a FLEET ALERT raised with a fleet in scope: a fleet job, a bot's bring-up or keepalive, `fleet notify --level alert` (`fleet_alert`) | the fleet, read as bot `fleet` |
| `unit_failed` | fleet-pulse, once an episode, for an owned unit in the failed state (#2532) | the fleet for one of its own units (bot `fleet`); the host for a host unit |
| `compaction_not_proven` | keepalive, once a request: no compaction proven within 10 minutes of the `/compact` it typed (#1053) | the bot |
| `rolling_restart_stalled` | a FLEET ALERT from `rolling-restart.sh` when a roll halts | the fleet of the shell that ran it, else the host: not the fleet it was rolling (#2112) |
| `disk_high`, `memory_high`, `memory_thrash`, `memory_thrash_unarmed`, `undervoltage`, `storage_stall`, `host_health`, `binary_update_failed`, `binary_unrunnable`, `vault_sync_failed` | a FLEET ALERT from a host job, which runs with no fleet | the host, as is any FLEET ALERT raised with no fleet in scope; no fleet's `event list` reads it |
| `public_write_guard_unarmed` | the public-write guard (opt-in), on each GitHub write it checks while the host has no term list | the bot |
| `shadow_parity_diverged` | nothing now (the plane cutover's shadow) | kept so its old rows classify |
