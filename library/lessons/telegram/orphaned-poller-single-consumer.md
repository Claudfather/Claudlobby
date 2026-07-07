---
title: "Lesson: an orphaned Telegram poller silently steals the bot token's single-consumer slot"
---

Telegram's `getUpdates` allows **one consumer per bot token**. When a Claude Code session dies without cleanly shutting down its Telegram channel plugin, the poller child (`bun server.ts`) can survive, reparented to init/launchd — a **deaf orphan** that keeps long-polling with the token while delivering messages to a parent that no longer exists.

## Symptoms (observed live, macOS host, orphan survived two months)

- Every *new* session's Telegram MCP server **flaps** — connect, 409-conflict against the orphan, disconnect, reconnect — for as long as the orphan lives.
- Inbound messages are **randomly swallowed**: whichever poller wins the slot for that poll cycle gets the message; the orphan's copies go nowhere.
- The conflicted poller **hot-loops with no backoff** — the observed orphan burned ~100% of a core continuously (~23.5 CPU-days) and did not service SIGTERM while hot (`kill -9` required).
- `<state-dir>/bot.pid` goes stale: it names whichever poller last stamped it, not necessarily the live one.

## Diagnosis

```bash
ps aux | grep -E "bun.*server\.ts" | grep -v grep     # more than one? suspect
ps -o pid,ppid,lstart,command -p <pid>                # ppid 1 (init/launchd) = orphan
cat <state-dir>/bot.pid                                # who owns the slot on paper
```

An orphan's tell: `ppid == 1` and a start date matching a long-dead session. On Linux, `bridge_state` (lib-common) automates exactly this ownership walk; on macOS it returns `unknown` — see the issue tracking the port.

## Fix

`kill <wrapper-pid> <poller-pid>`; escalate to `kill -9` if CPU is pegged (TERM is not serviced mid-hot-loop). The next live session's bridge claims the slot within one reconnect and `bot.pid` re-stamps correctly. Verify: exactly one `bun server.ts` remains, CPU near 0%, MCP flapping stops.

## Prevention / fleet posture

- Fleet bots on Linux are covered: `bridge_state`'s ancestor walk reads a reparented orphan as `no_bridge`, and fleet-pulse raises `bridge_down` within a pulse interval.
- macOS hosts (and interactive dev machines) have no detection today — ownership-check port + host-level orphan reaper are tracked as framework work.
- The plugin-level defects (no 409 backoff, no parent-death watchdog, unserviced TERM) are upstream; the fleet defense stands regardless.
