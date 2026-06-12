---
title: Fleet Observability — Where to Look
description: Decision tree for diagnosing fleet issues from logs, events, and CLI tools
---

# Fleet Observability — Where to Look

## Quick Reference

| Question | Where to look | Command |
|----------|---------------|---------|
| Is the fleet healthy? | Fleet status dashboard | `claudlobby status` |
| What happened recently? | JSONL event stream | `claudlobby events --critical --tail 20` |
| Is a specific bot stuck? | Bot keepalive log | `cat runtime/bots/<bot>/keepalive.log \| tail -20` |
| Why did a bot restart? | Keepalive events + journal | `claudlobby events --bot <bot> --type keepalive` |
| Did a script fail? | Script error events | `claudlobby events --type script_error` |
| Is a service down? | systemd journal | `journalctl --user -u <BOT_SERVICE> -n 30` |
| What's the bot doing right now? | tmux pane | `tmux capture-pane -t <bot> -p \| tail -10` |
| How long has the fleet been up? | Uptime metrics | `claudlobby uptime` |
| What work completed? | Report-back ledger | `claudlobby report-back --since 24h` |
| Fleet-wide log search | Tail all logs | `lib/tail-fleet.sh --fleet <name> --grep ERROR` |
| Last pulse snapshot | Pulse summary file | `cat state/pulse/pulse-summary.txt` |

## Event Data Flow

```
Bot activity
  └─► bot-vitals.sh (hook) ──► data/events/fleet-YYYY-MM-DD.jsonl (source: vitals)
  └─► keepalive.sh (timer)  ──► data/events/keepalive-YYYY-MM-DD.jsonl (source: keepalive)
                             ──► keepalive.log (plaintext, legacy)
  └─► fleet-pulse.sh (cron) ──► data/events/fleet-YYYY-MM-DD.jsonl (source: pulse)
                             ──► state/pulse/pulse-summary.txt (human-readable)
                             ──► [FLEET-PULSE] notification to manager tmux
```

## Event Types

### Critical (require action)

| Type | Source | Meaning |
|------|--------|---------|
| `session_missing` | pulse | Bot's tmux session is gone |
| `service_down` | pulse | Bot's systemd/launchd unit is not active |
| `activity_stuck` | pulse | Bot is animating but hasn't made a tool call in >threshold seconds |
| `overdue_dispatch` | pulse | A dispatched task passed its deadline with no report |
| `script_error` | lib | A lifecycle script exited non-zero |

### Informational

| Type | Source | Meaning |
|------|--------|---------|
| `tool_call` | vitals | Bot used a tool (high volume — filter or skip in queries) |
| `keepalive` | keepalive | Periodic state check: BUSY, IDLE, RESTART, UNKNOWN |
| `pane_stuck` | pulse | Bot's pane content unchanged for >5 min |
| `wip_uncommitted` | pulse | Bot has uncommitted changes in a project repo |
| `session_event` | vitals | Session lifecycle (start, stop) |

## Diagnosis Decision Tree

**Bot is unresponsive:**

1. `tmux has-session -t <bot>` — is the session alive?
2. If no: `systemctl --user status <BOT_SERVICE>` — is the service running?
3. If service failed: `journalctl --user -u <BOT_SERVICE> -n 50` — what killed it?
4. If service running but no tmux: `lib/spin-up-bot.sh <bot-dir>` to re-enroll

**Bot is "stuck" (session alive, not making progress):**

1. `tmux capture-pane -t <bot> -p | tail -20` — what's on screen?
2. `claudlobby events --bot <bot> --type activity_stuck` — has fleet-pulse flagged it?
3. If at a permission prompt → the bot needs input
4. If spinner but no tool calls → restart: `systemctl --user restart <BOT_SERVICE>`

**Multiple bots down simultaneously:**

1. `claudlobby events --critical` — fleet-wide critical events
2. `lib/reconcile-fleet.sh <fleet>` — audit supervision state
3. `lib/reconcile-fleet.sh <fleet> --enroll` — re-enroll orphans
4. Check if a recent `claudlobby generate` changed unit file names without re-enrolling

**Script failures:**

1. `claudlobby events --type script_error --tail 10` — recent errors
2. Check the `data` field for `script` name, `exit_code`, and `message`
3. Run the failing script manually with `bash -x` for debug trace

## File Locations

| Path | Content | Retention |
|------|---------|-----------|
| `runtime/bots/<bot>/keepalive.log` | Plaintext keepalive state log | Rotated by log-rotate.sh (500 lines) |
| `runtime/bots/<bot>/data/events/fleet-*.jsonl` | Structured events from pulse + vitals | 7 days (configurable via `OBSERVABILITY_REAP_DAYS`) |
| `runtime/bots/<bot>/data/events/keepalive-*.jsonl` | Keepalive JSONL events | 7 days (configurable) |
| `runtime/bots/<bot>/data/.idle` | Idle marker — touched by keepalive.sh on IDLE, cleared on BUSY. Fleet-pulse reads mtime. | Transient (current state only) |
| `runtime/bots/<bot>/data/.last-tool-call` | Tool-call marker — touched by bot-vitals.sh on every hook. Stale mtime + no `.idle` = activity_stuck candidate. | Transient (current state only) |
| `state/fleet-state.json` | Per-bot current status + task | Persistent |
| `state/pulse/pulse-summary.txt` | Last fleet-pulse human-readable output | Overwritten each run |
| `state/pulse/<bot>.pane_hash` | Pane change detection markers | Persistent |
| `state/dispatch-log.jsonl` | Dispatch history for overdue tracking | Persistent |

## Configuration

Event behavior is controlled via `fleet.yaml` `observability:` block, which lands in each bot's `bot.conf`:

| Env var | Default | Meaning |
|---------|---------|---------|
| `OBSERVABILITY_PULSE_INTERVAL` | 300 | Seconds between fleet-pulse runs |
| `OBSERVABILITY_REAP_DAYS` | 7 | Days to retain event JSONL files |
| `OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD` | 1800 | Seconds before flagging activity_stuck |
| `OBSERVABILITY_DISPATCH_DEADLINE` | 1800 | Seconds before flagging overdue_dispatch |
