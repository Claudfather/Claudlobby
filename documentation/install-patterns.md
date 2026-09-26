# Install Patterns

claudlobby's compositor generates the host-side service definitions for each bot — `<service_prefix>.<bot>.plist` (launchd) and `<service_prefix>.<bot>.service` (systemd) — but you choose how to register and run them. There are two supported patterns. Pick the one that fits your host.

## When to use which

| Pattern | Host | Trade-offs |
|---|---|---|
| **launchd LaunchAgents** (macOS) | Mac mini / MacBook | Native, integrated with macOS sleep/wake; per-bot `KeepAlive` plus a fleet-wide 60s keepalive timer. Recommended on Mac. |
| **systemd user services** (Linux) | Raspberry Pi / Linux server | Native, self-restarting (`Restart=on-failure`), structured logging via `journalctl`. Recommended on Linux when you want "real services." Requires `loginctl enable-linger $USER` for persistence past login. |

You can mix patterns across a fleet — e.g., bots on a Pi via systemd, bots on a Mac via launchd. The `lib/keepalive.sh` core is identical in both.

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

See [mac-mini-setup-guide.md](./runbooks/mac-mini-setup-guide.md) for full host setup (SSH, Homebrew, Tailscale, Claude Code).

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
lib/install-code-audit-sweep-systemd.sh <fleet>  # nightly code-audit sweep (only if fleet.sweep set)
```

Units land in `~/.config/systemd/user/`. View with `systemctl --user list-timers`, follow logs with `journalctl --user -u <name> -f`.

See [pi-setup-guide.md](./runbooks/pi-setup-guide.md) for full host setup.

## Cron + tmux (retired)

`lib/install-cron.sh` was a third supervision plane that neither first-class host used; it was removed. Supervise with systemd user units (Pattern 2) or launchd (Pattern 1) — `lib/setup-fleet <fleet>` enrolls every composed job on either.

## Generic helpers (used by both patterns)

These ship with claudlobby and run under the composed fleet jobs; they're useful standalone too:

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
- "I'm on a Pi, I want the simplest thing that works." → systemd — `lib/setup-fleet <fleet>` enrolls every composed job in one call.
