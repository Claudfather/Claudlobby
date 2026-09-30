# claudlobby

A **compositor** for Claude Code agent fleets. One repo, one `fleet.yaml`, N bots — composed at runtime from a library of personas, skills, MCP fragments, guardrails, and protocols.

```
library/  ← composable building blocks (sources of truth, in git)
voices/   ← personality overlays
fleet.yaml ← the recipe (which bots, which pieces, which Telegram groups)
runtime/  ← what gets generated (gitignored)
```

Runs anywhere Claude Code does: Mac mini, Linux box, Raspberry Pi 5.

## Why a compositor

The early "one directory per bot" pattern duplicated the same persona scaffolding, MCP boilerplate, lifecycle protocol, and guardrail rules across every bot. Adding a 9th bot meant copy-pasting a CLAUDE.md and editing it. Updating a guardrail meant editing 8 files.

claudlobby flips that: every cross-cutting concern (a guardrail, a protocol, an MCP server config, a skill, a persona) lives **once** in `library/`. `fleet.yaml` declares which bot uses which pieces. `claudlobby generate` produces self-contained bot directories under `runtime/bots/<name>/` that Claude Code can run directly.

Add a 9th bot? Add 10 lines to `fleet.yaml`. Update a guardrail? Edit one file in `library/guardrails/`. Re-run `claudlobby generate`. Done.

## What it changes on your machine

The `/setup` skill runs three steps from your checkout: `lib/setup-system` once per host, `claudlobby generate`, and `lib/setup-fleet` once per fleet (`.claude/skills/setup/SKILL.md`). `lib/setup-system --dry-run` changes nothing and prints each change it would make, except the `apt-get update` it runs first on Linux. Each row names the file that makes the change.

| Change | Where | Made by |
|---|---|---|
| **With `sudo`, Linux:** `apt-get update` on every run; `apt-get install` of tmux, jq and curl when missing; the GitHub CLI from GitHub's apt repository (a keyring and a source list); Node 20 from NodeSource (`curl … \| sudo -E bash -`) when node is missing or older than 18 | `/etc/apt/`, system packages | `lib/setup-system` (`phase_packages`, `phase_node`) |
| **macOS:** Homebrew's installer when `brew` is missing, then `brew install` of tmux, jq, gh and node when missing | Homebrew | `lib/setup-system` (`phase_packages`, `phase_node`) |
| Claude Code, when `claude` is missing: `npm install -g @anthropic-ai/claude-code` | npm's global prefix | `lib/setup-system` (`phase_claude`) |
| Claude Code plugins, at user level: `telegram@claude-plugins-official`, and `claudna@Claudfather` from the `Claudfather/clauDNA` marketplace. Each bot start also installs or updates plugins | `~/.claude/plugins/` | `lib/setup-system` (`phase_plugins`); `plugin_ensure` in `lib/lib-common.sh`, run by `lib/start-bot.sh` |
| **With `sudo`, on every run: managed settings.** Sets `allowedChannelPlugins` to the official Telegram plugin and Claudfather's fork, keeping any other keys. Managed settings bind every Claude Code session on the host, yours included, and this key replaces Claude Code's built-in list of approved channel plugins | `/etc/claude-code/managed-settings.json`; macOS: `/Library/Application Support/ClaudeCode/managed-settings.json` | `lib/setup-system` (`phase_managed_settings`) |
| **Your own Claude Code settings.** Each bot start sets `skipAutoPermissionPrompt` and `skipDangerousModePermissionPrompt` to `true`, pre-accepting Claude Code's consent prompts for auto and bypass-permissions mode in your sessions as well as the bots'. If the file is not valid JSON, it is replaced by one holding only those two keys | `~/.claude/settings.json` (under `$CLAUDE_CONFIG_DIR` if set) | `lib/start-bot.sh` (the headless consent block) |
| Each bot's Telegram access list (see [Safety model](#safety-model)) | `~/.claude/channels/telegram-<handle>/access.json` | `claudlobby generate` (`claudlobby/composer.py`) |
| **With `sudo`, Linux, when linger is off:** `loginctl enable-linger $USER`, so your user units run at boot and after you log out | systemd-logind | `lib/setup-system` (`phase_systemd`) |
| Units that run as you: a service per bot, each fleet's timers, and the host's `claudlobby-*` timers and services | `~/.config/systemd/user/`; macOS: `~/Library/LaunchAgents/` | `lib/install-bot-systemd.sh`, `lib/install-bot.sh`, `lib/install_fleet_timer.sh`, `lib/install_fleet_timer_launchd.sh` and `lib/install-host-service-systemd.sh`, run by `lib/setup-system` and `lib/setup-fleet` |
| MCP server packages, fetched before the first boot | package caches in your home directory | `claudlobby warm-cache`, run by `lib/setup-fleet` |

Setup writes nothing to `~/.config/claudlobby/`. A `system.yaml` there is your own override of the host jobs (`claudlobby/config.py`), and `github-app.conf` (mode 600) appears only if you run `lib/setup-github-app.sh` for a GitHub App identity.

**After setup, jobs keep running.** `claudlobby/system.yaml` lists them (`host.jobs`, `defaults.jobs`) and whether each is on, and `claudlobby doctor --switches` prints the same for your host. Three act beyond the fleet:

- `claude-update`, daily: `npm install -g @anthropic-ai/claude-code@latest`, with `sudo` when the `claude` it updates is a root-owned install under `/usr` (`lib/update-claude-code.sh`).
- `reload-fleet`, daily: `claude plugin update`, `claudlobby generate`, then `lib/setup-fleet --jobs-only`, which enrolls any timer that newly composes (`lib/reload-fleet.sh`).
- `orphan-browser-reaper`, daily: kills browser processes whose parent has exited (`lib/orphan-browser-reaper.sh`).

The two jobs that pull new source into your checkouts, `pull-root` and `update-siblings`, stay off unless you turn them on. The plane serves a read-only web view on `127.0.0.1:8899` (`claudlobby/commands/_parsers.py`) and takes events on a Unix socket, `state/plane/ingest.sock` (`claudlobby/plane/daemon.py`).

**Inside the checkout**, all gitignored (`.gitignore`): `.venv/`, `.env`, `state/` (the plane database `state/plane/plane.db`, fleet state, logs), and the generated bot directories under `runtime/` or `local/<fleet>/`.

**Where secrets live**

- `.env` files, read in this order with the most specific winning: `~/.env`, `<checkout>/.env`, `local/<fleet>/.env`, `<bot dir>/.env` (`lib/env-tiers.sh`). `/setup` writes the Telegram token, and a GitHub token if you give one, to `<checkout>/.env` (`.claude/skills/setup/SKILL.md`, Step 3). `claudlobby env-register` shows which file each value comes from.
- Claude Code's login, in its config directory. Every bot on the `default` account uses yours (`accounts` in `fleet.yaml.seed`).
- Your `gh` login. Every bot can use it, since every bot runs as you.
- With a GitHub App identity: `~/.config/claudlobby/github-app.conf` and the App's private key (`documentation/runbooks/github-app-setup.md`).

## Safety model

**A bot is a Claude Code session that can run shell commands as you.** Trust it as you would a person with a shell on your account.

**What a bot can do**

- **Run shell commands without asking.** The default permission mode is `acceptEdits` (`claudlobby/composer.py`), but an allowed tool runs without a prompt. `allow_all: true` in `library/expertise/software-engineering.md` allows every tool, bare `Bash` included (`ALL_TOOLS` in `claudlobby/composer.py`), and the seed bot's `library/expertise/setup-assistant.md` allows `Bash` as well. `fleet.yaml` can also set `permission_mode: auto` or `dangerously_skip_permissions: true` (`fleet.yaml.example`).
- **Act as your user.** Every bot's unit runs under your account (see the units above), so the operating system lets a bot read and change anything you can, other bots' files included.
- **Use `sudo` wherever your account needs no password for it.** No composed rule denies `sudo`, so on a host where `sudo -n true` succeeds, every bot has root.
- **Use every credential on the host.** Each bot's session exports the `.env` files above (`lib/start-bot.sh`), and it has your Claude Code and `gh` logins.
- **Take instructions over Telegram.** Direct messages are accepted only from `human_telegram_id`. In a fleet's group, `generate` leaves `allowFrom` empty (`claudlobby/composer.py`), which the Telegram plugin reads as every member of the group (the plugin's `server.ts`).
- **Follow your own Claude Code settings.** Bots on the `default` account read your `~/.claude/settings.json`, so an allow rule you add there, such as a bare `Bash`, applies to every bot.

**What bounds them**

- **Deny rules**, in each bot's `.claude/settings.local.json`. Every bot is denied Read and Edit of its fleet siblings' directories (`claudlobby/composer.py`), plus any `tools.deny` in `fleet.yaml`, and `isolation.shared_config: true` adds transcripts, credentials and the `.env` files (`claudlobby/isolation.py`). **These are not an operating-system boundary.** A deny rule gates Claude Code's own tool calls: the Read tool, and a shell command given a literal path. It does not stop an interpreter that opens a file itself, a path written through a variable, or any script, hook, timer or MCP server (`claudlobby/isolation.py`). Splitting bots off your account is tracked in #1606.
- **Hooks on every bot** (`defaults.hooks` in `claudlobby/system.yaml`). `lib/gh-mention-guard.sh` rewrites `@` mentions out of GitHub-bound text, and `lib/vault-git-guard.sh` refuses git state rewrites (checkout, rebase, reset and the like) inside a Claudron vault. The rest record activity.
- **Guardrails are instructions, not enforcement.** The seed's `no-push-main`, `no-destructive-git`, `pii-protection` and `no-fabrication` (`fleet.yaml.seed`) are text in each bot's `CLAUDE.md`, and none carries a deny rule (`library/guardrails/`). Only branch protection on your repository actually blocks a push to `main`.
- **The sandbox is off in the seed** (`sandbox: enabled: false` in `fleet.yaml.seed`). The `sandbox:` block turns Claude Code's sandbox on (`documentation/fleet-yaml-schema.md`).
- **Your tokens' scope.** A bot can do on GitHub, Telegram or any other service what the token it holds allows.

To tighten it: run the fleet under a dedicated account that has no `sudo`, keep each fleet's Telegram group to people you would give a shell, give bots fine-grained tokens scoped to the fleet's repositories, and turn the sandbox on.

## Quick start

**You need:** An Anthropic account (Claude Max, Team, or Enterprise — or an `ANTHROPIC_API_KEY`), [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed, and a Telegram account.

**Guided setup (recommended):** Clone the repo, install, and let claudfather walk you through it:

```bash
git clone https://github.com/Claudfather/Claudlobby.git
cd Claudlobby
python3 -m venv .venv               # required — see note below
source .venv/bin/activate
python3 -m pip install -e '.[plane-ui]'
claude                              # opens Claude Code in the repo
```

Then type `/setup` — it checks your host, collects credentials, and spins up claudfather (the built-in setup assistant) on Telegram. Continue setup from your phone.

> **Why `[plane-ui]`.** The operator plane (`claudlobby plane view`) is enrolled by default
> and needs FastAPI + uvicorn — two pure-Python wheels. Install without the extra and the
> compositor deliberately composes no unit for it, so nothing crash-loops; `claudlobby doctor
> --switches` then shows `plane-view` off with this pip line as its arm. Everything else works
> either way.
>
> **Why the venv is not optional.** Homebrew python (macOS) and Debian/Raspberry Pi system
> python are both marked externally-managed under [PEP 668](https://peps.python.org/pep-0668/),
> so a bare `pip install -e .` is *refused* on the two hosts this project targets first. Note
> `python3 -m pip`, not `pip` — Homebrew ships `pip3` only, so plain `pip` is not a command.
>
> Prefer not to manage it yourself? `lib/setup-system` creates the venv, installs claudlobby,
> and checks every other host prerequisite in one idempotent pass (`--dry-run` to preview).
> **It will prompt for `sudo`** — its managed-settings phase writes the root-owned
> `/Library/Application Support/ClaudeCode/managed-settings.json` (and on Linux it installs
> packages). Use `--dry-run` first if you want to see everything it would touch.

**Manual setup:**

```bash
git clone https://github.com/Claudfather/Claudlobby.git
cd Claudlobby
python3 -m venv .venv && source .venv/bin/activate && python3 -m pip install -e '.[plane-ui]'

cp fleet.yaml.seed fleet.yaml       # one bot (claudfather) — the blessed first run
cp .env.seed.example .env           # fill in your Telegram token + GitHub PAT
$EDITOR fleet.yaml                  # replace every REPLACE_ME (validate enforces this)

claudlobby validate && claudlobby generate
lib/setup-fleet                     # enrolls timers + starts every declared bot
```

Start from `fleet.yaml.seed` (one bot, ~60 lines). `fleet.yaml.example` is the **reference** —
a full multi-bot manifest documenting every available field — not a starting point.

The generated `runtime/bots/<bot>/` is everything Claude Code needs — `CLAUDE.md`, `.mcp.json`, `bot.conf`, `.claude/skills/` symlinks, plus a systemd `<bot>.service` and a launchd `<bot>.plist`. Pick the right one for your host.

See [`documentation/getting-started.md`](documentation/getting-started.md) for the full zero-to-running walkthrough.

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│  library/                  ← single source of truth (git)   │
│  ├── expertise/            ← orchestration, engineering, …   │
│  ├── skills/               ← dispatch, lifecycle, prs, …    │
│  ├── mcp/                  ← github.json, notion.json, …    │
│  ├── guardrails/           ← no-push-main, pii-protection,…│
│  └── protocols/            ← report-back, dispatch, …      │
└────────────────────────┬───────────────────────────────────┘
                         │
                         ▼
                    fleet.yaml ← which bots, which pieces
                         │
                         ▼ claudlobby generate
                         │
┌────────────────────────────────────────────────────────────┐
│  runtime/bots/<name>/     ← gitignored, regeneratable       │
│  ├── CLAUDE.md            ← persona + voice + roster +      │
│  │                          protocols + guardrails          │
│  ├── .mcp.json            ← merged from library/mcp/        │
│  ├── bot.conf             ← env exports for lib/start-bot.sh│
│  ├── .claude/skills/      ← symlinks → library/skills/      │
│  ├── <bot>.service        ← systemd (Linux)                 │
│  └── <bot>.plist          ← launchd (macOS)                 │
└────────────────────────┬───────────────────────────────────┘
                         │
                         ▼
                ┌────────────────┐
                │ Claude Code    │ ← started by lib/start-bot.sh
                │ + tmux session │   inside the runtime dir
                │ + Telegram     │
                │ + MCP servers  │
                └────────────────┘
```

The composition order inside `CLAUDE.md` is: persona base → voice overlay (after H1) → team roster (managers only) → protocols → guardrails. You can read the generated file top-to-bottom and it's obvious where each piece came from.

See [`documentation/architecture/overview.md`](documentation/architecture/overview.md) for the deeper dive.

## CLI

```
claudlobby validate              # check fleet.yaml against library/
claudlobby generate              # compose runtime/bots/ from fleet.yaml
claudlobby generate --bot <name> # compose only one bot
claudlobby host-timers           # compose host-global timer units from system.yaml
claudlobby list-library          # show available personas / skills / mcp / etc.
claudlobby diff [--bot <name>]   # show drift between runtime/ and library/
claudlobby promote <bot>         # move runtime drift back to library/ (v1: manual)
claudlobby status [--bot <name>] # fleet health dashboard
claudlobby doctor                # pre-flight fleet health diagnostic
claudlobby report-back           # worker reports from the plane (--since, --bot)
claudlobby uptime [--bot <name>] # per-bot uptime, MTBR, restart-rate metrics
claudlobby events                # fleet events from the plane (--bot, --type, --critical)
claudlobby new-bot               # interactive bot scaffolding
claudlobby new-skill             # scaffold a new skill directory
claudlobby new-guardrail         # scaffold a new guardrail file
claudlobby move-bot <bot> --to <fleet>  # move a bot between fleets
claudlobby warm-cache            # pre-download npx + uvx packages for MCP servers
```

## What this repo gives you — and doesn't

**Gives you:**

- `library/` — 19 expertise profiles (manager, engineer, reviewer, designer, business, data-engineering, …), 54 skills (dispatch, lifecycle, prs, sweep, fleet-status, briefing, status, triage, …), 17 MCP fragments (github, github-app, gws, google-analytics, google-search-console, meta-ads, meta-business, posthog, notion, linear, slack, shopify, printify, homeassistant, docker, spotify, granola), 25 guardrails, 40 protocols
- `lib/` — 90 bash lifecycle scripts: `start-bot.sh`, `keepalive.sh`, `report-back.sh`, `tg-post.sh`, `creds-check.sh` (daily credential keepalive), `fleet-state-update.sh`, and more
- `bin/claudlobby` — the Python compositor
- `fleet.yaml.example` — a full fleet manifest template you can copy and adapt

**You install separately** (the things people miss on a fresh clone):

- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — the CLI + OAuth login (or `ANTHROPIC_API_KEY`)
- **[Telegram channel plugin](https://github.com/anthropics/claude-plugins-official)** — `claude plugin install telegram@claude-plugins-official`
- **A clauDNA-style global skills install** — the `~/.claude/skills/` library (`/simplify`, `/review-pr`, `/tech-debt`, `/session-handoff`, …) is what makes the bots feel competent. Without it, the project skills in `library/skills/` work, but the global toolbox is sparse.
- **Your secrets** — `GITHUB_PAT`, `NOTION_TOKEN`, BotFather tokens (one per bot), MCP server credentials. Stored in `.env` at the repo root (gitignored).

See [`documentation/getting-started.md`](documentation/getting-started.md) for the full bootstrap sequence.

## Sync-back: bots that learn

Bots can edit themselves at runtime — `runtime/bots/` is gitignored, so an in-session edit to a skill, a CLAUDE.md, or a protocol won't pollute git. Two patterns:

- **Skills** auto-sync because they're symlinks: a bot editing `runtime/bots/X/.claude/skills/foo/SKILL.md` is editing `library/skills/foo/SKILL.md`. The change propagates to every bot using `foo`.
- **Composed CLAUDE.md** doesn't auto-sync (the next `generate` would overwrite it). Use:
  - `claudlobby diff <bot>` — show drift vs what `generate` would produce
  - `claudlobby promote <bot>` — pick which drifted lines belong in `library/expertise/`, `voices/`, or a new guardrail/protocol

Foundation for the future ML layer: when claudlobby has embeddings + a knowledge graph behind `library/`, runtime drift becomes training data for "what if more bots needed this rule?"

## Hosts

| Host | Notes |
|------|-------|
| macOS (Mac mini) | launchd via `<bot>.plist`. `lib/creds-check.sh` ships with a launchd install pattern. |
| Linux (Raspberry Pi 5, Debian, Ubuntu) | systemd user services via `<bot>.service`. Set `CLAUDLOBBY_ROOT=$HOME/claudlobby` in the unit's Environment. |
| Linux (root systemd) | Same as user systemd; install to `/etc/systemd/system/` instead of `~/.config/systemd/user/`. |

**GitHub identity.** By default bots use a shared `GITHUB_PAT`. To give a fleet its own
**GitHub App identity** (short-lived installation tokens, branch protection that binds the
bot, commits as `<slug>[bot]`), see
[`documentation/runbooks/github-app-setup.md`](documentation/runbooks/github-app-setup.md).
Opt-in and dormant — a fleet that declares no `github_app:` is unaffected.

## Status

This repo is in active migration from the older "one-dir-per-bot" template model to the compositor. The current layout is: `library/` (sources), `lib/` (lifecycle scripts), `runtime/` (output), `voices/` (overlays).

PRs welcome.
