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

claudlobby flips that: every cross-cutting concern (a guardrail, a protocol, an MCP server config, a skill, a persona) lives **once** in `library/`. `fleet.yaml` declares which bot uses which pieces. A configuration plan composes self-contained bot directories under `runtime/bots/<name>/`; host activation applies the reviewed plan and enrolls their native units.

Add a 9th bot? Add its stanza to `fleet.yaml`. Update a guardrail? Edit its source in `library/guardrails/`. Stage and review a configuration plan, then activate it.

## Quick start

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

- `library/` — 19 expertise profiles (manager, engineer, reviewer, designer, business, data-engineering, …), 55 skills (dispatch, lifecycle, prs, sweep, fleet-status, briefing, status, triage, …), 17 MCP fragments (github, github-app, gws, google-analytics, google-search-console, meta-ads, meta-business, posthog, notion, linear, slack, shopify, printify, homeassistant, docker, spotify, granola), 25 guardrails, 40 protocols
- `lib/` — native runtime scripts and companions; `harness/` holds the development and measurement instruments. Operators and agents use the public CLI
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
