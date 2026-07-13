# Install Patterns

claudlobby's compositor generates the host-side service definitions for each bot — `<service_prefix>.<bot>.plist` (launchd) and `<service_prefix>.<bot>.service` (systemd) — but you choose how to register and run them. There are three supported patterns. Pick the one that fits your host.

## When to use which

| Pattern | Host | Trade-offs |
|---|---|---|
| **launchd LaunchAgents** (macOS) | Mac mini / MacBook | Native, integrated with macOS sleep/wake; per-bot `KeepAlive` plus a fleet-wide 60s keepalive timer. Recommended on Mac. |
| **systemd user services** (Linux) | Raspberry Pi / Linux server | Native, self-restarting (`Restart=on-failure`), structured logging via `journalctl`. Recommended on Linux when you want "real services." Requires `loginctl enable-linger $USER` for persistence past login. |
| **cron + tmux** (Linux or macOS) | Raspberry Pi / Linux / Mac | Simplest mental model, fewest moving parts. No service supervisor — bots are tmux sessions kept alive by a cron-driven `keepalive.sh`. Trade-off: 30-min keepalive granularity by default vs. 60s for systemd/launchd. |

You can mix patterns across a fleet — e.g., bots on a Pi via cron, bots on a Mac via launchd. The `lib/keepalive.sh` core is identical in all three.

## Pattern 1 — macOS launchd

```bash
# One-time host setup (idempotent, 10 phases incl. channel-plugin approvals + host-job enrollment)
lib/setup-system

# Per-fleet apply+enroll: composed jobs + bots + reconcile (idempotent,
# skips already-healthy bots). Run `claudlobby generate` first.
lib/setup-fleet <fleet>

# Or piecewise:
lib/install-bot.sh local/<fleet>/runtime/bots/<bot>      # one bot at a time
lib/install_fleet_timer_launchd.sh keepalive <fleet>     # any composed fleet timer by name
lib/install_fleet_timer_launchd.sh creds-check <fleet>
lib/install-code-audit-sweep.sh <fleet>                  # nightly code-audit sweep (only if fleet.sweep set)
```

Each bot becomes `com.claudlobby.<fleet>.<bot>` in `~/Library/LaunchAgents/`. View with `launchctl list | grep claudlobby` and tail logs at `lib/logs/<bot>.{out,err}.log`.

See [mac-mini-setup-guide.md](./mac-mini-setup-guide.md) for full host setup (SSH, Homebrew, Tailscale, Claude Code).

## Pattern 2 — Linux systemd

```bash
# One-time, per-host (packages, linger, host-job enrollment — idempotent)
lib/setup-system

# Per-fleet apply+enroll: composed jobs + bots + reconcile (idempotent,
# skips already-healthy bots). Run `claudlobby generate` first.
lib/setup-fleet <fleet>

# Or piecewise, per bot / per timer:
lib/install-bot-systemd.sh local/<fleet>/runtime/bots/<bot>
lib/install_fleet_timer.sh keepalive <fleet>     # any composed fleet timer by name
lib/install-creds-check-systemd.sh <fleet>       # per-timer thin wrappers still work
lib/install-code-audit-sweep-systemd.sh <fleet>  # nightly code-audit sweep (only if fleet.sweep set)
```

Units land in `~/.config/systemd/user/`. View with `systemctl --user list-timers`, follow logs with `journalctl --user -u <name> -f`.

See [pi-setup-guide.md](./pi-setup-guide.md) for full host setup.

## Pattern 3 — cron + tmux

```bash
# One-time, per fleet — installs everything as crontab entries:
#   - Per-bot keepalive (every 30 min, staggered)
#   - Weekly log rotation (Sunday 03:00) — lib/-level logs only, see note below
#   - Fleet pulse (every 5 min by default, derived from OBSERVABILITY_PULSE_INTERVAL)
#   - Daily creds-check (09:00) — pass --no-creds-check to skip
lib/install-cron.sh --fleet <name>

# Inspect what would change without writing:
lib/install-cron.sh --fleet <name> --dry-run
```

Bots are still tmux sessions — start them once with `lib/start-bot.sh <bot-dir>` (or via a `@reboot` cron entry — see below). Cron's `keepalive.sh` ticks restart any session that died.

`install-cron.sh` writes a single managed block bracketed by:

```
# BEGIN claudlobby:<fleet>
…
# END claudlobby:<fleet>
```

Anything outside that block is preserved on re-run. To remove the block entirely, edit `crontab -e` and delete it.

**Scope notes:**
- The weekly log rotation only targets five `lib/`-level log files (`keepalive.log`, `keepalive-all.log`, `bot-sweep-cron.log`, `disk-monitor.log`, `creds-check.log`) — it does not rotate per-bot logs (`runtime/bots/<bot>/keepalive.log`, `<bot>/logs/startup.log`, `<bot>/data/events/*.jsonl`). For the same comprehensive per-bot coverage Patterns 1/2 get automatically from their composed `log-rotation` timer, add a weekly cron line for `lib/log-rotate-fleet.sh --fleet <name>` yourself.
- Disk-usage monitoring and the other host-wide jobs (`disk-monitor`, `claude-update`, `notify-behind`, `fleet-memory-check`) are **not** installed by `install-cron.sh` — they're host jobs enrolled once per host by `lib/setup-system` (`disk-monitor` runs daily at 05:00), independent of which bot-supervision pattern you use. Run `lib/setup-system` on this host if you haven't already.

### Adding bot startup at reboot (cron)

Cron does not auto-start tmux sessions on boot. To bring bots up after a Pi reboot:

```bash
# Add to crontab manually (one entry per bot):
@reboot sleep 60 && /path/to/lib/start-bot.sh /path/to/runtime/bots/<bot>
```

The `sleep 60` gives the network and Tailscale time to come up before Claude Code tries to authenticate.

## Generic helpers (used by all patterns)

These ship with claudlobby and are referenced by the cron block install above; they're useful standalone too:

- `lib/keepalive.sh <bot-dir>` — restart a bot's service if its tmux session is dead; nudge an idle pane with `Enter`.
- `lib/keepalive-all.sh [<fleet-name> | <abs-runtime-bots-dir>]` — iterate every declared bot in the fleet and run `keepalive.sh` per bot (composed units pass the fleet name; an absolute path selects a bots dir directly).
- `lib/log-rotate.sh [--keep N] <log-path>...` — tail each log to last N lines (default 500). Cheap, idempotent.
- `lib/log-rotate-fleet.sh [--keep N] [--fleet <name>]` — discovers and rotates all bot logs (walks `runtime/bots/<bot>/` per fleet, plus `lib/logs/`) using `log-rotate.sh` under the hood; without `--fleet`, rotates every fleet under `local/`. This is what Patterns 1/2's composed `log-rotation` timer runs — Pattern 3's cron block does not call it by default (see Scope notes above).
- `lib/disk-monitor.sh [--threshold N] [--mount /]` — FLEET ALERT when disk usage exceeds N% (default 90, mount `/`); reports per-bot data sizes. Runs daily as the `disk-monitor` host job.
- `lib/bot-sweep-cron.sh <bot-name> <dispatch-text>` — send a trigger string into a bot's tmux pane (skips if pane is busy). Use to wire periodic actions like `briefing morning` or `SWEEP DEEP`.
- `lib/creds-check.sh` — probe fleet-critical credentials, alert Telegram on state transitions.

## Picking your pattern, in plain terms

- "I'm on a Mac and following the runbook." → launchd.
- "I'm on a fresh Linux server and want self-healing services." → systemd.
- "I'm on a Pi, I want the simplest thing that works, and 30-min keepalive granularity is fine." → cron.
- "I want to run a few bots quickly without committing to a service supervisor." → cron.
