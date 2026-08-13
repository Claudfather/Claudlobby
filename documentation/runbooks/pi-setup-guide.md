# Raspberry Pi Setup Guide

Complete setup guide for preparing a Raspberry Pi 5 to run a Claude Code bot fleet.

## Base System

### OS

Raspberry Pi OS (Debian Bookworm, 64-bit). Flash with [Raspberry Pi Imager](https://www.raspberrypi.com/software/).

### Initial Config

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Set timezone
sudo timedatectl set-timezone America/New_York  # or your timezone

# Enable SSH (if not already)
sudo systemctl enable --now ssh

# Update Pi firmware (important — newer firmware is more SD/USB-tolerant)
sudo rpi-eeprom-update -a
sudo reboot
```

### SD card stability — strongly recommended

If the Pi will run 24/7 (which a fleet host does), apply this kernel cmdline param **before deploying**:

```bash
# Edit /boot/firmware/cmdline.txt — single line, APPEND to the existing line, do NOT add a newline:
sudo sed -i -e "s|\$| sdhci.debug_quirks2=0x4|" /boot/firmware/cmdline.txt
sudo reboot
```

**What it does:** drops the SD bus from UHS-I SDR104 (200 MHz, 1.8 V) to High-Speed mode (50 MHz, 3.3 V). Pi 5 + some SD cards have a known compatibility class of issues at SDR104 — the kernel logs `mmc0: Card stuck being busy! __mmc_poll_for_busy` and `jbd2/mmcblk0p2 blocked for 120+ seconds` events at random intervals, eventually wedging the system. The throttle eliminates this without needing a card replacement.

**Verify after reboot:**

```bash
cat /sys/kernel/debug/mmc0/ios | grep -E "clock|timing|signal"
# Expected:
#   clock:           50000000 Hz
#   timing spec:     2 (sd high-speed)
#   signal voltage:  0 (3.30 V)
```

Performance halves (~90 → ~45 MB/s sequential) — irrelevant for a fleet workload of logs + state writes. See `library/lessons/raspberry-pi/sdhci-uhs-quirk.md` for the full postmortem.

> **It reduces this class; it does not eliminate it.** The "eliminates" claim above predates
> counter-evidence and is being narrowed here rather than left standing. On a Pi 5 fleet host with
> the quirk **already applied and verified live** on `/proc/cmdline`, the kernel still logged
> `mmc_rescan` / `__mmc_claim_host` stalls on 2026-07-19, 07-21 and 07-29. The 07-19 trace is the
> stark one: successive lines of a *single* kernel stack trace are timestamped 10:02 through 12:28
> — over two hours to emit one trace — and that boot ends there.
>
> So: apply the quirk, it is cheap and it helps. But **do not treat it as a fix for a card that is
> failing.** If stalls persist under it, the remaining move is hardware — move the root filesystem
> to NVMe (the Pi 5 has the slot) rather than tuning the SD bus further. SD cards do not expose
> `life_time`, so wear cannot be read directly to confirm; persistence under the quirk is the
> signal you get.
>
> This matters more once a hardware watchdog is armed (below): a `pid1` blocked in uninterruptible
> D-state on a stalled controller is the failure most likely to trigger a reset, and it is invisible
> to `schedstat`, which measures runqueue wait and therefore records nothing for a D-state process.

**Note:** the legacy `dtparam=sd_overclock=N` and `dtparam=sd_disable_uhs=1` parameters from Pi 1-4 do NOT take effect on Pi 5 (different driver). Use the cmdline approach above.

## Required Software

### Node.js + npm

Required for MCP servers (most use `npx`). Install via [nvm](https://github.com/nvm-sh/nvm) and track the current LTS rather than pinning a NodeSource major version by hand — Node majors go end-of-life roughly every year (20.x already has, as of this writing), so a hardcoded version here goes stale on a fixed clock. nvm also gives you a user-owned install, so global `npm install -g` never needs sudo.

```bash
# Install nvm (check https://github.com/nvm-sh/nvm for the current release tag)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.5/install.sh | bash
source ~/.bashrc   # or restart your shell

# Install and switch to the current LTS (floor: Node >=20)
nvm install --lts
nvm use --lts

# verify
node --version
npm --version
```

### Bun (for Telegram plugin)

```bash
curl -fsSL https://bun.sh/install | bash
echo 'export PATH="$HOME/.bun/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### Python + uv/uvx (for workspace-mcp and other Python MCP servers)

```bash
# Python should already be installed. Install uv:
curl -LsSf https://astral.sh/uv/install.sh | sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### tmux

```bash
sudo apt install -y tmux
```

### jq

Required by several `lib/` scripts (dispatch, reconcile-fleet, report-back, creds-check, keepalive helpers, etc.) for JSON parsing.

```bash
sudo apt install -y jq
```

### Claude Code

```bash
# Install via npm
npm install -g @anthropic-ai/claude-code

# Authenticate
claude auth login
# Complete OAuth flow in browser (use SSH tunnel if headless)
```

### GitHub CLI

```bash
# Install (current official instructions — /etc/apt/keyrings/, not the older /usr/share/keyrings/ path)
sudo mkdir -p -m 755 /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update && sudo apt install -y gh

# Authenticate
gh auth login
```

## Optional CLIs

Install these based on what your bots need to manage.

### Vercel CLI

```bash
npm install -g vercel
vercel login
# Complete browser auth via SSH tunnel
```

### Railway CLI

```bash
npm install -g @railway/cli
railway login
# Complete browser auth via SSH tunnel
```

### Neon CLI

```bash
npm install -g neon
neon auth
# Complete browser auth via SSH tunnel
```

### DigitalOcean CLI (doctl)

```bash
# Install the latest doctl release (check https://github.com/digitalocean/doctl/releases for the current version)
DOCTL_VERSION=$(curl -fsSL https://api.github.com/repos/digitalocean/doctl/releases/latest | grep -m1 '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/')
wget "https://github.com/digitalocean/doctl/releases/download/v${DOCTL_VERSION}/doctl-${DOCTL_VERSION}-linux-arm64.tar.gz"
tar xf "doctl-${DOCTL_VERSION}-linux-arm64.tar.gz"
sudo mv doctl /usr/local/bin/
doctl auth init
# Paste API token from DO dashboard
```

### dbt Core (for data teams)

```bash
pip install dbt-snowflake  # or dbt-postgres, dbt-bigquery, etc.
```

### Tailscale (remote access)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# Authenticate via URL
```

## MCP Server Dependencies

Most MCP servers install on-demand via `npx` or `uvx`. Some need pre-installation:

### workspace-mcp (Gmail / Google Calendar)

```bash
# Installs on-demand via uvx, but needs a Google Cloud OAuth client:
# 1. Go to console.cloud.google.com
# 2. Create project → APIs & Services → Credentials → OAuth Client ID
# 3. Type: Desktop app
# 4. Copy Client ID and Client Secret
# 5. First run triggers OAuth flow — open URL in browser via SSH tunnel
```

### Notion MCP

```bash
# Installs on-demand via npx. Needs:
# 1. Go to notion.so/profile/integrations
# 2. Create integration → copy token (ntn_...)
# 3. Share target pages/databases with the integration
```

### Home Assistant MCP

```bash
# hass-mcp installs via uvx. Needs:
# 1. HA long-lived access token from HA dashboard → Profile → Security
# 2. HA must be accessible from Pi (usually http://localhost:8123)
```

### NPX Cache — Critical Infrastructure

The `~/.npm/_npx/` directory caches downloaded MCP server packages. **Do not clear this cache** unless you understand the consequences:

- With warm cache: MCP servers start in ~1.5s (local resolution, no network)
- With cold cache: each package takes 30-60s to download on Pi hardware
- With 8 bots sharing the same packages, a cold cache causes catastrophic IO contention (SD card at 19 MB/s)

**Protected operations — do NOT run on a fleet host:**

```bash
npm cache clean --force    # clears ~/.npm/_npx/ among other things
rm -rf ~/.npm/_npx/        # instant fleet-wide cold start regression
```

**Health check:**

```bash
lib/check-npx-cache.sh --fleet <name>   # verify all MCP packages are cached
claudlobby warm-cache                    # pre-download any missing packages
```

**Recovery if cache is cleared:**

```bash
# Stop the fleet keepalive timer FIRST — otherwise it revives the stopped
# bots within 60s, mid-warm
systemctl --user stop <service_prefix>.keepalive.timer

# Then stop all bots to avoid contention
systemctl --user stop bot1 bot2 bot3 ...   # all bots in the fleet

# Re-warm the cache serially (not in parallel)
claudlobby --fleet <name> warm-cache

# Restart fleet, then re-arm keepalive
systemctl --user start bot1 bot2 bot3 ...  # same list
systemctl --user start <service_prefix>.keepalive.timer
```

> **If this procedure is interrupted** (Ctrl-C, dropped SSH session) after the
> first step, the fleet is left with keepalive DISARMED and nothing re-arms it.
> Before walking away, run
> `systemctl --user start <service_prefix>.keepalive.timer`.

The cache typically occupies 500-800 MB. This is expected and should not be reclaimed.

## SSH Tunnel for OAuth Flows

Headless Pi can't open browsers. Use SSH tunnels for OAuth:

```bash
# On your laptop — forward Pi's OAuth callback port
ssh -L 8000:localhost:8000 your-pi-host -N

# Then open the OAuth URL in your laptop's browser
# The callback redirects to localhost:8000 which tunnels to the Pi
```

For multiple Gmail accounts, each needs a unique port:
```bash
# Account 1: port 8000 (default)
# Account 2: port 8001
# Account 3: port 8002
# Set WORKSPACE_MCP_PORT in .mcp.json for each
```

## Security Hardening

```bash
# Lock down secret files
chmod 600 ~/claudlobby/*/.env ~/claudlobby/*/.mcp.json

# Disable SSH password auth
sudo sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh

# Firewall
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from YOUR_LAN_SUBNET/24 to any port 22
sudo ufw enable

# Brute-force protection
sudo apt install -y fail2ban
echo -e "[sshd]\nenabled = true\nbackend = systemd" | sudo tee /etc/fail2ban/jail.local
sudo systemctl enable --now fail2ban

# Disable unnecessary services
sudo systemctl disable --now cups cups-browsed ModemManager
```

## Swap (Recommended for 4+ Bots)

**Set `CONF_MAXSWAP` as well as `CONF_SWAPSIZE`, or you silently get 2 GB.**
`/sbin/dphys-swapfile` hardcodes `CONF_MAXSWAP=2048` and clamps `CONF_SWAPSIZE`
down to it (`restricting to config limit: 2048MBytes`, easy to miss in the
output). Setting only `CONF_SWAPSIZE=4096` leaves you with half what you asked
for and no error.

```bash
sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=4096/' /etc/dphys-swapfile
# raise the ceiling too — add the line if it is absent, it usually is
grep -q '^CONF_MAXSWAP=' /etc/dphys-swapfile \
  && sudo sed -i 's/^CONF_MAXSWAP=.*/CONF_MAXSWAP=4096/' /etc/dphys-swapfile \
  || echo 'CONF_MAXSWAP=4096' | sudo tee -a /etc/dphys-swapfile

sudo dphys-swapfile swapoff && sudo dphys-swapfile setup && sudo dphys-swapfile swapon
```

**Verify the number, do not trust the config:**

```bash
cat /proc/swaps        # Size column is in KB — 4194288 ≈ 4.0 GB, 2097148 ≈ 2.0 GB
free -h | grep Swap
```

**Before `swapoff`, check you have RAM to absorb it.** `swapoff` pages everything
back into memory; on a host already under pressure that can OOM. `free -m` should
show `available` comfortably above the in-use swap figure.

**The tradeoff, stated honestly:** on a stock Pi the swapfile lives on the SD
card, so more swap means more SD writes — and SD stress is itself a hang suspect
(see *SD card stability* above). Swap that absorbs a spike still beats a livelock,
but the two mitigations pull against each other. If the host has NVMe, put the
swapfile there and the tension disappears.

**Sizing signal:** if `/proc/swaps` shows the Used column near Size on a
199 MB default, the host is already swapping hard and this is overdue rather
than precautionary.

## Hardware watchdog (strongly recommended for unattended hosts)

A Pi running a fleet 24/7 will eventually hard-hang. Without a watchdog it stays
hung until a human power-cycles it — an overnight hang costs the whole night.
The BCM2835 watchdog is present on every Pi but ships **inactive**, and systemd
defaults `RuntimeWatchdogSec=off`, so out of the box nothing is watching.

```bash
sudo mkdir -p /etc/systemd/system.conf.d
sudo tee /etc/systemd/system.conf.d/10-claudlobby-watchdog.conf >/dev/null <<'EOF'
[Manager]
RuntimeWatchdogSec=14
RebootWatchdogSec=10min
EOF
sudo systemctl daemon-reexec
```

**Use 14s, not the 30s that circulates in most guides.** Check the ceiling first:

```bash
cat /sys/class/watchdog/watchdog0/timeout    # BCM2835: 15
```

systemd pings the watchdog at **half** the configured interval. Set a value above
the hardware ceiling and the driver clamps the timeout to its own maximum while
systemd keeps pinging on the longer schedule you asked for — so the device can
expire between pings and reset a perfectly healthy host. Stay strictly under the
ceiling; 14 on a 15s device gives a 7s ping.

**Verify it is actually armed** — enrolling is not the same as running:

```bash
systemctl show -p RuntimeWatchdogUSec          # RuntimeWatchdogUSec=14s
cat /sys/class/watchdog/watchdog0/state        # active
cat /sys/class/watchdog/watchdog0/timeleft     # counts down = being petted
journalctl -b 0 | grep -i 'hardware watchdog'  # "Watchdog running with a hardware timeout of 14s"
```

`RebootWatchdogSec` is separate: it guards the *shutdown* path so a reboot that
wedges still completes. 10min is the sensible default.

## Host jobs — enrolling is a separate step from declaring

`system.yaml` `host.jobs` *declares* the host-level timers (`claude-update`,
`notify-behind`, `disk-monitor`, `fleet-memory-check`, `orphan-browser-reaper`,
`host-health-check`). `claudlobby host-timers` *composes* the units into
`runtime/_host/timers/`. Neither step enrolls them — `lib/setup-system` does.

A host that was set up before a job was added keeps running happily with that job
composed-but-never-enrolled, and nothing surfaces it. Audit periodically:

```bash
ls runtime/_host/timers/*.timer | xargs -n1 basename | sed 's/.timer//' | sort > /tmp/composed
systemctl --user list-timers --all | grep -oE 'claudlobby-[a-z-]+' | sort -u > /tmp/enrolled
comm -23 /tmp/composed /tmp/enrolled     # composed but NOT enrolled
```

Then enroll a specific one:

```bash
TIMER_DIR=$CLAUDLOBBY_ROOT/runtime/_host/timers \
UNIT_NAME=claudlobby-<job> \
  lib/install_fleet_timer.sh <job>
```

**Always start the service once by hand after enrolling.** A timer can enroll
cleanly and still fail every fire — a non-executable `ExecStart` gives
`status=203/EXEC` and only shows up in the journal:

```bash
systemctl --user start claudlobby-<job>.service
systemctl --user show claudlobby-<job>.service -p Result -p ExecMainStatus
journalctl --user -u claudlobby-<job>.service -n 20
```

## Installing the fleet (pick one pattern)

Two supported patterns on Linux. Pick the one that fits — both produce a working fleet, neither blocks the other later. Full reference: [install-patterns.md](../install-patterns.md).

### Pattern A — cron + tmux (simplest, what most Pi setups use)

```bash
# Compose the fleet first
claudlobby --fleet <name> generate

# Install cron entries (per-bot keepalive staggered, log rotation, disk monitor, daily creds-check)
lib/install-cron.sh --fleet <name>

# Inspect without writing
lib/install-cron.sh --fleet <name> --dry-run
```

`install-cron.sh` writes a managed block bracketed by `# BEGIN claudlobby:<fleet>` / `# END claudlobby:<fleet>` markers. Re-run it to update; lines outside the markers are preserved.

Bots themselves are still tmux sessions; bring them up at boot with one `@reboot` entry per bot:

```crontab
@reboot sleep 60 && /path/to/claudlobby/lib/start-bot.sh /path/to/runtime/bots/<bot>
```

### Pattern B — systemd user services (modern, self-restarting)

```bash
loginctl enable-linger $USER     # one-time, so user services persist past logout
claudlobby --fleet <name> generate

# Per bot
lib/install-bot-systemd.sh local/<name>/runtime/bots/<bot>

# Fleet-wide timers
lib/install-keepalive-systemd.sh <name>
lib/install-creds-check-systemd.sh
```

Each bot becomes a `systemd --user` unit with `Restart=on-failure`. View with `systemctl --user list-timers` and `journalctl --user -u <name> -f`.

### Generic helpers used by both patterns

- `lib/keepalive.sh <bot-dir>` — restart a dead session, nudge idle panes
- `lib/log-rotate.sh [--keep N] <log>...` — tail each log to last N lines
- `lib/disk-monitor.sh [--threshold N]` — warn if disk usage > threshold
- `lib/bot-sweep-cron.sh <bot> <trigger>` — periodic dispatch (e.g. `bot-sweep-cron.sh assistant "briefing morning"`)
