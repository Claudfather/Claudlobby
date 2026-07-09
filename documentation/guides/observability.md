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
| What's the bot doing right now? | tmux pane | `tmux -L "$(tmux_socket_for_bot runtime/bots/<bot>)" capture-pane -t <bot> -p \| tail -10` |
| How long has the fleet been up? | Uptime metrics | `claudlobby uptime` |
| What work completed? | Report-back ledger | `claudlobby report-back --since 24h` |
| Fleet-wide log search | Tail all logs | `lib/tail-fleet.sh --fleet <name> --grep ERROR` |
| Last pulse snapshot | Pulse summary file | `cat state/pulse/pulse-summary.txt` |

> Every bot runs its own private tmux server (`-L <socket>`, the socket name is the bot's `BOT_SERVICE`/`TMUX_SOCKET`) since per-bot-tmux-socket isolation shipped. A bare `tmux -t <bot>` targets the shared *default* server, which has none of your bots on it, and silently reports no session instead of erroring. The commands above resolve the socket via `tmux_socket_for_bot <bot-dir>` — `source lib/lib-common.sh` first (from the claudlobby repo root) to get it in scope — or skip raw tmux entirely and dispatch through `lib/dispatch.sh` / the `bot_tmux`/`bot_tmux_send` wrappers. See [advanced-patterns.md](../advanced-patterns.md) for the full model.

## Event Data Flow

```
Bot activity
  └─► bot-vitals.sh (hook) ──► data/events/fleet-YYYY-MM-DD.jsonl (source: vitals)
  └─► keepalive.sh (timer)  ──► data/events/keepalive-YYYY-MM-DD.jsonl (source: keepalive)
                             ──► keepalive.log (plaintext, legacy)
  └─► fleet-pulse.sh (cron) ──► data/events/fleet-YYYY-MM-DD.jsonl (source: pulse)
                             ──► state/pulse/pulse-summary.txt (human-readable)
                             ──► [FLEET-PULSE] notification to manager tmux
  └─► emit_failure_alert / emit_fleet_notice ──► state/events/fleet-YYYY-MM-DD.jsonl (fleet-root, bot:"fleet", source: alert/notice)
      (start-bot.sh, reload-fleet.sh,             ──► [FLEET-ALERT]/[FLEET-NOTICE] nudge to manager tmux
       weekly-worker-restart.sh, etc.)             ──► Telegram (loudest channel)
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
| `bridge_down` | pulse / alert | Live tmux session, but the bot's Telegram bridge (channel poller) isn't delivering. Raised per-pulse by `fleet-pulse.sh` once down past `OBSERVABILITY_BRIDGE_DOWN_GRACE` seconds, and separately by `start-bot.sh` at bring-up on a verified-dark bridge or missing token |
| `reload_failed` | alert | Daily `reload-fleet.sh` plugin/skill update or `claudlobby generate` failed |
| `restart_failed` | alert | Weekly worker bounce (`weekly-worker-restart.sh`) failed to bring the bot back up |
| `rc_timeout` | startup / alert | `start-bot.sh`'s remote-control readiness probe hit its `RC_READY_TIMEOUT_S` ceiling — the bot came up without `--remote-control`, so channel replies drop while inbound still arrives (the #533 outage class). Emitted once per (re)start; `fleet-pulse.sh` escalates it like its other crit types, so a fleet-wide TIMEOUT pages instead of sitting silent in every `startup.log` |

> **Not yet caught by `--critical`:** `bridge_down`, `reload_failed`, and `restart_failed` are operationally critical — all three page the manager via a tmux nudge + Telegram through `emit_failure_alert`/`emit_fleet_notice` — but aren't in `claudlobby/commands/events.py`'s `CRITICAL_TYPES` set, so `claudlobby events --critical` won't surface them. Query them explicitly (`claudlobby events --type bridge_down`). `reload_failed`, `restart_failed`, and `bridge_down` raised at bot bring-up also write to the fleet-root `state/events/` directory (see File Locations below), which `claudlobby events` doesn't read at all — only the pulse-sourced `bridge_down` lands in the normal per-bot `data/events/` path.

### Informational

| Type | Source | Meaning |
|------|--------|---------|
| `tool_call` | vitals | Bot used a tool (high volume — filter or skip in queries) |
| `keepalive` | keepalive | Periodic state check: BUSY, IDLE, RESTART, UNKNOWN |
| `pane_stuck` | pulse | Bot's pane content unchanged for >5 min |
| `wip_uncommitted` | pulse | Bot has uncommitted changes in a project repo |
| `session_event` | vitals | Session lifecycle (start, stop) |
| `send_miss` | dispatch | A cross-socket tmux send (dispatch, cross-bot nudge) found no live session on the resolved socket — logged breadcrumb, not escalated |

## Diagnosis Decision Tree

**Bot is unresponsive:**

1. `tmux -L "$(tmux_socket_for_bot <bot-dir>)" has-session -t <bot>` — is the session alive on its private socket?
2. If no: `systemctl --user status <BOT_SERVICE>` — is the service running?
3. If service failed: `journalctl --user -u <BOT_SERVICE> -n 50` — what killed it?
4. If service running but no tmux: `lib/spin-up-bot.sh <bot-dir>` to re-enroll

**Bot is "stuck" (session alive, not making progress):**

1. `tmux -L "$(tmux_socket_for_bot <bot-dir>)" capture-pane -t <bot> -p | tail -20` — what's on screen?
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
| `state/events/fleet-*.jsonl` | Fleet-root events not tied to one bot (`bot:"fleet"`) — `reload_failed`, `restart_failed`, `bridge_down` at bring-up (via `emit_failure_alert`/`emit_fleet_notice`), plus `script_error`/`send_miss` when emitted outside a bot context (host jobs). **Not read by `claudlobby events`**, which only scans per-bot `data/events/` | Not currently reaped — no `OBSERVABILITY_REAP_DAYS` sweep touches this path |

## Configuration

Event behavior is controlled via `fleet.yaml` `observability:` block, which lands in each bot's `bot.conf`:

| Env var | Default | Meaning |
|---------|---------|---------|
| `OBSERVABILITY_PULSE_INTERVAL` | 300 | Seconds between fleet-pulse runs |
| `OBSERVABILITY_REAP_DAYS` | 7 | Days to retain event JSONL files |
| `OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD` | 1800 | Seconds before flagging activity_stuck |
| `OBSERVABILITY_DISPATCH_DEADLINE` | 1800 | Seconds before flagging overdue_dispatch |
| `OBSERVABILITY_BRIDGE_DOWN_GRACE` | 300 | Seconds of post-(re)start grace before an actionable `bridge_down` fires (avoids flagging a poller still coming up after a restart) |
| `RC_READY_TIMEOUT_S` | 90 | Seconds `start-bot.sh` waits for the `remote-control is active` readiness string before logging TIMEOUT and emitting `rc_timeout`. A raw `start-bot.sh` env knob — **not** composed from the `observability:` block; lower it only to exercise the TIMEOUT path in tests, or raise it for slow hosts |
