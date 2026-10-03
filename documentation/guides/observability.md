---
title: Fleet Observability — Where to Look
description: Decision tree for diagnosing fleet issues from logs, events, and CLI tools
---

# Fleet Observability — Where to Look

## Quick Reference

| Question | Where to look | Command |
|----------|---------------|---------|
| Is the fleet healthy? | Fleet status dashboard | `claudlobby fleet status` |
| What happened recently? | The plane (`state/plane/plane.db`) | `claudlobby event list --critical --limit 20` |
| Is a specific bot stuck? | The plane's heartbeat samples | `claudlobby fleet status` (the newest heartbeat) / `claudlobby fleet uptime --bot <bot>` (the history) / `claudlobby event list --bot <bot> --source keepalive` (the transitions) |
| Why did a bot restart? | Keepalive events + journal | `claudlobby event list --bot <bot> --type keepalive_restart` |
| Did a script fail? | Script error events | `claudlobby event list --type script_error` |
| Is a service down? | systemd journal | `journalctl --user -u <BOT_SERVICE> -n 30` |
| What's the bot doing right now? | tmux pane | `tmux -L "$(tmux_socket_for_bot runtime/bots/<bot>)" capture-pane -t <bot> -p \| tail -10` |
| How long has the fleet been up? | Uptime metrics | `claudlobby fleet uptime` |
| What work completed? | The plane's fleet reports | `claudlobby --json fleet reports list --status completed --since "$CUTOFF"` (`CUTOFF` must be an offset-bearing RFC3339 instant derived for the intended window; follow `next_cursor`) |
| What did a manager decide at its last check-in, and why? | The plane (the check-in's `checkin_decision` rows, joined through `checkin_dispatch` to the task's status) | `claudlobby --json --fleet <F> checkin list --bot <b> --last`; inspect `data.items[0]` |
| How is a manager's check-in window distributed: actions, ask rate, what it could not read, dispatch outcomes — by project? | The plane (the decision rows, rolled up) | `claudlobby --json --fleet <F> checkin list --summary --since 14d` |
| Recent fleet or bot logs | Bounded selected log files, with missing sources reported per bot | `claudlobby --fleet <name> fleet logs --lines 20` or `claudlobby --fleet <name> bot logs <bot> --lines 20` |
| Last pulse snapshot | The fleet's pulse summary file | `cat state/pulse/<fleet>.pulse-summary.txt` |
| Is the observable-plane kernel healthy? | Plane kernel status (db/spool/quarantine) | `claudlobby plane doctor` |

> Every bot runs its own private tmux server (`-L <socket>`, the socket name is the bot's `BOT_SERVICE`/`TMUX_SOCKET`) since per-bot-tmux-socket isolation shipped. A bare `tmux -t <bot>` targets the shared *default* server, which has none of your bots on it. For the read-only pane inspection above, source `claudlobby/_runtime_scripts/lib-common.sh` from the repository root to resolve `tmux_socket_for_bot <bot-dir>`. To send work, use the public CLI's separate `task admit`, `task assign`, and `assignment deliver` operations; ordinary messages use `message send`. See [the CLI examples](../advanced-patterns.md#5-inter-bot-communication-and-reports) and `/fleet-ops` for arguments and receipt handling.

> **The plane is the fleet's only record.** `emit_fleet_event` and fleet doors land on `state/plane/plane.db`; `claudlobby event list` / `fleet reports list` / `fleet uptime` / `fleet status` / `brief` read it; `plane prune` ages its metric samples. Health: `claudlobby plane status` / `plane doctor`.

## Event Data Flow

```
Bot activity
  └─► bot-vitals.sh (hook)      ──► emit_fleet_event ──► state/plane/plane.db (source: vitals)
  └─► keepalive.sh (timer)      ──► bot.heartbeat / bot.session_up metric samples + keepalive_* events ──► the plane
  └─► fleet-pulse.sh (cron)     ──► emit_fleet_event ──► the plane (source: pulse)
                                ──► state/pulse/<fleet>.pulse-summary.txt (human-readable)
                                ──► [FLEET-PULSE] notification to manager tmux
  └─► emit_failure_alert / emit_fleet_notice ──► emit_fleet_event ──► the plane (anchored on the fleet, or the host for a host job; source: alert/notice)
      (start-bot.sh, reload-fleet.sh, …)      ──► [FLEET-ALERT]/[FLEET-NOTICE] nudge to manager tmux
Readers: claudlobby event list / fleet reports list / fleet uptime / fleet status / brief; the plane's samples age under `plane prune`.
```

## Event Types

### Critical (require action)

| Type | Source | Meaning |
|------|--------|---------|
| `session_missing` | pulse | Bot's tmux session is gone |
| `service_down` | pulse | Bot's systemd/launchd unit is not active |
| `activity_stuck` | pulse | Bot is animating but hasn't made a tool call in >threshold seconds |
| `input_held` | pulse | The bot's input box holds text that was never submitted and no turn is running, past `OBSERVABILITY_INPUT_HELD_THRESHOLD` (default 300 s). It is not hung: an operator presses Enter in its pane, and a restart would discard the text |
| `overdue_dispatch` | pulse | A dispatched task passed its deadline with no report |
| `script_error` | lib | A lifecycle script exited non-zero |
| `bridge_down` | pulse / alert | Live tmux session, but the bot's Telegram bridge (channel poller) isn't delivering. Raised per-pulse by `fleet-pulse.sh` once down past `OBSERVABILITY_BRIDGE_DOWN_GRACE` seconds, and separately by `start-bot.sh` at bring-up on a verified-dark bridge or missing token |
| `reload_failed` | alert | Daily `reload-fleet.sh` plugin/skill update or `claudlobby generate` failed, or a run was killed or aborted before it finished (the reason names the step it died in; a SIGKILL is raised by the next run, #1924) |
| `restart_failed` | alert | Weekly worker bounce (`weekly-worker-restart.sh`) failed to bring the bot back up |
| `rc_timeout` | startup / alert | `start-bot.sh`'s readiness poll (the Telegram poller's `bridge_state=up`, session-scoped) hit its `RC_READY_TIMEOUT_S` ceiling before the poller came up, so channel replies drop while inbound still arrives (the #533 outage class). Emitted once per (re)start; `fleet-pulse.sh` escalates it like its other crit types, so a fleet-wide TIMEOUT pages instead of sitting silent in every `startup.log` |
| `crash_loop` | pulse | The bot's unit fails EVERY start and systemd keeps restarting it: it is mid-start (`activating/*`, `active/running`) with at least 2 automatic restarts in this streak (`NRestarts`; #1769). Data: `unit`, `restarts`, `state`. Critical: `fleet-pulse.sh` escalates it and pushes the manager a note naming `logs/startup.log`, and for that bot raises no `session_missing` or `service_down`, whose remedies (re-enroll, restart) are wrong while systemd is already retrying; keepalive skips it rather than restarting. Both hold except in the few-millisecond `deactivating/stop-post` window between two attempts, which reads no verdict. **Its severity is stamped at ingest by the resident plane daemon, from the registry it loaded at start: a daemon started before a type was registered stores it with no severity until restarted, so it never reaches the escalation, `brief` or `events --critical` (the manager push still fires)** |
| `rolling_restart_stalled` | alert | `rolling-restart.sh` halted: a bot failed its restart or its bridge readiness, so the rest of the fleet was not restarted |
| `keepalive_failed` | alert | `keepalive-all.sh`: keepalive failed to run for a bot (admission or runtime), so the watchdog that restarts a dead session did not run for it |
| `alert_target_refused` | alert | The fleet's alert Telegram target was refused (`fleet-pulse.sh`, `creds-check.sh`): the watchdog's page channel is dark by configuration |
| `alert_pair_unreachable` | alert | `creds-check.sh`: the fleet's alert chat is not reachable by its sender, or the sender holds no token |
| `disk_high` | alert | `disk-monitor.sh`: disk usage on the checked mount past its threshold |
| `memory_high` | alert | `fleet-memory-check.sh`: the fleet's memory use against the host's available RAM, past its threshold |
| `undervoltage`, `storage_stall`, `host_health` | alert | `host-health-check.sh`: Pi under-voltage or throttling, an SD/MMC storage stall, or (`host_health`) a finding that is neither |
| `binary_update_failed`, `binary_unrunnable` | alert | `update-claude-code.sh`: the staged `claude` binary failed to install, or the binary the fleet launches cannot run |
| `vault_sync_failed` | alert | `vault-sync.sh`: a scheduled vault sync failed; the job never resolves a conflict |
| `fleet_alert` | fleet-notify | `claudlobby fleet notify --level alert`: the caller's own event name and message ride in its data |
| `public_write_guard_unarmed` | hook | The public-write guard is on, but the host has no term list (`~/.config/claudlobby/public-write-terms`), so it lets each GitHub write it checks through and records this. It does not page; `public-write-guard.py --check` says whether a host is armed |
| `shadow_parity_diverged` | plane | Historical: the plane cutover's shadow recorded it. Nothing records it now; it stays registered so its rows still classify |

> **Where an alert is recorded decides who can read it.** A FLEET ALERT (`alert` source) raised by a fleet job or at a bot's bring-up is anchored on the FLEET's identity (a fleet-level receipt, bot `fleet` in `claudlobby event list`), the pulse-sourced `bridge_down` on the bot's; `event list` reads both, so a type can appear from either. A host job runs with no fleet, so its alerts (`disk_high`, `memory_high`, `undervoltage`, `storage_stall`, `host_health`, `binary_update_failed`, `binary_unrunnable`, `vault_sync_failed`) are anchored on the host and appear in no fleet's `event list`. A bot's `brief` reads only that bot's own rows, so it shows neither kind (#2109). Every FLEET ALERT still reaches a manager's pane and Telegram when it is raised.

> **Severity is the registry's,** `SYSTEM_EVENT_SEVERITY` in `claudlobby/plane/registries.py`, stamped at ingest. `bot_teardown_started` is registered **notice**, not critical: `spin-down-bot.sh` is also the throwaway-canary reaper, so `--critical` would fill with expected noise. Query it explicitly (`claudlobby event list --type bot_teardown_started`).

### Informational

| Type | Source | Meaning |
|------|--------|---------|
| `tool_call` | vitals | Bot used a tool (high volume — filter or skip in queries) |
| `keepalive_restart`, `keepalive_skip`, `keepalive_reload`, `bridge_heal` | keepalive | A keepalive transition: it restarted a dead session, declined a restart (the session reappeared, a crash loop, a boot in flight), sent an idle bot `/reload-plugins` and `/reload-skills`, or bounced or reset a dark Telegram bridge. The per-tick verdict (BUSY, IDLE, HELD, UNKNOWN) rides the `bot.heartbeat` sample, not an event |
| `bot_teardown_started` | spin-down | `spin-down-bot.sh` was invoked on a bot: records the door (`action`), `actor`, `fleet`, `bot_dir`, `expected_return`, and `reason`. Emitted BEFORE the teardown legs run, so it records an intent, not a confirmed outcome — a crash mid-teardown still leaves the record. **Dormant unless the fleet sets `SPINDOWN_RECEIPT_ENABLED=1`**, so an unarmed fleet writes no rows and an empty result means *not armed*, not *no teardowns* |
| `pane_stuck` | pulse | Bot's pane content unchanged for >5 min |
| `wip_uncommitted` | pulse | Bot has uncommitted changes in a project repo |
| `session_event` | vitals | Session lifecycle (start, stop) |
| `send_miss` | dispatch | A cross-socket tmux send (dispatch, cross-bot nudge) found no live session on the resolved socket — logged breadcrumb, not escalated |
| `job_reenroll_deferred` | notice | Historical notice from the retired fleet setup path: a launchd job could not apply its changed plist while it was running its own enrollment. Kept readable for older Plane records; sealed host activation now owns enrollment. |

## Diagnosis Decision Tree

**Bot is unresponsive:**

1. `tmux -L "$(tmux_socket_for_bot <bot-dir>)" has-session -t <bot>` — is the session alive on its private socket?
2. If no: `systemctl --user status <BOT_SERVICE>` — is the service running?
3. If service failed: `journalctl --user -u <BOT_SERVICE> -n 50` — what killed it?
4. If service running but no tmux: `claudlobby/_runtime_scripts/spin-up-bot.sh <bot-dir>` to re-enroll

**Bot is "stuck" (session alive, not making progress):**

1. `tmux -L "$(tmux_socket_for_bot <bot-dir>)" capture-pane -t <bot> -p | tail -20` — what's on screen?
2. `claudlobby event list --bot <bot> --type activity_stuck` — has fleet-pulse flagged it?
3. If at a permission prompt → the bot needs input
4. If spinner but no tool calls → restart: `systemctl --user restart <BOT_SERVICE>`

**Multiple bots down simultaneously:**

1. `claudlobby event list --critical` — fleet-wide critical events
2. `claudlobby/_runtime_scripts/reconcile-fleet.sh <fleet>` — audit supervision state
3. `claudlobby/_runtime_scripts/reconcile-fleet.sh <fleet> --enroll` — re-enroll orphans
4. Check if a recent `claudlobby generate` changed unit file names without re-enrolling

**Script failures:**

1. `claudlobby event list --type script_error --limit 10` — recent errors
2. Check the `data` field for `script` name, `exit_code`, and `message`
3. Run the failing script manually with `bash -x` for debug trace

## File Locations

| Path | Content | Retention |
|------|---------|-----------|
| `runtime/bots/<bot>/keepalive.log` | Plaintext keepalive state log | Rotated by log-rotate.sh (500 lines) |
| `state/plane/plane.db` | The plane: every event, dispatch, report and heartbeat sample (F18 closure — the per-bot and fleet-root event files are gone); read with `claudlobby event list` / `fleet reports list` / `fleet uptime` / `brief` | Append-only; metric samples aged by `plane prune` (30d) |
| `runtime/bots/<bot>/data/.idle` | Idle marker — touched by keepalive.sh on IDLE, cleared on BUSY. Fleet-pulse reads mtime. | Transient (current state only) |
| `runtime/bots/<bot>/data/.last-tool-call` | Tool-call marker — touched by bot-vitals.sh on every hook. Stale mtime + no `.idle` = activity_stuck candidate. | Transient (current state only) |
| `state/fleet-state.json` | Per-bot current status + task | Persistent |
| `state/pulse/<fleet>.pulse-summary.txt` | Last fleet-pulse human-readable output | Overwritten each run |
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
