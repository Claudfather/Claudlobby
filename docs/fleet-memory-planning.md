---
title: Fleet Memory Planning
description: Per-bot RSS estimates and host sizing guidelines for claudlobby fleets.
---

# Fleet Memory Planning

This document covers how to estimate per-bot memory usage, size your host for a
given fleet configuration, and use `lib/fleet-memory-check.sh` to monitor RSS
in production.

## Per-Bot Memory Estimates

These are RSS (Resident Set Size) figures measured on a Raspberry Pi 5 (8 GB)
and a Mac Mini M2 (16 GB). "RSS" is actual physical RAM held — not virtual
address space. Numbers are approximate and vary with context window fill.

| Bot configuration                  | Typical RSS  | Peak RSS    |
|------------------------------------|-------------|-------------|
| Claude Sonnet + 1 MCP server       | 300–500 MB  | ~600 MB     |
| Claude Opus + 1 MCP server         | 500–750 MB  | ~900 MB     |
| Claude Sonnet, no MCP              | 230–400 MB  | ~500 MB     |
| Each additional MCP server         | +70 MB      | +100 MB     |
| Python-based MCP server (uvx/uv)   | +80–120 MB  | +150 MB     |
| Node-based MCP server              | +60–90 MB   | +100 MB     |

### Why RSS Varies So Much

- **Context window fill:** Claude Code loads file content into context. A bot
  actively working through a large codebase may use 2-3x its idle RSS.
- **MCP server count:** Each MCP server is a separate process (node, Python, or
  binary). They start small but grow with activity.
- **Model tier:** Opus is not a heavier process than Sonnet — both run the same
  claude-code binary — but Opus bots tend to be assigned heavier tasks, which
  drives more file I/O and larger context, hence higher observed RSS.

## Max-Bots-Per-Host Guidelines

These assume the 80% safety threshold (reserve 20% for OS + non-fleet work).
Numbers are conservative; your fleet may run leaner if bots are idle most of
the time or use minimal MCP.

### Raspberry Pi 5 — 8 GB RAM (~6.4 GB usable at 80%)

| Bot mix                              | Max bots | Notes                        |
|--------------------------------------|----------|------------------------------|
| All Sonnet + 1 MCP each              | 10–12    | Typical mixed fleet          |
| All Sonnet, no MCP                   | 12–16    | Lightweight, terminal-only   |
| Mixed Sonnet/Opus + 1-2 MCP each     | 6–8      | Heavier workloads            |
| All Opus + 2 MCP each                | 4–5      | Research/analysis fleet      |

**Practical limit on Pi 5:** 8 bots (Sonnet + 1 MCP each) with headroom for
os updates and log rotation. Above 10 active bots, enable swap (at least 4 GB
on a fast SD card or USB SSD) to absorb peaks.

### Mac Mini M2 — 16 GB RAM (~12.8 GB usable at 80%)

| Bot mix                              | Max bots | Notes                        |
|--------------------------------------|----------|------------------------------|
| All Sonnet + 1 MCP each              | 20–25    | Comfortable                  |
| Mixed Sonnet/Opus + 2 MCP each       | 12–16    | Production workhorse         |
| All Opus + 3 MCP each                | 8–10     | Heavy research fleet         |

### Server / Cloud Instance — 32 GB RAM (~25.6 GB usable at 80%)

| Bot mix                              | Max bots | Notes                             |
|--------------------------------------|----------|-----------------------------------|
| All Sonnet + 1 MCP each              | 40–50    | Use fleet YAML sharding           |
| Mixed + 2-3 MCP each                 | 20–30    | Practical upper bound before mgmt overhead |
| Opus-heavy + many MCP                | 15–20    | Diminishing returns above ~20     |

**Note:** Past ~20 active bots, the manager bot's dispatch loop and
`keepalive-all.sh` add overhead. Profile before pushing beyond 25 concurrent
bots on a single host.

## Running the Memory Check Script

```bash
# One-shot check, print to stdout and log
lib/fleet-memory-check.sh

# With a fleet overlay
lib/fleet-memory-check.sh --fleet crog-eng-team

# Custom threshold (warn at 85% instead of 80%)
lib/fleet-memory-check.sh --threshold 85

# Add to cron — run every 5 minutes
# Telegram alert fires automatically when threshold is crossed.
*/5 * * * * CLAUDLOBBY_ROOT=$HOME/claudlobby TELEGRAM_GROUP_CHAT_ID=<chat_id> \
    $HOME/claudlobby/lib/fleet-memory-check.sh --fleet <name> >> /dev/null 2>&1
```

The script:

1. Reads `/proc/meminfo` (Linux) or `vm_stat` (macOS) for available RAM.
2. Sums RSS of all `claude`, `node`, and Python MCP processes owned by the
   current user via `ps aux`.
3. If `fleet RSS / total RAM >= threshold`, calls `lib/tg-post.sh` to fire a
   Telegram alert (requires `TELEGRAM_GROUP_CHAT_ID` in environment).
4. Writes a one-line status entry to `lib/fleet-memory-check.log`.
5. Exits 0 unconditionally — a RAM threshold crossing must not abort cron chains.

## What the 80% Threshold Means

The 80% threshold is the fraction of **total** RAM at which fleet processes
should trigger an alert:

```
fleet_rss_mb / total_ram_mb * 100 >= threshold  →  alert
```

Reserving 20% covers:

- Linux kernel buffers and page cache (typically 200-500 MB active)
- systemd, sshd, and other host services (~100-200 MB)
- Burst headroom: a bot spiking while loading a large context
- Log rotation, git operations, and cron jobs

**Tuning guidance:**

| Situation                                    | Recommended threshold |
|----------------------------------------------|-----------------------|
| Pi with no swap, bursty bots                 | 70%                   |
| Pi with 4 GB swap on fast SSD                | 85%                   |
| Mac Mini / server, always-on production      | 80% (default)         |
| Dev box, can tolerate OOM killer             | 90%                   |

To change the default, pass `--threshold N` or set a cron environment variable.
There is intentionally no config file — the threshold is a single call-site
decision.

## What to Do When the Alert Fires

1. Check which bots are idle: `lib/reconcile-fleet.sh <fleet>`
2. Stop the most RAM-hungry idle bots:
   `systemctl --user stop <bot-name>.service`
3. If all bots are active, defer new dispatches until at least one completes.
4. Consider scaling to a host with more RAM if alerts are frequent.
5. Review MCP server counts — each unnecessary MCP adds ~70 MB.

## Relationship to Other Monitoring Scripts

| Script                      | What it monitors         | Alert channel  |
|-----------------------------|--------------------------|----------------|
| `lib/disk-monitor.sh`       | Disk usage %             | Telegram       |
| `lib/fleet-memory-check.sh` | Fleet RSS %              | Telegram       |
| `lib/keepalive.sh`          | Bot session liveness     | Log only       |
| `lib/reconcile-fleet.sh`    | Supervision state        | stdout         |
| `lib/creds-check.sh`        | Token expiry             | Log + stdout   |

Run `disk-monitor.sh` and `fleet-memory-check.sh` together in the same cron
block for a complete host-health snapshot.
