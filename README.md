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

## Quick start

**You need:** An Anthropic account (Claude Max, Team, or Enterprise — or an `ANTHROPIC_API_KEY`), [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed, and a Telegram account.

**Guided setup (recommended):** Clone the repo, install, and let claudfather walk you through it:

```bash
git clone https://github.com/Claudfather/Claudlobby.git
cd Claudlobby
python3 -m venv .venv               # required — see note below
source .venv/bin/activate
python3 -m pip install -e .
claude                              # opens Claude Code in the repo
```

Then type `/setup` — it checks your host, collects credentials, and spins up claudfather (the built-in setup assistant) on Telegram. Continue setup from your phone.

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
python3 -m venv .venv && source .venv/bin/activate && python3 -m pip install -e .

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
claudlobby report-back           # query bot work event ledger (--since, --bot)
claudlobby uptime [--bot <name>] # per-bot uptime, MTBR, restart-rate metrics
claudlobby events                # tail/filter JSONL events across all bots
claudlobby new-bot               # interactive bot scaffolding
claudlobby new-skill             # scaffold a new skill directory
claudlobby new-guardrail         # scaffold a new guardrail file
claudlobby move-bot <bot> --to <fleet>  # move a bot between fleets
claudlobby warm-cache            # pre-download npx packages for MCP servers
```

## What this repo gives you — and doesn't

**Gives you:**

- `library/` — 19 expertise profiles (manager, engineer, reviewer, designer, business, data-engineering, …), 53 skills (dispatch, lifecycle, prs, sweep, fleet-status, briefing, status, triage, …), 17 MCP fragments (github, github-app, gws, google-analytics, google-search-console, meta-ads, meta-business, posthog, notion, linear, slack, shopify, printify, homeassistant, docker, spotify, granola), 24 guardrails, 39 protocols
- `lib/` — 79 bash lifecycle scripts: `start-bot.sh`, `keepalive.sh`, `report-back.sh`, `tg-post.sh`, `creds-check.sh` (daily credential keepalive), `fleet-state-update.sh`, and more
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
