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

## Running the Memory Check

```bash
# From an operator shell, request the selected, enabled host job once.
claudlobby host job run fleet-memory-check
```

The check is also scheduled by the selected host configuration. The public
job route does not take the old script's `--fleet` or `--threshold` flags;
review the configured job instead of treating an ad-hoc flag as a lasting
threshold change.

The script:

1. Reads `/proc/meminfo` (Linux) or `vm_stat` (macOS) for available RAM.
2. Sums RSS of all `claude`, `node`, and Python MCP processes owned by the
   current user via `ps aux`.
3. If available RAM drops below the reserve floor (`total * (100 - threshold) / 100`),
   raises a FLEET ALERT via the shared signal path (fleet event + manager tmux
   nudge + Telegram) — no per-script env needed; delivery works fleet-less.
4. Writes a fleet-wide summary line (fleet RSS, available RAM/%, reserve floor) to `lib/fleet-memory-check.log`, followed by a per-bot RSS breakdown — one line per bot directory, walked via that bot's tmux session process tree. Not a single line: the alert/OK verdict line is appended after.
5. Exits 0 unconditionally — monitoring must not abort the timer chain.

## What the 80% Threshold Means

The 80% threshold means "alert when less than 20% of total RAM remains
available." The check compares available RAM against a reserve floor:

```
available_mb < total_ram_mb * (100 - threshold) / 100  →  alert
```

For threshold=80 on a 15 GB host: alert fires when available RAM drops below
3 GB (20% of 15 GB). This catches swap pressure early without producing
nonsensical >100% values.

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

The public job route has no threshold override. The percentages above describe
the private check's policy, not flags to pass to `host job run`.

## What to Do When the Alert Fires

1. Check selected supervision and session state with `claudlobby --fleet FLEET
   fleet reconcile` and `claudlobby --fleet FLEET fleet status`.
2. Check the candidate bot for uncommitted WIP in its `projects/` checkouts.
   For permanent retirement, omit it from authored `fleet.yaml`, activate the
   reviewed configuration plan, then run `claudlobby --fleet FLEET bot remove BOT`.
   Add `--purge` only after reviewing retained directory contents. `bot stop`
   is a temporary supervised stop; `systemctl --user stop` is not a durable
   removal from the keepalive fleet.
3. If all bots are active, defer new dispatches until at least one completes.
4. Consider scaling to a host with more RAM if alerts are frequent.
5. Review MCP server counts — each unnecessary MCP adds ~70 MB.

## Relationship to Other Monitoring Scripts

| Script                      | What it monitors         | Alert channel  |
|-----------------------------|--------------------------|----------------|
| `lib/disk-monitor.sh`       | Disk usage %             | FLEET ALERT    |
| `lib/fleet-memory-check.sh` | Fleet RSS %              | FLEET ALERT    |
| `lib/keepalive.sh`          | Bot session liveness     | Log only       |
| `claudlobby fleet reconcile` | Supervision state      | CLI result     |
| `lib/creds-check.sh`        | Token expiry             | Log + Telegram (on ok↔fail transition) |

The selected host configuration schedules these checks; inspect it with
`claudlobby host job list` before requesting a one-shot `host job run`.
