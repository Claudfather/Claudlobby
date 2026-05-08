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

```bash
git clone https://github.com/<your-username>/claudlobby.git
cd claudlobby

python3 -m venv .venv && .venv/bin/pip install -e .
cp fleet.yaml.example fleet.yaml
cp .env.example .env                # if present; otherwise create one with your tokens

# Edit fleet.yaml — pick personas, skills, MCP servers, set Telegram chat IDs
$EDITOR fleet.yaml

# Compose runtime/bots/<name>/ for every bot
.venv/bin/claudlobby generate

# Or, without an install:
./bin/claudlobby generate
```

The generated `runtime/bots/<bot>/` is everything Claude Code needs — `CLAUDE.md`, `.mcp.json`, `bot.conf`, `.claude/skills/` symlinks, plus a systemd `<bot>.service` and a launchd `<bot>.plist`. Pick the right one for your host.

See [`docs/getting-started.md`](docs/getting-started.md) for the zero-to-running walkthrough.

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

See [`docs/architecture.md`](docs/architecture.md) for the deeper dive.

## CLI

```
claudlobby validate              # check fleet.yaml against library/
claudlobby generate              # compose runtime/bots/ from fleet.yaml
claudlobby generate --bot <name> # compose only one bot
claudlobby list-library          # show available personas / skills / mcp / etc.
claudlobby diff [--bot <name>]   # show drift between runtime/ and library/
claudlobby promote <bot>         # move runtime drift back to library/ (v1: manual)
claudlobby status                # fleet health (stub for now)
```

## What this repo gives you — and doesn't

**Gives you:**

- `library/` — pre-built personas (manager, engineer, reviewer, designer, business), 16+ skills (dispatch, lifecycle, prs, sweep, fleet-status, briefing, status, triage, …), 9 MCP fragments (github, notion, slack, gmail, shopify, printify, homeassistant, docker, spotify), 8 guardrails, 7 protocols
- `lib/` — bash lifecycle scripts: `start-bot.sh`, `keepalive.sh`, `report-back.sh`, `tg-post.sh`, `creds-check.sh` (daily credential keepalive), `fleet-state-update.sh`
- `bin/claudlobby` — the Python compositor
- `examples/fleet-pi.yaml` — a working 8-bot Pi fleet manifest you can copy and adapt

**You install separately** (the things people miss on a fresh clone):

- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — the CLI + OAuth login (or `ANTHROPIC_API_KEY`)
- **[Telegram channel plugin](https://github.com/anthropics/claude-plugins-official)** — `claude plugin install telegram@claude-plugins-official`
- **A clauDNA-style global skills install** — the `~/.claude/skills/` library (`/simplify`, `/review-pr`, `/tech-debt`, `/session-handoff`, …) is what makes the bots feel competent. Without it, the project skills in `library/skills/` work, but the global toolbox is sparse.
- **Your secrets** — `GITHUB_PAT`, `NOTION_TOKEN`, BotFather tokens (one per bot), MCP server credentials. Stored in `.env` at the repo root (gitignored).

See [`docs/first-run-bootstrap.md`](docs/first-run-bootstrap.md) for the full bootstrap sequence.

## Sync-back: bots that learn

Bots can edit themselves at runtime — `runtime/bots/` is gitignored, so an in-session edit to a skill, a CLAUDE.md, or a protocol won't pollute git. Two patterns:

- **Skills** auto-sync because they're symlinks: a bot editing `runtime/bots/X/.claude/skills/foo/SKILL.md` is editing `library/skills/foo/SKILL.md`. The change propagates to every bot using `foo`.
- **Composed CLAUDE.md** doesn't auto-sync (the next `generate` would overwrite it). Use:
  - `claudlobby diff <bot>` — show drift vs what `generate` would produce
  - `claudlobby promote <bot>` — pick which drifted lines belong in `library/personas/`, `library/voices/`, or a new guardrail/protocol

Foundation for the future ML layer: when claudlobby has embeddings + a knowledge graph behind `library/`, runtime drift becomes training data for "what if more bots needed this rule?"

## Hosts

| Host | Notes |
|------|-------|
| macOS (Mac mini) | launchd via `<bot>.plist`. `lib/creds-check.sh` ships with a launchd install pattern. |
| Linux (Raspberry Pi 5, Debian, Ubuntu) | systemd user services via `<bot>.service`. Set `CLAUDLOBBY_ROOT=$HOME/claudlobby` in the unit's Environment. |
| Linux (root systemd) | Same as user systemd; install to `/etc/systemd/system/` instead of `~/.config/systemd/user/`. |

## Status

This repo is in active migration from the older "one-dir-per-bot" template model to the compositor. The current layout is: `library/` (sources), `lib/` (lifecycle scripts), `runtime/` (output), `voices/` (overlays).

PRs welcome.
