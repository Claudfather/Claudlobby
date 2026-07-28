# claudlobby

Compositor for Claude Code agent fleets. Transforms `fleet.yaml` + `library/` into runnable bot directories with isolated identities, MCP servers, skills, and systemd/launchd supervision.

**North star:** Trivial to run a fleet of distinct, cooperating bots on cheap hardware — and to point that fleet at a goal (fleets know the mission they serve, pick work that advances it, and close it at each project's declared rigor).

**New here?** See [`documentation/getting-started.md`](documentation/getting-started.md) for the clone-to-fleet walkthrough and [`documentation/fleet-yaml-schema.md`](documentation/fleet-yaml-schema.md) for every config field.

## Ecosystem boundary

Claudlobby is the stack's **composition system**: it turns `fleet.yaml` into wired bots —
identities, plugins, env, permission grants, supervision — and owns fleet *policy* (what each bot
may do, including vault writer topology). Engineering-workflow behavior is clauDNA's; the durable
knowledge corpus is Claudron's. The local rules:

- **Skills here are fleet operations.** `library/skills/` operate the fleet itself (dispatch,
  restart, pulse) — runtime content that happens to use the skill format. Engineering-workflow
  skills belong in clauDNA.
- **Durable knowledge lives in the vault.** `library/` composes context into bots; it is not the
  corpus. Learned-the-hard-way content accrues through the Claudron door (`/claudna:capture`),
  not as new library files (see `library/CLAUDE.md`).
- **Consume siblings by contract, never by assertion.** Wire what is shipped (the `claudron` CLI
  door; the `claudron_compat.py` floor); never validate or warn about a sibling surface that does
  not exist at the pinned version. The canonical vault env var is `CLAUDRON_VAULT_PATH` (Claudron
  CLI contract).
- **Placement test** (one line): does it *wire, grant, or supervise*? → here. Behavior → clauDNA;
  knowledge → the vault. Full algorithm: Claudron repo,
  `documentation/plans/2026-07-20-claudfather-boundary-separation.md` §10.3.

## Architecture

```
fleet.yaml          →  claudlobby generate  →  runtime/bots/<name>/
library/                                         ├── CLAUDE.md      (composed instructions)
  expertise/                                     ├── bot.conf       (env vars, sourced at startup)
  skills/                                        ├── .mcp.json      (MCP server config)
  mcp/                                           ├── .claude/       (settings.local.json, skills/ symlinks)
  guardrails/                                    ├── *.service      (systemd unit, Linux)
  protocols/                                     ├── *.plist        (launchd unit, macOS)
  integrations/                                  ├── memory/        (bot-owned persistent state)
  resources/                                     ├── data/          (bot-owned data — mutable, never regenerated)
  lessons/                                       ├── tools/         (composited scripts — generated, never hand-edited)
  principles/                                    ├── logs/          (bot log files)
  permissions/                                   └── projects/      (git checkouts, gitignored)
  post_actions/
  tools/
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
- **Tools** — Composited bot scripts in `library/tools/<name>/` (`tool.yaml` + Jinja template), rendered per-bot into `<bot_dir>/tools/` (0755) with compose-time params; secrets stay runtime env reads. See `library/tools/README.md`.
- **Plugins** — Claude Code plugins installed fleet-wide. `claudna@Claudfather` is a built-in default; extras via `fleet.plugins.additional`. Auto-installed on bot start.
- **Voices** — Optional personality overlays from `voices/`.

### Runtime model

Bots run as supervised processes: systemd user units on Linux, launchd LaunchAgents on macOS. Each bot lives in its own tmux session on its **own** tmux server (a private `-L <socket>` == `BOT_SERVICE`), so one server's death drops only that bot, never the whole fleet. The manager dispatches work via the socket-aware `lib/dispatch.sh` helper (which resolves the worker's socket); workers report back via `lib/report-back.sh`.

Key lifecycle scripts in `lib/`:

| Script | Purpose |
|--------|---------|
| `start-bot.sh` | Launches a bot's tmux session with env vars from bot.conf |
| `spin-up-bot.sh` | Idempotent: enrolls bot as supervised service, then starts |
| `spin-down-bot.sh` | Inverse of spin-up: guaranteed teardown/reaper for canary/throwaway bots — removes supervision, kills the tmux server, drops the bot's fleet-state key; `--purge` also removes the bot dir. Emits a `bot_teardown_started` receipt (action/actor/fleet/bot_dir/expected_return/reason) to the **fleet** ledger so it outlives `--purge`, written *before* the legs run so a crash mid-teardown still leaves a record. **Dormant by default** — `lib/` is a shared install that cannot be staged per-bot, so a fleet arms the receipt with `SPINDOWN_RECEIPT_ENABLED=1` in its `fleet.yaml` `env:` rather than a root-pull silently activating new behavior on a destructive door. Not the tool for transient RAM pressure: `systemctl --user stop` frees the same RAM and keeps the bot enrolled |
| `keepalive.sh` | Per-bot watchdog — restarts if tmux session dies |
| `keepalive-all.sh` | Fleet-level watchdog — runs keepalive for all bots |
| `reconcile-fleet.sh` | Audits supervision state: healthy, orphan, missing, unsupervised-down, unbound |
| `report-back.sh` | Worker → manager structured reporting via tmux |
| `fleet-state-update.sh` | Atomic state/fleet-state.json updates with flock locking |
| `workstream-update.sh` | Single-writer mutator for the per-fleet `workstreams.json` registry (open/progress/renew/block/close/prune); reads go through `claudlobby workstreams` |
| `pre-stop-handoff.sh` | Graceful context handoff before service stop |
| `lib-common.sh` | Shared helpers: OS detection, bot.conf loading, safe mktemp |
| `log-rotate-fleet.sh` | Fleet-wide log rotation |
| `log-rotate.sh` | Single-bot log rotation |
| `git-pull-all.sh` | Pull all repos in a bot's projects/ directory |
| `tg-post.sh` | Bash helper for posting to Telegram |
| `disk-monitor.sh` | Daily disk-usage check, FLEET ALERT past threshold — runs as the `disk-monitor` host job |
| `fleet-memory-check.sh` | Daily fleet RSS vs available-RAM check, FLEET ALERT past reserve floor — runs as the `fleet-memory-check` host job |
| `transcript-digest.sh` | SessionEnd hook — distils each finished session into one structured JSONL row via a cheap Haiku pass (#785 Phase A ingestion; the ai-platform monitor reasons over digests, never raw transcripts). Two-stage input reduction: distil to user/assistant text + tool *names* (drops tool results/attachments — 36× on a real transcript), then tail-cap. Schema reuses clauDNA `capture`'s session-mode rubric (context/worked/failed/would_change/reusable) — shared schema, not shared mechanism; SessionEnd is not clauDNA's to hook (clauDNA #203). A session below the qualifying gate still gets a `skipped` row at zero model cost, distinct from an `ok` row with empty fields (the model finding nothing notable). Non-blocking: every failure path still writes a row and exits 0. Composed fleet-wide via `system.yaml` `defaults.hooks.SessionEnd` but **dormant by default** — a fleet arms it with `SESSION_DIGEST_ENABLED=1` in its `fleet.yaml` `env:`, so rollout is per-fleet rather than estate-wide on the next generate |
| `orphan-browser-reaper.sh` | Daily reap of browser processes a dead automation task orphaned (#807) — matches on the `comm` basename, never a cmdline pattern; "orphaned" means the parent is init *or a subreaper* (systemd `--user` adopts what a bot session orphans, so `ppid == 1` alone misses the common case); reaps the whole descendant tree, FLEET NOTICE on reap. `--dry-run` reports without killing or notifying. Runs as the `orphan-browser-reaper` host job |
| `bench-cold-start.sh` | Cold-start timing benchmark — logs CSV rows to `bench-results.log` (no automated regression detection) |
| `check-npx-cache.sh` | Verify npx package cache state for MCP servers |
| `sprint-trigger.sh` | Schedule-driven autonomous sprint nudger |
| `briefing-trigger.sh` | Fire a bot's scheduled briefing as a real slash command — run by the composer-generated per-(bot,slot) briefing timer |
| `creds-check.sh` | Credential validation for fleet secrets |
| `bot-sweep-cron.sh` | Periodic bot sweep via cron |
| `code-audit-sweep.sh` | No-LLM rolling code-audit selector — picks the stalest repo via GitHub `auto-audit` issues, hands off to the owner bot's session — runs as the opt-in `code-audit-sweep` fleet job |
| `install-code-audit-sweep.sh` | Code-audit-sweep timer enrollment (launchd, thin wrapper) |
| `install-code-audit-sweep-systemd.sh` | Code-audit-sweep timer enrollment (systemd, thin wrapper) |
| `install-bot.sh` | Bot service enrollment (launchd) |
| `install-bot-systemd.sh` | Bot service enrollment (systemd) |
| `install_fleet_timer.sh` | Generic fleet/host timer enrollment (systemd) — copies composed units + enables |
| `install_fleet_timer_launchd.sh` | Generic fleet/host timer enrollment (launchd) — copies composed plist + bootstraps |
| `install-keepalive-systemd.sh` | Keepalive timer enrollment (systemd, thin wrapper) |
| `install-cron.sh` | Cron job installation |
| `install-creds-check-systemd.sh` | Creds-check timer enrollment (systemd, thin wrapper) |
| `install-fleet-pulse-systemd.sh` | Fleet-pulse systemd timer enrollment (systemd) |
| `setup-system` | Setup backbone: host prereqs + system.yaml host-job enrollment (cross-platform) |
| `setup-fleet` | Setup backbone: per-fleet apply+enroll — default jobs (dormant opt-ins skipped), atomic legacy-keepalive swap (enable-new → verify → disable-old), bots (skips healthy), reconcile; root mode when invoked without a fleet |
| `setup-fleets` | Run setup-fleet for every fleet on the host |
| `bot-vitals.sh` | Bot vitals collection for observability |
| `fleet-pulse.sh` | Fleet-wide heartbeat / status monitoring |
| `tail-fleet.sh` | Fleet-wide log tail + grep filter |
| `ci-health-check.sh` | Pre-push CI health canary for target branch |
| `data-sweep.sh` | Weekly per-bot data/ ephemeral purge — only `events/*.jsonl`, vetted text-log names, and `*.bak` age out (30d default, fleet-overridable); durable files are never swept — runs as the `data-sweep` fleet job |
| `dispatch.sh` | Dispatch helper for manager → worker |
| `dispatch-task.sh` | Task dispatch helper |
| `dispatch-overdue.py` | Finds overdue dispatches — the matcher behind the `fleet-pulse.sh` watchdog (age-capped via `DISPATCH_OVERDUE_MAX_AGE_S`) |
| `telegram-instant-ack.sh` | Telegram instant acknowledgment for inbound messages |
| `fleet-utilization.sh` | Fleet utilization rollup — per-bot busy/idle % |
| `transcript-usage.py` | Per-session token accounting from Claude Code transcripts — sums the four `message.usage` components (flat, never `iterations[]`) over assistant turns, splits sidechains, emits `protocol_sensitive` + `cost_weighted_total` axes and an outbound-comms estimate (`--comms-share`). Prize-sizing instrument for the token-efficiency protocol (#716/#729); #717 extends it |
| `ab-comms-eval.sh` | #729 stage-C A/B comms-eval harness (scaffolding) — runs the same comms-heavy battery against two identical bots differing only in `protocols: [token-efficiency]`, composed by real `generate`, and sums the two gated axes (`protocol_sensitive` + `cost_weighted_total`) per variant via `transcript-usage.py`. Task content + quality rubric are F2 stubs; `--dry-run` is CI-safe (synthetic transcripts, zero model calls); real mode (`AB_EVAL_REAL=1`) is refused pending F2 + P1. Wrapped by `tests/test_ab_comms_eval.py` |
| `ab-comms-verdict.py` | #729 stage-C pass-bar / verdict for `ab-comms-eval.sh` — pairs per-rep deltas by task, runs a seeded bootstrap CI on the median relative reduction per axis, and applies the cost-weighted-co-primary, per-task, INCONCLUSIVE-never-PASS bar (no F2-ratified threshold → INCONCLUSIVE by construction). Standalone stdlib module (`dispatch-overdue.py` precedent) so F2 extends the threshold + scorer in a unit-tested place |
| `validate-bot-change.sh` | Empirical validation harness for bot behavior changes |
| `freshbox-boot-gate.sh` | #644 P4 real-boot gate — boots a scoped bot on a fresh empty `CLAUDE_CONFIG_DIR` (auth+trust seeded before first contact) and asserts the composed perms hold: clean boot, zero prompts, transcript ⊆ allow. Gated job (deps absent → skip); wrapped by `tests/test_freshbox_boot_harness.py` (opt-in `FRESHBOX_REALBOOT=1`) |
| `rehearse-keepalive-swap.sh` | Phase 6 gate 1 — rehearse the atomic legacy-keepalive swap on a throwaway fleet with real 60s timers; journal-derived no-gap assertion |
| `update-claude-code.sh` | Daily Claude Code binary download (download-only; no fleet bounce) — runs as the `claude-update` host job (system.yaml `host.jobs`, enrolled by `setup-system`) |
| `notify-behind.sh` | Daily source-currency nudge — FLEET NOTICE when the install is N commits behind origin/main (notify-only, never pulls) — runs as the `notify-behind` host job |
| `reload-fleet.sh` | Daily live plugin/skill reload — `claude plugin update` + generate, then mark running bots for a keepalive-driven `/reload` (no restart) |
| `install-reload-fleet-systemd.sh` | Reload-fleet daily timer enrollment (systemd) |
| `weekly-worker-restart.sh` | Weekly lossless restart of worker bots (managers excluded) to apply a staged binary |
| `install-weekly-worker-restart-systemd.sh` | Weekly-worker-restart timer enrollment (systemd) |
| `rolling-restart.sh` | Serial fleet restart — bots one at a time, each gated on a fresh Telegram `BRIDGE_READY` before the next, so a mass restart cannot land the fleet inbound-dead (#689) |
| `host-health-check.sh` | Host-hardware early-warning — FLEET ALERT on under-voltage/thermal throttling (Pi `vcgencmd`) and SD/MMC storage stalls (kernel log), de-duped via a state file — runs as the `host-health-check` host job |
| `migrate-fleet-to-system.sh` | Staged, reversible migration of a flat `local/<fleet>/` into a nested vault system container (`local/<system>/<fleet>/`), re-pointing every bot supervision unit at the new path |
| `rehearse-briefing-timer.sh` | Rehearse the composed briefing-timer chain on a throwaway fleet — asserts the equippable `<prefix>.briefing-<bot>-<slot>` unit fires and that removing a slot prunes its units on both platforms; sibling of `rehearse-keepalive-swap.sh` |

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
6. No apostrophes in comments inside `$( )` — bash 3.2 (macOS `/bin/bash`, the shebang target) does not strip comments while scanning a command substitution, so a stray apostrophe corrupts quoting for the rest of the file (gate: `tests/test_bash_parse.py`)

### Validating changes to how a bot behaves — MANDATORY

Any change that affects **how a bot behaves at runtime** (lib/ supervision & observability scripts, hooks, skills, protocols, guardrails, principles, composed `bot.conf` env) must be **empirically validated** before merge. Unit tests prove *composition* — that the env var lands in `bot.conf`. Only running the code proves *behavior* — that the event actually fires, the alert actually sends, the bot actually does the thing. Follow the loop:

1. **Deliver** — make the code/library change.
2. **Add config** — set the relevant field(s) in `fleet.yaml`.
3. **Recompose** — `claudlobby --fleet <fleet> generate`; confirm the change landed in the composed `bot.conf` / `settings.local.json` / `CLAUDE.md`.
4. **Observe** — run it and watch the real behavior:
   - For observability/trust-loop behaviors: `bash lib/validate-bot-change.sh` stands up a throwaway bot + tmux sessions and asserts the events fire end-to-end. Extend it when you add a new event/check.
   - For other behavior: spin a bot (`lib/spin-up-bot.sh`), drive the affected path, and watch `data/events/*.jsonl` / `keepalive.log` / the pane.

**Cite the observation in the PR body** ("ran `validate-bot-change.sh` → activity_stuck + overdue_dispatch fired; manager notified") — claimed evidence is not evidence. This is also how latent bugs surface: the harness above caught a `fleet-pulse.sh` sweep-abort that every unit test missed.

**This gate proves the code; it does not prove the rollout.** Clearing it is mandatory for every runtime change. Separately, when a change to the framework itself (claudlobby, clauDNA, claudron) ships **live fleet-wide** — supervision/`lib` scripts, plugins, the bridge, composed `bot.conf` — the manager should *by default* canary the rollout on one production bot before rolling the fleet: a strong default for fleet-wide framework changes, not a universal mandate (skip it for single-bot, product-repo, or non-runtime work). See the `canary-rollout` protocol.

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
claudlobby host-timers                 # compose host-global timer units from system.yaml
claudlobby diff                        # show drift between runtime and generate
claudlobby promote <name>              # extract bot drift back into library
claudlobby list-library                # show available building blocks
claudlobby new-bot                     # interactive bot scaffolding

# Operations
claudlobby status                      # fleet health dashboard
claudlobby status --bot <name>         # detailed status for one bot
claudlobby doctor                      # pre-flight fleet health diagnostic
claudlobby freshbox                    # fresh-box self-containment audit (over-grant/orphan, denied source values, externals report, fleet-tier .env, rendered tools/; --strict, --bot, --reap)
claudlobby report-back                 # query bot work event ledger
claudlobby report-back --since 24h     # filter by time window
claudlobby uptime                      # per-bot uptime, MTBR, restart-rate
claudlobby events                      # tail/filter JSONL events across all bots
claudlobby workstreams [list|show <id>] # read-only fleet workstream registry
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
claudlobby lessons-migrate             # migrate referential library/lessons/ into the Claudron vault (dry-run by default)

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
  __main__.py         — Thin CLI entry point (~55 lines); argparse setup + subcommands live in commands/
  commands/           — CLI command implementations: argparse registration, core ops, migrations, scaffolding, move-bot, events (12 files)
  config.py           — fleet.yaml parsing, BotConfig/FleetConfig dataclasses
  known_values.py     — Known-good value sets for fleet.yaml fields (SSOT for config + validator)
  composer.py         — CLAUDE.md/bot.conf/.mcp.json/systemd unit generation
  mcp_resolve.py      — MCP fragment ${VAR} env-var / instance resolution (shared by composer + validator)
  tool_resolve.py     — Library tools manifest/template/param resolution (shared by composer + validator)
  loader.py           — Library file loading, frontmatter parsing, heading demotion
  validator.py        — Fleet validation (env vars, MCP refs, scope checks)
  newbot.py           — Interactive bot scaffolding wizard
  newskill.py         — Skill directory scaffolding
  newguardrail.py     — Guardrail file scaffolding
  prompts.py          — Shared interactive prompt helpers for the scaffolding wizards
  diff.py             — Drift detection and promotion
  dotenv.py           — .env file handling
  paths.py            — Path resolution helpers
  doctor.py           — Pre-flight fleet health diagnostic
  freshbox.py         — Fresh-box self-containment audit (#644 P4): over-grant/orphan + under-grant + Tier-A composed-not-inherited; deny-by-default rungs (#703): denied source values, externals report + unused-declaration WARN, fleet-tier .env guard, rendered tools/ (backs `claudlobby freshbox`)
  status.py           — Fleet health dashboard (tmux/systemd/fleet-state)
  uptime.py           — Per-bot uptime, MTBR, restart-rate metrics
  utilization.py      — Fleet utilization rollup — per-bot busy/idle % over rolling windows
  path_audit.py       — L1 path/grant provenance audit (source-side ownership + external_paths)
  conformance.py      — L4 conformance gates (rename-map drift, boundary invariants)
  workstreams.py      — Read-only view of the per-fleet workstream registry (workstreams.json)
  claudron_compat.py  — Claudron compatibility floor — min capability per integration surface
```
