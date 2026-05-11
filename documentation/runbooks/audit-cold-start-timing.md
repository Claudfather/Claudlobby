# Cold Start Timing Audit

Empirical profiling of fleet cold start. All numbers measured on production hardware under normal load. May 9, 2026.

## Hardware Baseline

| Component | Spec |
|-----------|------|
| Platform | Raspberry Pi 5 (aarch64) |
| CPU | 4 cores (BCM2712) |
| RAM | 16 GB (15 Gi usable) |
| Storage | 256 GB SD card (mmcblk0) |
| IO scheduler | mq-deadline (with kyber, bfq available) |
| Sequential write | ~19 MB/s |
| Sequential read | ~3.6 GB/s (page cache; raw card ~90-100 MB/s typical) |
| Kernel | 6.12.75+rpt-rpi-2712 PREEMPT |

At steady state with 8 crog-eng-team bots + 2 tl-enterprises bots running:
- **RAM used:** 7.7 Gi / 15 Gi (51%)
- **Claude processes:** ~11, totaling ~5.1 GB RSS
- **Node/MCP processes:** ~21, totaling ~1.1 GB RSS
- Per-bot claude process: 250-750 MB depending on context usage and uptime

## End-to-End Startup Timing

### Single bot (warm cache, no contention)

Test subject: Navi (1 MCP server, 18 KB CLAUDE.md, Sonnet model).

| Run | remote-control active | start-bot.sh complete |
|-----|----------------------|----------------------|
| Navi #1 | 5.1s | ~16s (incl 5s sleep + startup prompt) |
| Virgil #1 | 4.1s | ~14s |

### Parallel 2-bot start (warm cache)

| Bot | remote-control active |
|-----|----------------------|
| Virgil | 6.3s |
| Navi | 7.3s |

~40% slower than single-bot due to CPU contention on 4 cores.

### Full fleet start (8 bots, journal data from May 8 17:36)

systemd dispatched all 8 units over a 7-second window (17:36:47–17:36:54). start-bot.sh scripts ran in parallel. Completion times from journal:

| Bot | systemd start | start-bot.sh done | Wall clock |
|-----|--------------|-------------------|------------|
| greg | 17:36:47 | 17:37:03 | 16s |
| branden | 17:36:48 | 17:37:07 | 19s |
| craig | 17:36:47 | 17:37:09 | 22s |
| navi | 17:36:50 | 17:37:11 | 21s |
| rajan | 17:36:49 | 17:37:13 | 24s |
| virgil | 17:36:51 | 17:37:19 | 28s |
| mason | 17:36:52 | 17:37:19 | 27s |
| ari | 17:36:54 | 17:37:23 | 29s |

**Total fleet cold start: 36 seconds** (first unit dispatched to last start-bot.sh complete).

Ari is slowest: 2 MCP servers (github + notion), largest CLAUDE.md (38 KB, 642 lines), Opus model.

## Component Breakdown

Measured in isolation, single-bot, warm cache:

| Component | Time | Notes |
|-----------|------|-------|
| systemctl dispatch | ~37ms | Negligible |
| env loading (3-tier source) | ~8ms | 4 files sourced |
| tmux session creation | <100ms | First bot creates tmux server |
| Claude CLI binary load | ~670ms | Native binary, not npx |
| Claude init → remote-control active | ~3-4s | CLAUDE.md parse, MCP handshake, channel plugin |
| ~~Hardcoded `sleep 5`~~ | ~~5000ms~~ | **Removed** — was fixed cost after remote-control already active |
| Startup prompt (API round-trip) | ~3-5s | Sonnet: ~3s, Opus: ~5s |
| fleet-state-update | <100ms | flock + JSON write |

The `sleep 5` was a flat 5-second penalty on every start. It fired after the remote-control loop already confirmed the bot is ready. Original comment: "buffer for MCP servers and channels" — but MCP servers connect during the init phase that remote-control waits for. **Removed in #81.**

## Cache State Analysis

### NPX cache (Node MCP servers)

- Cache location: `~/.npm/_npx/`
- Cache size: **752 MB** (12 cached packages)
- Every bot uses `npx -y @modelcontextprotocol/server-github`
- Warm npx startup: **~1.5s** (cache hit, no download)
- Cold npx startup: not measured (would add download time, ~10-30s on Pi network)

### UVX cache (Python MCP servers)

- Cache location: `~/.cache/uv/`
- Cache size: **3.2 GB**
- Not currently used by crog-eng-team bots (only by tl-enterprises)

### Cache risk

If `~/.npm/_npx/` is cleared (npm cache clean, disk cleanup, etc.), all 8 bots will try to `npm install` the same packages simultaneously on restart. On SD card IO at 19 MB/s, this would cause significant contention. The npx cache is load-bearing infrastructure.

## Concurrency Analysis

### Systemd ordering

All bot units declare only `After=network-online.target`. No inter-bot dependencies. When started via `systemctl --user start ari greg craig...` or at boot via `WantedBy=default.target`, **systemd starts them in parallel**.

The journal shows units dispatched ~1s apart (17:36:47–17:36:54), which is systemd's scheduling jitter, not serialization.

### Parallel contention effects

| Scenario | Time to remote-control |
|----------|----------------------|
| 1 bot | ~4-5s |
| 2 bots | ~6-7s (+40%) |
| 8 bots (journal) | 16-29s per bot |

With 4 CPU cores and 8 bots starting simultaneously:
- Load average spikes to 3.7 (from baseline ~1.8)
- Memory jumps ~700 MB in first 6 seconds (2 bots)
- IO wait stays under 1% — **SD card is not the bottleneck for startup**
- CPU is the bottleneck: 8 Claude CLI binary loads + 8 Node processes + 8 npx resolutions competing for 4 cores

### tmux serialization

The first bot to start creates the tmux server. This is ~100ms and not a meaningful bottleneck. Subsequent bots create sessions within the existing server (faster).

## Critical Path

For a single bot restart:

```
systemctl → env load → tmux → claude binary → [MCP + channel init] → RC active → startup prompt → ready
   37ms      8ms      100ms     670ms            3-4s                              3-5s
```

**With `sleep 5` removed**, the dominant cost is now the Claude init phase (MCP + channel handshake, ~3-4s). Total single-bot start drops from ~10-11s to ~5-6s wall clock (excluding startup prompt).

For full fleet startup, the critical path is the **last bot to finish** (ari, 29s). The dominant cost is CPU contention across 8 parallel Claude CLI startups on 4 cores.

## CPU Contention Is the Bottleneck — Not IO

Key finding from issue #81 profiling:

- **IO wait stays under 1%** during full fleet boot — faster SD card hardware would not meaningfully improve cold start
- **CPU is the bottleneck**: load spikes to 3.7 (baseline 1.8) on the 4-core Pi 5 during parallel bot startup
- Single bot: ~5s to remote-control active. 8 parallel bots: 16-29s per bot (3-6x slower)
- The 3-6x slowdown is pure CPU scheduling overhead — all 8 Claude CLI binaries + 8 Node processes competing for 4 cores

Implication: optimizing IO (faster storage, IO scheduler tuning) is the wrong lever. The three meaningful levers are CPU reduction per bot, CPU staggering across bots, and eliminating wasted CPU-idle time.

## Optimization Levers

### Lever 1: Remove `sleep 5` (DONE — #81)

**Impact: saves 5s per bot start, every start.**

The `sleep 5` on line 101 of `start-bot.sh` was a fixed cost after remote-control was already confirmed active. It ran after the wait loop verified initialization — pure dead time. Removed in this PR. The comment now explains why no sleep is needed.

### Lever 2: Stagger fleet startup to reduce CPU contention (TODO)

**Impact: estimated ~10s reduction in tail latency (ari's 29s → ~19s).**

Currently all 8 units start within 7 seconds. With 4 cores, that's 2:1 oversubscription during the CPU-heavy binary load phase. Options:

- Add `ExecStartPre=/bin/sleep $((RANDOM % 5))` to spread the load over a 5s window
- Or batch: start first 4, wait 3s, start next 4 (requires a wrapper script)
- Or add inter-bot `After=` dependencies to serialize in groups of 2

The stagger approach is low-risk: worst case it adds a few seconds to best-case start time while significantly cutting tail latency.

### Lever 3: Move MCP servers from npx to global install (TODO)

**Impact: saves ~1.5s per bot start.**

`npx -y @modelcontextprotocol/server-github` does a cache check + version resolution on every start (~1.5s warm). A global `npm install -g` eliminates this overhead entirely. The compositor would emit `node /path/to/mcp-server-github/index.js` instead of `npx -y ...`. Larger benefit when the npx cache is cold (prevents the cold-cache disaster scenario where all 8 bots simultaneously npm-install the same packages).

### Lever 4: arm64-optimized Claude CLI build (stretch goal — document only)

The Claude CLI binary ships as a universal or x64 binary. A native arm64 build would reduce the ~670ms binary load time. No action item here — this is upstream Anthropic's call. Worth checking if an arm64 build is available when upgrading CLI versions.

## Recommendations (ordered by impact)

### 1. ~~Remove or reduce `sleep 5` in start-bot.sh~~ (DONE — #81)

Removed the fixed post-readiness sleep. See Lever 1 above.

### 2. Stagger fleet startup to reduce CPU contention (saves ~10s on tail latency)

See Lever 2 above. Not yet implemented.

### 3. Pre-warm npx cache in a boot-time oneshot (prevents cold-cache disaster)

Create a systemd unit that runs before bot units:

```ini
[Unit]
Description=Pre-warm NPX cache for MCP servers
Before=ari.service greg.service craig.service ...

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'npx -y @modelcontextprotocol/server-github --help >/dev/null 2>&1'
```

This ensures the npx cache is warm before 8 bots all try to resolve the same package.

### 4. Consider per-bot memory budgets (future-proofing)

At 250-750 MB per claude process + ~70 MB per Node MCP server, the fleet currently uses ~6.2 GB. With 8.2 GB available, headroom is ~2 GB. Adding a 9th bot with Opus model would be tight. Monitor with `free -h` in keepalive.

### 5. Move MCP servers from npx to global install (saves ~1.5s per bot)

See Lever 3 above. Not yet implemented.
