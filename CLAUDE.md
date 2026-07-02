# claudlobby

Compositor for Claude Code agent fleets. Transforms `fleet.yaml` + `library/` into runnable bot directories with isolated identities, MCP servers, skills, and systemd/launchd supervision.

**North star:** Trivial to run a fleet of distinct, cooperating bots on cheap hardware.

**New here?** See [`documentation/getting-started.md`](documentation/getting-started.md) for the clone-to-fleet walkthrough and [`documentation/fleet-yaml-schema.md`](documentation/fleet-yaml-schema.md) for every config field.

## Architecture

```
fleet.yaml          →  claudlobby generate  →  runtime/bots/<name>/
library/                                         ├── CLAUDE.md      (composed instructions)
  expertise/                                     ├── bot.conf       (env vars, sourced at startup)
  skills/                                        ├── .mcp.json      (MCP server config)
  mcp/                                           ├── *.service      (systemd unit)
  guardrails/                                    ├── memory/        (bot-owned persistent state)
  protocols/                                     ├── data/          (bot-owned data + scripts)
  integrations/                                  └── projects/      (git checkouts, gitignored)
  resources/
  lessons/
  principles/
  permissions/
  post_actions/
voices/
templates/claude.md.j2
```

The compositor reads `fleet.yaml` (which declares bots, their expertise, skills, MCP servers, guardrails, etc.) and assembles each bot's directory from the shared `library/`. The template `claude.md.j2` owns all top-level structure — library files supply slot content.

### Key concepts

- **Expertise** — Role definitions (e.g. `software-engineering`, `orchestration`, `code-review`). Each bot gets one or more. This is who the bot is.
- **Skills** — Composable slash-command packages in `library/skills/<name>/SKILL.md`. What the bot can do.
- **MCP fragments** — JSON wire configs in `library/mcp/` with `${ENV_VAR}` placeholders. Never real tokens.
- **Guardrails** — Safety rules composed per-bot (e.g. `no-push-main`, `snowflake-read-only`).
- **Protocols** — Reusable workflow patterns (dispatch, review-flow, context-management).
- **Plugins** — Claude Code plugins installed fleet-wide. `claudna@Claudfather` is a built-in default; extras via `fleet.plugins.additional`. Auto-installed on bot start.
- **Voices** — Optional personality overlays from `voices/`.

### Runtime model

Bots run as supervised processes: systemd user units on Linux, launchd LaunchAgents on macOS. Each bot lives in its own tmux session on its **own** tmux server (a private `-L <socket>` == `BOT_SERVICE`), so one server's death drops only that bot, never the whole fleet. The manager dispatches work via the socket-aware `lib/dispatch.sh` helper (which resolves the worker's socket); workers report back via `lib/report-back.sh`.

Key lifecycle scripts in `lib/`:

| Script | Purpose |
|--------|---------|
| `start-bot.sh` | Launches a bot's tmux session with env vars from bot.conf |
| `spin-up-bot.sh` | Idempotent: enrolls bot as supervised service, then starts |
| `keepalive.sh` | Per-bot watchdog — restarts if tmux session dies |
| `keepalive-all.sh` | Fleet-level watchdog — runs keepalive for all bots |
| `reconcile-fleet.sh` | Audits supervision state: healthy, orphan, missing, unbound |
| `report-back.sh` | Worker → manager structured reporting via tmux |
| `fleet-state-update.sh` | Atomic state/fleet-state.json updates with flock locking |
| `pre-stop-handoff.sh` | Graceful context handoff before service stop |
| `lib-common.sh` | Shared helpers: OS detection, bot.conf loading, safe mktemp |
| `log-rotate-fleet.sh` | Fleet-wide log rotation |
| `log-rotate.sh` | Single-bot log rotation |
| `log-tool-call.sh` | Reference hook script for tool-call logging |
| `git-pull-all.sh` | Pull all repos in a bot's projects/ directory |
| `tg-post.sh` | Bash helper for posting to Telegram |
| `disk-monitor.sh` | Check disk usage, alert if high |
| `fleet-memory-check.sh` | Fleet memory planning and monitoring |
| `bench-cold-start.sh` | Cold-start timing baseline and regression tracking |
| `check-npx-cache.sh` | Verify npx package cache state for MCP servers |
| `sprint-trigger.sh` | Schedule-driven autonomous sprint nudger |
| `creds-check.sh` | Credential validation for fleet secrets |
| `bot-sweep-cron.sh` | Periodic bot sweep via cron |
| `install-bot.sh` | Bot service enrollment (launchd) |
| `install-bot-systemd.sh` | Bot service enrollment (systemd) |
| `install_fleet_timer.sh` | Generic fleet/host timer enrollment (systemd) — copies composed units + enables |
| `install_fleet_timer_launchd.sh` | Generic fleet/host timer enrollment (launchd) — copies composed plist + bootstraps |
| `install-keepalive-systemd.sh` | Keepalive timer enrollment (systemd, thin wrapper) |
| `install-cron.sh` | Cron job installation |
| `install-creds-check-systemd.sh` | Creds-check timer enrollment (systemd, thin wrapper) |
| `install-fleet-pulse-systemd.sh` | Fleet-pulse systemd timer enrollment (systemd) |
| `setup-mac-mini.sh` | macOS host setup automation |
| `setup-system` | Setup backbone: host prereqs + system.yaml host-job enrollment (cross-platform) |
| `setup-fleet` | Setup backbone: per-fleet apply+enroll — default jobs (dormant opt-ins skipped), bots (skips healthy), reconcile; root mode when invoked without a fleet |
| `setup-fleets` | Run setup-fleet for every fleet on the host |
| `bot-vitals.sh` | Bot vitals collection for observability |
| `fleet-pulse.sh` | Fleet-wide heartbeat / status monitoring |
| `tail-fleet.sh` | Fleet-wide log tail + grep filter |
| `ci-health-check.sh` | Pre-push CI health canary for target branch |
| `data-sweep.sh` | Data directory sweep and cleanup |
| `dispatch.sh` | Dispatch helper for manager → worker |
| `dispatch-task.sh` | Task dispatch helper |
| `telegram-instant-ack.sh` | Telegram instant acknowledgment for inbound messages |
| `fleet-utilization.sh` | Fleet utilization rollup — per-bot busy/idle % |
| `validate-bot-change.sh` | Empirical validation harness for bot behavior changes |
| `update-claude-code.sh` | Daily Claude Code binary download (download-only; no fleet bounce) — runs as the `claude-update` host job (system.yaml `host.jobs`, enrolled by `setup-system`) |
| `notify-behind.sh` | Daily source-currency nudge — FLEET NOTICE when the install is N commits behind origin/main (notify-only, never pulls) — runs as the `notify-behind` host job |
| `reload-fleet.sh` | Daily live plugin/skill reload — `claude plugin update` + generate, then mark running bots for a keepalive-driven `/reload` (no restart) |
| `install-reload-fleet-systemd.sh` | Reload-fleet daily timer enrollment (systemd) |
| `weekly-worker-restart.sh` | Weekly lossless restart of worker bots (managers excluded) to apply a staged binary |
| `install-weekly-worker-restart-systemd.sh` | Weekly-worker-restart timer enrollment (systemd) |

## Repository Hygiene — MANDATORY

### What goes in git (shared, reusable, generalized)

Everything in these top-level directories is committed and shared:

- `library/` — All composable building blocks (see Architecture above)
- `voices/` — Personality overlays
- `templates/` — Jinja2 templates for CLAUDE.md generation
- `lib/` — Lifecycle and utility scripts
- `claudlobby/` — Python compositor source
- `documentation/` — Architecture docs, schema reference, setup guides
- `fleet.yaml.example` — Template manifest (committed; `fleet.yaml` is NOT)

### What stays local (gitignored, fleet-specific, secret)

These are ALL gitignored — never commit them:

- `fleet.yaml` — Your active fleet config. Copy from `fleet.yaml.example`.
- `local/` — Fleet overlays. Each `local/<fleet>/` contains fleet.yaml, local library overrides, voices, and runtime output. **All fleet-specific content lives here.**
- `local/<fleet>/runtime/bots/` — Generated bot directories
- `local/<fleet>/library/` — Fleet-specific library content not general enough for shared
- `.env` — Secrets (tokens, PATs, OAuth credentials). Never committed.
- `runtime/` — Root-mode generated output (if running without fleet overlays)
- `*/projects/` — Git checkouts in bot directories

### The bright line

**If it contains a real token, API key, credential, org ID, database UUID, or fleet-specific path → it goes in `local/` or `.env`.** If it's a reusable pattern that any fleet could benefit from → it goes in `library/`.

When in doubt: would another person running claudlobby find this useful? Yes → library. No → local overlay.

### No PII in committed assets

No personally identifiable information in any checked-in file. This includes:

- Real email addresses, phone numbers, physical addresses
- Real Telegram chat IDs, user IDs, or bot tokens
- Real API keys, OAuth tokens, or credentials
- Real database UUIDs, org IDs, or project IDs
- Real names tied to personal details (author names in pyproject.toml are fine)
- Real IP addresses (localhost/examples are fine)
- Financial account numbers or identifiers

Documentation and examples must use obviously fake placeholders (`ghp_xxxxxxxxxxxxxxxxxxxx`, `"-1001234567890"`, `8888888:AAAAAAAAAAAAAAAAAAAA`). If you need to reference a real service, use generic descriptions, not real account details.

### Before committing, always verify

```bash
git status           # nothing from local/, runtime/, .env should appear
git diff --cached    # no secrets, no fleet-specific UUIDs, no hardcoded paths
```

## Working on This Repo

### Adding library content

Each library category has its own format. Check the category's `README.md` for specifics. General rules:

1. Create the file in the appropriate `library/<category>/` directory
2. Use YAML frontmatter with `title:` and `description:` fields
3. Add an H1 heading (`# Title`) matching the frontmatter title — the loader strips it to avoid duplication in composed output
4. Use `{{BOT_NAME}}`, `{{FLEET_NAME}}`, `{{CLAUDLOBBY_ROOT}}` Jinja2 placeholders where appropriate
5. Test: `claudlobby --fleet <your-fleet> generate` and verify the content appears in the right bot's CLAUDE.md
6. Commit to a branch, PR, review

**Heading levels matter.** The template renders library content inside `##`/`###` sections. The loader runs `_demote_headings` to shift all headings down. An H1 (`#`) in your file becomes H2 in the output. If you start with H3, it becomes H4 — which may be too deep.

### Adding compositor features

1. Edit Python source in `claudlobby/`
2. Run tests: `pip install -e '.[dev]' && pytest`
3. Test against your local fleet: `claudlobby --fleet <name> validate` then `generate`
4. Run `claudlobby --fleet <name> diff` to verify no unintended drift
5. Commit to a branch, PR, review

### Adding or modifying lib/ scripts

1. Source `lib-common.sh` for shared helpers (OS detection, bot.conf loading, safe mktemp)
2. Always use `set -euo pipefail` — use `|| true` for intentional failures
3. Quote all variables. Use `printf '%s'` instead of `echo` for values that may contain tokens
4. Test on both Linux and macOS where applicable (use `lib-common.sh` OS detection helpers)
5. Never hardcode fleet names, user home dirs, or Homebrew paths — use env vars and detection

### Validating changes to how a bot behaves — MANDATORY

Any change that affects **how a bot behaves at runtime** (lib/ supervision & observability scripts, hooks, skills, protocols, guardrails, principles, composed `bot.conf` env) must be **empirically validated** before merge. Unit tests prove *composition* — that the env var lands in `bot.conf`. Only running the code proves *behavior* — that the event actually fires, the alert actually sends, the bot actually does the thing. Follow the loop:

1. **Deliver** — make the code/library change.
2. **Add config** — set the relevant field(s) in `fleet.yaml`.
3. **Recompose** — `claudlobby --fleet <fleet> generate`; confirm the change landed in the composed `bot.conf` / `settings.local.json` / `CLAUDE.md`.
4. **Observe** — run it and watch the real behavior:
   - For observability/trust-loop behaviors: `bash lib/validate-bot-change.sh` stands up a throwaway bot + tmux sessions and asserts the events fire end-to-end. Extend it when you add a new event/check.
   - For other behavior: spin a bot (`lib/spin-up-bot.sh`), drive the affected path, and watch `data/events/*.jsonl` / `keepalive.log` / the pane.

**Cite the observation in the PR body** ("ran `validate-bot-change.sh` → activity_stuck + overdue_dispatch fired; manager notified") — claimed evidence is not evidence. This is also how latent bugs surface: the harness above caught a `fleet-pulse.sh` sweep-abort that every unit test missed.

### Never hand-edit generated output

Files in `runtime/bots/<name>/` are generated by `claudlobby generate`. Hand-edits will be overwritten on the next generate. To change a bot's config:

1. Edit `fleet.yaml` (fleet-level config) or `library/<category>/` (content)
2. Re-run `claudlobby generate`
3. If the bot drifted during a session (`claudlobby diff` shows changes), use `claudlobby promote` to extract the drift back into library

## Key Commands

```bash
# Composition
claudlobby validate                    # check fleet.yaml against library
claudlobby generate                    # compose runtime/bots/ from fleet.yaml
claudlobby generate --bot <name>       # compose one bot
claudlobby diff                        # show drift between runtime and generate
claudlobby promote --bot <name>        # extract bot drift back into library
claudlobby list-library                # show available building blocks
claudlobby new-bot                     # interactive bot scaffolding

# Operations
claudlobby status                      # fleet health dashboard
claudlobby status --bot <name>         # detailed status for one bot
claudlobby doctor                      # pre-flight fleet health diagnostic
claudlobby report-back                 # query bot work event ledger
claudlobby report-back --since 24h     # filter by time window
claudlobby uptime                      # per-bot uptime, MTBR, restart-rate
claudlobby warm-cache                  # pre-download npx packages for MCP servers
claudlobby move-bot <bot> --to <fleet> # move a bot between fleets

# Scaffolding
claudlobby new-bot                     # interactive bot scaffolding
claudlobby new-skill                   # scaffold a new skill directory
claudlobby new-guardrail               # scaffold a new guardrail file

# Migration (from legacy layouts)
claudlobby env-migrate                 # migrate .env files into fleet structure
claudlobby data-migrate                # migrate bot data directories
claudlobby cron-migrate                # migrate crontab entries to new paths
claudlobby memory-migrate              # copy memory files from ~/.claude/projects/ to per-bot dirs

# Testing
pip install -e '.[dev]' && pytest      # run test suite
```

Use `--fleet <name>` for overlay mode: `claudlobby --fleet <your-fleet> generate`

### Fleet operations (lib/ scripts)

```bash
# Bot lifecycle
lib/spin-up-bot.sh <bot-dir>           # enroll + start (idempotent)
lib/reconcile-fleet.sh <fleet>         # audit fleet supervision state
lib/reconcile-fleet.sh <fleet> --enroll # fix orphan bots

# Maintenance
lib/log-rotate-fleet.sh --fleet <name> # rotate all bot logs
lib/git-pull-all.sh <projects-dir>     # pull all repos in a directory
lib/disk-monitor.sh                    # check disk usage, alert if high
lib/fleet-memory-check.sh              # fleet memory planning and monitoring
lib/bench-cold-start.sh               # cold-start timing baseline
lib/check-npx-cache.sh                # verify npx cache state
```

## Python Package Structure

```
claudlobby/
  __main__.py      — CLI entry point, migration commands, argparse setup
  config.py        — fleet.yaml parsing, BotConfig/FleetConfig dataclasses
  composer.py      — CLAUDE.md/bot.conf/.mcp.json/systemd unit generation
  loader.py        — Library file loading, frontmatter parsing, heading demotion
  validator.py     — Fleet validation (env vars, MCP refs, scope checks)
  newbot.py        — Interactive bot scaffolding wizard
  newskill.py      — Skill directory scaffolding
  newguardrail.py  — Guardrail file scaffolding
  diff.py          — Drift detection and promotion
  dotenv.py        — .env file handling
  paths.py         — Path resolution helpers
  doctor.py        — Pre-flight fleet health diagnostic
  status.py        — Fleet health dashboard (tmux/systemd/fleet-state)
  uptime.py        — Per-bot uptime, MTBR, restart-rate metrics
```
