# claudlobby

A **compositor** for agent-CLI fleets (Claude Code today; Codex through #2149). One repo, one `fleet.yaml`, N bots — composed at runtime from a library of personas, skills, MCP fragments, guardrails, and protocols.

```
library/  ← composable building blocks (sources of truth, in git)
voices/   ← personality overlays
fleet.yaml ← the recipe (which bots, which pieces, which Telegram groups)
runtime/  ← what gets generated (gitignored)
```

Runs anywhere the composed CLI does: Mac mini, Linux box, Raspberry Pi 5.

## Why a compositor

The early "one directory per bot" pattern duplicated the same persona scaffolding, MCP boilerplate, lifecycle protocol, and guardrail rules across every bot. Adding a 9th bot meant copy-pasting a CLAUDE.md and editing it. Updating a guardrail meant editing 8 files.

claudlobby flips that: every cross-cutting concern (a guardrail, a protocol, an MCP server config, a skill, a persona) lives **once** in `library/`. `fleet.yaml` declares which bot uses which pieces. A configuration plan composes self-contained bot directories under `runtime/bots/<name>/`; host activation applies the reviewed plan and enrolls their native units.

Add a 9th bot? Add its stanza to `fleet.yaml`. Update a guardrail? Edit its source in `library/guardrails/`. Stage and review a configuration plan, then activate it.

## What it changes on your machine

The [setup walkthrough](documentation/getting-started.md) builds an installed wheel, assembles a sealed release with `host setup`, and activates an authored fleet with `fleet setup`. Assembly checks prerequisites and writes release files; **activation starts supervised processes and installs user units**. Neither step installs system packages, invokes `sudo`, enables Linux lingering, or installs Claude Code. Provision those prerequisites separately.

| Change | Where | Made by |
|---|---|---|
| A copied interpreter, dependency wheels and immutable framework resources | `<data-root>/releases/` | `host setup` / release assembly |
| Authored manifests, staged configuration, activation records and Plane state | The explicit data root, including `local/` and `state/` | `fleet setup`, `config plan`, `host activate`, and runtime owners |
| Units running under your account: bots, fleet timers and host jobs | The reviewed user-unit directory: normally `~/.config/systemd/user/` or `~/Library/LaunchAgents/` | `host activate` through the native supervisor adapter |
| Claude Code plugins at user level | `~/.claude/plugins/` | `plugin_ensure` in `claudlobby/_runtime_scripts/lib-common.sh`, used by bot startup; this can affect your own Claude sessions too |
| Your Claude Code consent settings | `~/.claude/settings.json`, or the selected Claude config directory | The headless consent block in `claudlobby/_runtime_scripts/start-bot.sh` sets `skipAutoPermissionPrompt` and `skipDangerousModePermissionPrompt`; malformed JSON is replaced with those keys |
| Each bot's Telegram access list | `~/.claude/channels/telegram-<handle>/access.json` | The activation composition owner in `claudlobby/composer.py`; writes are atomic and mode 0600, and failures are disclosed per bot |
| Optional managed channel approvals | `/etc/claude-code/managed-settings.json`, or `/Library/Application Support/ClaudeCode/managed-settings.json` | Explicit operator `host channels approve`; preserves existing keys, requires pre-existing administrative write access and never invokes `sudo` |
| MCP package downloads | `state/mcp/npm` in the data root, and npm's and uv's host caches | `config plan` (and so `fleet setup`) installs a copy of each exactly pinned npx server before it composes; explicit `host cache warm` prepares the rest; the first MCP launch may otherwise download packages |

**After activation, enrolled jobs keep running.** `claudlobby/system.yaml` declares the defaults and `claudlobby host doctor --switches` exposes their state. Claude updates can update the user's shared CLI; plugin refreshes can affect shared plugin installs. The browser reaper targets verified automation leftovers. Sibling repository updates remain opt-in. The retired `pull-root` job is not installed by this release; an already-installed old puller must be held and retired through the [existing-host conversion runbook](documentation/existing-host-release-conversion.md).

The Plane serves a read-only view on `127.0.0.1:8899` and receives events through `<data-root>/state/plane/ingest.sock`. Release assembly and configuration planning are distinct from native activation; a successful plan does not prove a bot is running or receiving messages.

**Where secrets live**

- `.env` files resolve in host, root, fleet, then bot order, with the most specific assignment winning (`claudlobby/_runtime_scripts/env-tiers.sh`). Keep fleet tokens in the data overlay. `claudlobby config explain KEY` reports provenance without printing secret values.
- Claude Code's login lives in its config directory. Bots using the default account share the operator's login; the same OS user may also access the operator's `gh` login.
- A GitHub App identity uses its explicitly configured credentials and private key; see [GitHub App setup](documentation/runbooks/github-app-setup.md).

## Safety model

**A bot is a Claude Code session that can run shell commands as you.** Trust it as you would a person with a shell on your account.

**What a bot can do**

- **Run shell commands without asking.** The default permission mode is `acceptEdits` (`claudlobby/composer.py`), but an allowed tool runs without a prompt. `allow_all: true` in `library/expertise/software-engineering.md` allows every tool, bare `Bash` included (`ALL_TOOLS` in `claudlobby/composer.py`), and the seed bot's `library/expertise/setup-assistant.md` allows `Bash` as well. `fleet.yaml` can also set `permission_mode: auto` or `dangerously_skip_permissions: true` (`fleet.yaml.example`).
- **Act as your user.** Every bot's unit runs under your account (see the units above), so the operating system lets a bot read and change anything you can, other bots' files included.
- **Use `sudo` wherever your account needs no password for it.** No composed rule denies `sudo`, so on a host where `sudo -n true` succeeds, every bot has root.
- **Use every credential on the host.** Each bot's session exports the `.env` files above (`claudlobby/_runtime_scripts/start-bot.sh`), and it has your Claude Code and `gh` logins.
- **Take instructions over Telegram.** Direct messages are accepted only from `human_telegram_id`. In a fleet's group, composition leaves `allowFrom` empty (`claudlobby/composer.py`), which the Telegram plugin reads as every member of the group (the plugin's `server.ts`). A `fleet.yaml` field for that list is #1669.
- **Follow your own Claude Code settings.** Bots on the `default` account read your `~/.claude/settings.json`, so an allow rule you add there, such as a bare `Bash`, applies to every bot.

**What bounds them**

- **Deny rules**, in each bot's `.claude/settings.local.json`. Every bot is denied Read and Edit of its fleet siblings' directories (`claudlobby/composer.py`), plus any `tools.deny` in `fleet.yaml`, and `isolation.shared_config: true` adds transcripts, credentials and the `.env` files (`claudlobby/isolation.py`). **These are not an operating-system boundary.** A deny rule gates Claude Code's own tool calls: the Read tool, and a shell command given a literal path. It does not stop an interpreter that opens a file itself, a path written through a variable, or any script, hook, timer or MCP server (`claudlobby/isolation.py`). Splitting bots off your account is tracked in #1606.
- **Hooks on every bot** (`defaults.hooks` in `claudlobby/system.yaml`). `claudlobby/_runtime_scripts/gh-mention-guard.sh` rewrites `@` mentions out of GitHub-bound text, and `claudlobby/_runtime_scripts/vault-git-guard.sh` refuses git state rewrites (checkout, rebase, reset and the like) inside a Claudron vault. The rest record activity.
- **Guardrails are instructions, not enforcement.** The seed's `no-push-main`, `no-destructive-git`, `pii-protection` and `no-fabrication` (`fleet.yaml.seed`) are text in each bot's `CLAUDE.md`, and none carries a deny rule (`library/guardrails/`). Only branch protection on your repository actually blocks a push to `main`.
- **The sandbox is off in the seed** (`sandbox: enabled: false` in `fleet.yaml.seed`). The `sandbox:` block turns Claude Code's sandbox on (`documentation/fleet-yaml-schema.md`).
- **Your tokens' scope.** A bot can do on GitHub, Telegram or any other service what the token it holds allows.

To tighten it: run the fleet under a dedicated account that has no `sudo`, keep each fleet's Telegram group to people you would give a shell, give bots fine-grained tokens scoped to the fleet's repositories, and turn the sandbox on.

## Quick start

Existing checkout-based hosts must follow the [conversion prerequisite](documentation/existing-host-release-conversion.md) before pulling this release. The cold-host sequence below does not retire an old source-pulling timer.

**You need:** a working Python interpreter, `tmux`, a running user manager (launchd or systemd user), [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated, and the Telegram channel plugin if your fleet declares Telegram. `host setup` checks the native manager and required executables; it does not install them.

Build the candidate wheel and its offline, SHA-256-locked dependency wheelhouse as shown in [Getting started](documentation/getting-started.md). Install that wheel into a temporary bootstrap venv, then use its CLI to assemble a sealed release under an **absolute data root outside the checkout**:

```bash
"$WORK/bootstrap/bin/claudlobby" --root "$DATA" host setup --wheel "$WHEEL" \
  --dependency-lock "$WORK/dependency.lock" --wheelhouse "$WORK/wheelhouse" \
  --interpreter "$WORK/bootstrap/bin/python"
```

The result names the sealed release CLI. Edit a copy of its packaged `fleet.yaml.seed` outside the data root, put the matching token in `$DATA/local/seed/.env`, then activate it through that **sealed CLI**:

```bash
"$RELEASE_CLI" --root "$DATA" --fleet seed fleet setup \
  --config "$WORK/fleet.yaml" --install-directory "$USER_UNIT_DIR"
"$RELEASE_CLI" --root "$DATA" host doctor
```

`fleet setup` places the authored file at `$DATA/local/seed/fleet.yaml`, stages the whole host, and uses the existing activation owner to start it. A different existing fleet file requires `--replace-config`. The sealed release keeps its own interpreter and packaged library/native scripts; generated bot files and persistent state live under the data root. See [Getting started](documentation/getting-started.md) for the complete commands, prerequisites, and paths for macOS and Linux.

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
                         ▼ config plan → host activate
                         │
┌────────────────────────────────────────────────────────────┐
│  runtime/bots/<name>/     ← gitignored, regeneratable       │
│  ├── CLAUDE.md            ← persona + voice + roster +      │
│  │                          protocols + guardrails          │
│  ├── .mcp.json            ← merged from library/mcp/        │
│  ├── bot.conf             ← env exports for claudlobby/_runtime_scripts/start-bot.sh│
│  ├── .claude/skills/      ← symlinks → library/skills/      │
│  ├── <bot>.service        ← systemd (Linux)                 │
│  └── <bot>.plist          ← launchd (macOS)                 │
└────────────────────────┬───────────────────────────────────┘
                         │
                         ▼
                ┌────────────────┐
                │ Claude Code    │ ← started by claudlobby/_runtime_scripts/start-bot.sh
                │ + tmux session │   inside the runtime dir
                │ + Telegram     │
                │ + MCP servers  │
                └────────────────┘
```

The composition order inside `CLAUDE.md` is: persona base → voice overlay (after H1) → team roster (managers only) → protocols → guardrails. You can read the generated file top-to-bottom and it's obvious where each piece came from.

See [`documentation/architecture/overview.md`](documentation/architecture/overview.md) for the deeper dive.

## CLI

```
claudlobby --fleet <name> config validate  # check authored fleet.yaml
claudlobby host setup                      # assemble a sealed cold-host release
claudlobby --fleet <name> fleet setup       # copy initial source, stage and activate
claudlobby config plan --release <ID>      # stage all declared host fleets
claudlobby config diff <PLAN_ID>           # inspect staged paths and state digests
claudlobby host activate <PLAN_ID> --install-directory <PATH>  # apply the plan
claudlobby library list          # show available personas / skills / mcp / etc.
claudlobby config diff [--bot <name>]  # show current rendered drift without values
claudlobby fleet status             # fleet health dashboard
claudlobby bot status <name>        # one bot, including native and Plane observations
claudlobby host doctor                # pre-flight fleet health diagnostic
claudlobby --json fleet reports list  # paginated worker reports (--bot, --status, --since RFC3339)
claudlobby fleet uptime [--bot <name>] # per-bot uptime, MTBR, restart-rate metrics
claudlobby event list                # fleet events from the plane (--bot, --type, --critical)
claudlobby bot create --interactive               # author a bot; stage and activate separately
claudlobby library create --kind skill             # scaffold a new skill directory
claudlobby library create --kind guardrail         # scaffold a new guardrail file
claudlobby bot move <bot> --to <fleet>  # move a bot between fleets
claudlobby host cache warm            # pre-download npx + uvx packages for MCP servers
```

## What this repo gives you — and doesn't

**Gives you:**

- `library/` — 19 expertise profiles (manager, engineer, reviewer, designer, business, data-engineering, …), 55 skills (dispatch, lifecycle, prs, sweep, fleet-status, briefing, status, triage, …), 17 MCP fragments (github, github-app, gws, google-analytics, google-search-console, meta-ads, meta-business, posthog, notion, linear, slack, shopify, printify, homeassistant, docker, spotify, granola), 28 guardrails, 40 protocols
- `claudlobby/_runtime_scripts/` — native runtime scripts and companions; `harness/` holds the development and measurement instruments. Operators and agents use the public CLI
- `claudlobby` — the installed Python CLI and compositor
- `fleet.yaml.example` — a full fleet manifest template you can copy and adapt

**You install separately** (the things people miss on a fresh clone):

- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — the CLI + OAuth login (or `ANTHROPIC_API_KEY`)
- **[Telegram channel plugin](https://github.com/anthropics/claude-plugins-official)** — `claude plugin install telegram@claude-plugins-official`
- **A clauDNA-style global skills install** — the `~/.claude/skills/` library (`/simplify`, `/review-pr`, `/tech-debt`, `/session-handoff`, …) is what makes the bots feel competent. Without it, the project skills in `library/skills/` work, but the global toolbox is sparse.
- **Your secrets** — `GITHUB_PAT`, `NOTION_TOKEN`, BotFather tokens (one per bot), MCP server credentials. Keep them in the private host/data-root/fleet environment tiers described in the bootstrap guide.

See [`documentation/getting-started.md`](documentation/getting-started.md) for the full bootstrap sequence.

## Sync-back: bots that learn

Put intended skill and policy changes in authored fleet-overlay source. Packaged
release assets are sealed: editing a generated skill's symlink target can damage
the installed release. `claudlobby config diff --bot <bot>` identifies rendered
drift without printing values; review the change, edit its authored source, then
stage and inspect a configuration plan before activation. Bot-owned `memory/`
and `data/` remain mutable and are preserved during composition.

## Hosts

| Host | Notes |
|------|-------|
| macOS (Mac mini) | launchd via `<bot>.plist`. `claudlobby/_runtime_scripts/creds-check.sh` ships with a launchd install pattern. |
| Linux (Raspberry Pi 5, Debian, Ubuntu) | systemd user services via `<bot>.service`. Set `CLAUDLOBBY_ROOT=$HOME/claudlobby` in the unit's Environment. |
| Linux (root systemd) | Same as user systemd; install to `/etc/systemd/system/` instead of `~/.config/systemd/user/`. |

**GitHub identity.** By default bots use a shared `GITHUB_PAT`. To give a fleet its own
**GitHub App identity** (short-lived installation tokens, branch protection that binds the
bot, commits as `<slug>[bot]`), see
[`documentation/runbooks/github-app-setup.md`](documentation/runbooks/github-app-setup.md).
Opt-in and dormant — a fleet that declares no `github_app:` is unaffected.

## Status

This repo is in active migration from the older "one-dir-per-bot" template model to the compositor. The current layout is: `library/` (sources), `claudlobby/_runtime_scripts/` (lifecycle scripts), `runtime/` (output), `voices/` (overlays).

PRs welcome.

## License

Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE) for the notices that travel with it: the copyright notice, and the MIT License notice for three outside contributions made while the project declared MIT.
