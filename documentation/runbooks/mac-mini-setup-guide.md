# Mac Mini Setup Guide

A boiled-down guide for setting up an Apple Silicon Mac mini as a headless dev machine accessible via SSH + Tailscale, with Homebrew, Node, and Claude Code installed — ready to host a claudlobby bot fleet.

> Assumes: Apple Silicon Mac mini, recent macOS (tested on 26.x Tahoe), and you already have a Tailscale account.

---

## Prerequisites

Before starting, you need **physical/screen access to the mini** for the initial setup. After that, everything is remote.

You'll also want a second machine (laptop) with Tailscale installed and signed into the same Tailnet.

---

## Phase 1: Initial Mini Setup (requires screen access)

### 1.1 Enable SSH

```bash
sudo systemsetup -setremotelogin on

# verify
sudo systemsetup -getremotelogin
```

### 1.2 Configure power for headless operation

The big one — Macs sleep aggressively by default. Run these so the mini stays reachable:

```bash
sudo pmset -a sleep 0          # never sleep
sudo pmset -a disksleep 0      # don't spin down disk
sudo pmset -a womp 1           # wake on network access
sudo pmset -a autorestart 1    # auto-restart after power failure
sudo pmset -a powernap 0       # disable powernap

# verify
pmset -g
```

If any individual command errors (some flags vary by hardware), skip it and continue. `displaysleep` can stay on — it only blanks the screen, doesn't suspend the system.

### 1.3 Set a clean hostname

```bash
sudo scutil --set HostName mac-mini
sudo scutil --set LocalHostName mac-mini
sudo scutil --set ComputerName "Mac Mini"
```

Pick whatever name makes sense for your fleet — `mac-mini`, `fleet-host`, etc.

### 1.4 Auto-login on boot (System Settings, GUI only)

System Settings → Users & Groups → "Automatically log in as" → pick your user.

This ensures user-level services start after a reboot/power loss without anyone needing to type a password. Requires FileVault to be **off** (or the option will be greyed out).

### 1.5 Uninstall the GUI Tailscale app (if present)

If you already had the GUI Tailscale app installed, **uninstall it first** — it's sandboxed by Apple and can't run the Tailscale SSH server. The brew CLI version is what you want for headless.

```bash
# stop and remove any existing GUI app first
osascript -e 'quit app "Tailscale"' 2>/dev/null
sudo rm -rf /Applications/Tailscale.app
```

We'll install the brew version below after Homebrew is ready.

---

## Phase 2: Install Command Line Tools (CLT)

This is required for Homebrew. **The standard `xcode-select --install` flow is unreliable** — it often returns "Can't install the software because it is not currently available from the Software Update server."

### Recommended: Manual download

1. On any machine with a browser, go to: <https://developer.apple.com/download/all/>
2. Sign in with an Apple ID (free account works)
3. Search for **"Command Line Tools"**
4. Download the version that matches (or is older than) your macOS version
   - For macOS 26.x → Command Line Tools for Xcode 26.x
   - You can use older CLT on newer macOS, but **not the reverse**
5. Copy the `.dmg` to the mini if you downloaded it elsewhere:

   ```bash
   # from your laptop
   scp ~/Downloads/Command_Line_Tools_for_Xcode_*.dmg <your-user>@<mini-tailscale-ip>:~/
   ```

### Install on the mini

```bash
# clean up any broken state from a previous failed attempt
sudo rm -rf /Library/Developer/CommandLineTools
sudo xcode-select --reset

# mount and install
hdiutil attach ~/Command_Line_Tools_for_Xcode_*.dmg
ls /Volumes/                    # confirm mount name (usually "Command Line Developer Tools")
sudo installer -pkg "/Volumes/Command Line Developer Tools/Command Line Tools.pkg" -target /
hdiutil detach "/Volumes/Command Line Developer Tools"

# verify
xcode-select -p                  # should print: /Library/Developer/CommandLineTools
clang --version                  # should print version info
```

---

## Phase 3: Install Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# add brew to PATH (Apple Silicon)
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"

# verify
brew doctor
```

---

## Phase 4: Install Tailscale (CLI version) and enable Tailscale SSH

```bash
brew install tailscale
sudo brew services start tailscale

# bring up Tailscale with SSH enabled
sudo tailscale up --ssh --accept-routes

# verify
tailscale status
tailscale ip -4                  # note the 100.x.x.x IP
```

**If you see** `"The Tailscale SSH server does not run in sandboxed Tailscale GUI builds"`:

You still have the GUI app's daemon running. Stop it and restart the brew version:

```bash
sudo brew services stop tailscale
pkill -f tailscaled 2>/dev/null
ps aux | grep -i tailscale | grep -v grep   # confirm nothing's running
sudo brew services start tailscale
sudo tailscale up --ssh --accept-routes
```

In the [Tailscale admin console](https://login.tailscale.com), rename the device to match your hostname for cleaner SSH targets.

---

## Phase 5: Install Node + Claude Code

`jq` is also required here — several `lib/` scripts (dispatch, reconcile-fleet, report-back, creds-check, etc.) depend on it for JSON parsing.

```bash
brew install node jq
npm install -g @anthropic-ai/claude-code

# verify
node --version
jq --version
claude --version

# first run — completes OAuth
claude
```

Claude Code will print an auth URL. Open it on your laptop browser, sign in, paste the code back. Done.

---

## Phase 6: Set Up Remote Access (from your laptop)

Now do everything else from your laptop.

### 6.1 Make sure Tailscale is running on your laptop

If you only have the GUI Tailscale app on your laptop (no CLI), that's fine — SSH will still work, you just won't have MagicDNS hostname resolution. Use the IP directly.

### 6.2 SSH key auth (passwordless login)

```bash
# check for existing key
ls ~/.ssh/id_ed25519.pub

# create one if needed
ssh-keygen -t ed25519
# (press enter through prompts; passphrase optional)

# push key to mini (will ask for mini's password ONE last time)
ssh-copy-id <your-user>@<mini-tailscale-ip>

# test — should be passwordless now
ssh <your-user>@<mini-tailscale-ip>
```

### 6.3 SSH config alias (optional, very nice)

```bash
cat >> ~/.ssh/config <<'EOF'

Host mini
    HostName 100.x.x.x
    User <your-mac-username>
EOF

chmod 600 ~/.ssh/config
```

Replace `100.x.x.x` with the actual Tailscale IP from `tailscale ip -4`. Now `ssh mini` just works.

---

## Daily Workflow

From your laptop:

```bash
ssh mini             # or: ssh <your-user>@<tailscale-ip>
claude               # start Claude Code, do work
```

Anything Claude Code does (installs, git, file edits, scripts) runs on the mini.

---

## Optional but Recommended

### tmux (survive SSH disconnects)

If your SSH session drops (laptop sleeps, wifi blips, you close the lid), any running command dies with it. Fix:

```bash
# install on the mini
brew install tmux

# start a named session
tmux new -s work

# detach: Ctrl+b then d
# reattach from any future SSH session:
tmux attach -t work
```

The claudlobby bots use tmux for session persistence — installing it is required if you plan to run a fleet on this mini.

### mosh (better than SSH for flaky networks)

```bash
brew install mosh
# then from laptop: mosh <your-user>@<ip>
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `xcode-select` install fails with "Can't install... Software Update server" | Skip it. Download CLT manually from developer.apple.com (Phase 2) |
| CLT install errors with version mismatch | Make sure CLT version ≤ macOS version. Older CLT works on newer macOS; not the reverse |
| `tailscale up` errors about "non-default flags" | Re-run with all your existing flags: `sudo tailscale up --ssh --accept-routes` (or `--reset` to start fresh) |
| `tailscale up --ssh` says "sandboxed GUI builds" | GUI Tailscale app is still running. Quit it, uninstall, and use brew version only |
| SSH still asks for password after `ssh-copy-id` | You might be SSH'd into the mini already. Check your prompt — run `exit` and try from your laptop |
| Mini sleeps and becomes unreachable | Re-run the `pmset` commands from Phase 1.2; check with `pmset -g` |
| Hostname doesn't resolve from laptop | Use IP directly, or install Tailscale CLI on laptop for MagicDNS |

---

## What Needs Physical Access (vs. Remote)

**Need screen/keyboard:**

- First-time SSH enable
- Auto-login configuration
- macOS major OS updates (sometimes)
- FileVault unlock on boot
- App Store GUI installs

**Fully remote-able:**

- All `brew install` / `npm install` / `pip install`
- All file editing, git operations, script execution
- Background services (`brew services`, `launchctl`)
- Most `defaults write` system tweaks

---

## Reference: Final State Verification

Run these on the mini to confirm everything's good:

```bash
xcode-select -p             # /Library/Developer/CommandLineTools
brew --version              # Homebrew x.x.x
node --version              # vXX.X.X
jq --version                # jq-X.X.X
claude --version            # claude code version
tailscale status            # shows your tailnet, mini listed with --ssh
tailscale ip -4             # 100.x.x.x
pmset -g | grep -E "sleep|womp|autorestart"   # sleep 0, womp 1, autorestart 1
```

From your laptop:

```bash
ssh mini                    # passwordless, drops you straight in
```

If all of those work, you're set.

---

## Next Steps

Once the mini is reachable and Claude Code is installed, follow the [getting-started guide](../getting-started.md) to clone claudlobby and compose your first bot fleet. Then run `lib/setup-fleet <name>` to enroll the fleet's composed jobs (keepalive, creds-check, etc.), warm the npx cache, and spin up your bots.

- For details on launchd vs systemd vs cron install patterns, see [install-patterns.md](../install-patterns.md).
- For the Pi equivalent of this guide, see [pi-setup-guide.md](./pi-setup-guide.md).
