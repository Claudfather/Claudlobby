---
title: Fleet Observability — Where to Look
description: Decision tree for diagnosing fleet issues from logs, events, and CLI tools
---

# Fleet Observability — Where to Look

## Quick Reference

| Question | Where to look | Command |
|----------|---------------|---------|
| Is the fleet healthy? | Fleet status dashboard | `claudlobby status` |
| What happened recently? | The plane (`state/plane/plane.db`) | `claudlobby events --critical --tail 20` |
| Is a specific bot stuck? | The plane's heartbeat samples | `claudlobby status` (the newest heartbeat) / `claudlobby uptime --bot <bot>` (the history) / `claudlobby events --bot <bot> --source keepalive` (the transitions) |
| Why did a bot restart? | Keepalive events + journal | `claudlobby events --bot <bot> --type keepalive` |
| Did a script fail? | Script error events | `claudlobby events --type script_error` |
| Is a service down? | systemd journal | `journalctl --user -u <BOT_SERVICE> -n 30` |
| What's the bot doing right now? | tmux pane | `tmux -L "$(tmux_socket_for_bot runtime/bots/<bot>)" capture-pane -t <bot> -p \| tail -10` |
| How long has the fleet been up? | Uptime metrics | `claudlobby uptime` |
| What work completed? | The plane (the report door's task events) | `claudlobby report-back --since 24h` |
| What did a manager decide at its last check-in, and why? | The plane (the check-in's `checkin_decision` rows, joined through `checkin_dispatch` to the task's status) | `claudlobby checkins --bot <b> --last` (`--json` for tools) |
| How is a manager's check-in window distributed: actions, ask rate, what it could not read, dispatch outcomes — by project? | The plane (the decision rows, rolled up) | `claudlobby checkins --summary --since 14d` |
| Fleet-wide log search | Tail all logs | `lib/tail-fleet.sh --fleet <name> --grep ERROR` |
| Last pulse snapshot | Pulse summary file | `cat state/pulse/pulse-summary.txt` |
| Is the observable-plane kernel healthy? | Plane kernel status (db/spool/quarantine) | `claudlobby plane doctor` |

> Every bot runs its own private tmux server (`-L <socket>`, the socket name is the bot's `BOT_SERVICE`/`TMUX_SOCKET`) since per-bot-tmux-socket isolation shipped. A bare `tmux -t <bot>` targets the shared *default* server, which has none of your bots on it, and silently reports no session instead of erroring. The commands above resolve the socket via `tmux_socket_for_bot <bot-dir>` — `source lib/lib-common.sh` first (from the claudlobby repo root) to get it in scope — or skip raw tmux entirely and dispatch through `lib/dispatch.sh` / the `bot_tmux`/`bot_tmux_send` wrappers. See [advanced-patterns.md](../advanced-patterns.md) for the full model.

> **The plane is the fleet's only record.** `emit_fleet_event` and every door (`dispatch-task.sh`, `report-back.sh`, `keepalive.sh`, `bot-vitals.sh`, the hooks) land on `state/plane/plane.db`; `claudlobby events` / `report-back` / `uptime` / `status` / `brief` read it; `plane prune` ages its metric samples. Health: `claudlobby plane status` / `plane doctor`.

## Event Data Flow

```
Bot activity
  └─► bot-vitals.sh (hook)      ──► emit_fleet_event ──► state/plane/plane.db (source: vitals)
  └─► keepalive.sh (timer)      ──► bot.heartbeat / bot.session_up metric samples + keepalive_* events ──► the plane
  └─► fleet-pulse.sh (cron)     ──► emit_fleet_event ──► the plane (source: pulse)
                                ──► state/pulse/pulse-summary.txt (human-readable)
                                ──► [FLEET-PULSE] notification to manager tmux
  └─► emit_failure_alert / emit_fleet_notice ──► emit_fleet_event ──► the plane (anchored on the fleet, source: alert/notice)
      (start-bot.sh, reload-fleet.sh, …)      ──► [FLEET-ALERT]/[FLEET-NOTICE] nudge to manager tmux
Readers: claudlobby events / report-back / uptime / status / brief; the plane's samples age under `plane prune`.
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
| `bot_teardown_started` | spin-down | `spin-down-bot.sh` was invoked on a bot: records the door (`action`), `actor`, `fleet`, `bot_dir`, `expected_return`, and `reason`. Emitted BEFORE the teardown legs run, so it records an intent, not a confirmed outcome — a crash mid-teardown still leaves the record. **Dormant unless the fleet sets `SPINDOWN_RECEIPT_ENABLED=1`**, so an unarmed fleet writes no rows and an empty result means *not armed*, not *no teardowns* |
| `reload_failed` | alert | Daily `reload-fleet.sh` plugin/skill update or `claudlobby generate` failed |
| `restart_failed` | alert | Weekly worker bounce (`weekly-worker-restart.sh`) failed to bring the bot back up |
| `rc_timeout` | startup / alert | `start-bot.sh`'s readiness poll (the Telegram poller's `bridge_state=up`, session-scoped) hit its `RC_READY_TIMEOUT_S` ceiling before the poller came up, so channel replies drop while inbound still arrives (the #533 outage class). Emitted once per (re)start; `fleet-pulse.sh` escalates it like its other crit types, so a fleet-wide TIMEOUT pages instead of sitting silent in every `startup.log` |
| `crash_loop` | pulse | The bot's unit fails EVERY start and systemd keeps restarting it: it is mid-start (`activating/*`, `active/running`) with at least 2 automatic restarts in this streak (`NRestarts`; #1769). Data: `unit`, `restarts`, `state`. Critical: `fleet-pulse.sh` escalates it and pushes the manager a note naming `logs/startup.log`, and for that bot raises no `session_missing` or `service_down`, whose remedies (re-enroll, restart) are wrong while systemd is already retrying; keepalive skips it rather than restarting. Both hold except in the few-millisecond `deactivating/stop-post` window between two attempts, which reads no verdict. **Its severity is stamped at ingest by the resident plane daemon, from the registry it loaded at start: a daemon started before a type was registered stores it with no severity until restarted, so it never reaches the escalation, `brief` or `events --critical` (the manager push still fires)** |

> **One plane, one reader:** `reload_failed`, `restart_failed`, and `bridge_down` raised at bot bring-up are anchored on the FLEET's identity (a fleet-level receipt), the pulse-sourced `bridge_down` on the bot's; `claudlobby events` reads both from the plane, so a type can appear from either. `bot_teardown_started` is deliberately **not** in `CRITICAL_TYPES` — `spin-down-bot.sh` is also the throwaway-canary reaper, so `--critical` would fill with expected noise. Query it explicitly (`claudlobby events --type bot_teardown_started`).

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
| `state/plane/plane.db` | The plane: every event, dispatch, report and heartbeat sample (F18 closure — the per-bot and fleet-root event files are gone); read with `claudlobby events` / `report-back` / `uptime` / `brief` | Append-only; metric samples aged by `plane prune` (30d) |
| `runtime/bots/<bot>/data/.idle` | Idle marker — touched by keepalive.sh on IDLE, cleared on BUSY. Fleet-pulse reads mtime. | Transient (current state only) |
| `runtime/bots/<bot>/data/.last-tool-call` | Tool-call marker — touched by bot-vitals.sh on every hook. Stale mtime + no `.idle` = activity_stuck candidate. | Transient (current state only) |
| `state/fleet-state.json` | Per-bot current status + task | Persistent |
| `state/pulse/pulse-summary.txt` | Last fleet-pulse human-readable output | Overwritten each run |
| `state/pulse/<bot>.pane_hash` | Pane change detection markers | Persistent |

## Configuration

Event behavior is controlled via `fleet.yaml` `observability:` block, which lands in each bot's `bot.conf`:

| Env var | Default | Meaning |
|---------|---------|---------|
| `OBSERVABILITY_PULSE_INTERVAL` | 300 | Seconds between fleet-pulse runs |
| `OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD` | 1800 | Seconds before flagging activity_stuck |
| `OBSERVABILITY_DISPATCH_DEADLINE` | 86400 (24h) — the `system.yaml` tier, the composer constant and the door's fallback all | Seconds before flagging overdue_dispatch; `0` = open-ended, no deadline minted |
| `OBSERVABILITY_BRIDGE_DOWN_GRACE` | 300 | Seconds of post-(re)start grace before an actionable `bridge_down` fires (avoids flagging a poller still coming up after a restart) |
| `OBSERVABILITY_BRIDGE_HEAL` | 0 (off) | Tier-2 Telegram-bridge self-heal gate. When `1`, `keepalive.sh` bounces a bot whose poller is verified-dark on an **idle** tick — the only respawn, since the `bun server.ts` poller is an MCP stdio child of `claude` — reusing the same restart ladder as the dead-session watchdog. The `.spawn` grace spaces retries; `BRIDGE_HEAL_MAX_ATTEMPTS` caps them, then it escalates once and stops. `no_token`/`unknown`/`no_handle` never bounce. **Ships OFF**: enabling the bounce fleet-wide is gated on the production bounce→recovery telemetry (issue #453 Fork F6b). Enable via a bot/fleet `env:` entry once the gate clears |
| `BRIDGE_HEAL_MAX_ATTEMPTS` | 3 | Max bridge-heal bounces before `keepalive.sh` stops and escalates once (F3 escalate-only); the bot still serves tmux dispatch throughout. Only consulted when `OBSERVABILITY_BRIDGE_HEAL=1` |
| `RC_READY_TIMEOUT_S` | 200 at package defaults (derived, not flat) | Seconds `start-bot.sh` waits for the Telegram poller's `bridge_state=up` (session-scoped) before logging TIMEOUT and emitting `rc_timeout`. Composed since #1573 — from `host.boot.mcp_timeout_ms` via `BootPolicy` (`max(90, mcp_timeout_ms // 1000 + 20)`), **not** from this `observability:` block. `bot.conf`'s composed value wins whenever the key is present; the bare `90` env fallback fires only for an un-regenerated `bot.conf` that predates the key (F4). Lower `host.boot.mcp_timeout_ms` (or the env fallback, pre-regenerate) only to exercise the TIMEOUT path in tests, or raise it for slow hosts |
