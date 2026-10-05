# claudlobby

Compositor for Claude Code agent fleets. Transforms `fleet.yaml` + `library/` into runnable bot directories with isolated identities, MCP servers, skills, and systemd/launchd supervision.

**North star:** Trivial to run a fleet of distinct, cooperating bots on cheap hardware — and to point that fleet at a goal (fleets know the mission they serve, pick work that advances it, and close it at each project's declared rigor).

**What that is for:** specialist agent teams that take on the work a team of people would otherwise do. Such a team **communicates in plain terms to the human while keeping the rigor behind it intact** — simplified for the reader, never simplified in the doing (`library/principles/simple-outside-rigorous-inside.md`). It works toward a business goal with enough autonomy to parallelize its own workstreams and fill its own idle time, and sustains that over days rather than a session. **Long-running is the precondition for the rest**: a fleet that cannot survive a restart with its context intact cannot hold a goal. Tracked as #974.

**New here?** See [`documentation/getting-started.md`](documentation/getting-started.md) for the clone-to-fleet walkthrough and [`documentation/fleet-yaml-schema.md`](documentation/fleet-yaml-schema.md) for every config field.

**This file is an index.** Detail lives beside what it describes: [`claudlobby/_runtime_scripts/CLAUDE.md`](claudlobby/_runtime_scripts/CLAUDE.md) (each runtime script in depth, the contracts of the public-CLI commands that replaced scripts, and the shell authoring rules), [`harness/CLAUDE.md`](harness/CLAUDE.md) (validation and measurement instruments), the other nested `CLAUDE.md` files, script headers, and `documentation/`. Read "Instruction files" below before adding to this one.

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
fleet.yaml          →  config plan + host activate  →  runtime/bots/<name>/
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

Bots run as supervised processes: systemd user units on Linux, launchd LaunchAgents on macOS. Each bot lives in its own tmux session on its **own** tmux server (a private `-L <socket>` == `BOT_SERVICE`), so one server's death drops only that bot, never the whole fleet. The canonical `claudlobby task admit` / `task assign` commands record work; `assignment deliver` sends the committed assignment, and `assignment progress|block|return|complete|fail` records a linked worker report. `message send` and `fleet reports submit` handle ordinary and unlinked communication.

**Changing a running fleet:** when a change reaches a bot depends on **when the artifact is read, not its file type** (#1310). Read **once at session start** (composed `CLAUDE.md` text, `bot.conf` env, `.mcp.json`): a running bot sees it from its next restart, so a canary window exists. Read **on demand, per use** (skill symlinks, hook scripts, `claudlobby/_runtime_scripts/`): live on every bot once activation publishes it, with **no canary window**. Composed `settings.local.json` permissions apply from the next tool call, so they too have no canary window. See [`documentation/fleet-update-lifecycle.md`](documentation/fleet-update-lifecycle.md) before assuming a merged change is in force.

**Defaults (ruled 2026-09-07):** a job or door is **ON by default** unless it *deletes data, spends money, mutates operator source, sends outbound to people at scale, or has no deployment gate*. The registry is the list: `claudlobby/switches.py` gives each opt-in's reason, and `claudlobby host doctor --switches` prints the live table and the line that arms each. A door turned off no-ops **loudly**.

### Runtime scripts (`claudlobby/_runtime_scripts/`)

One line per script, for routing; operators use the public CLI. **Before changing one, read [`claudlobby/_runtime_scripts/CLAUDE.md`](claudlobby/_runtime_scripts/CLAUDE.md)**: the shell authoring rules and each script's full reference. Validation and measurement instruments live in `harness/`, indexed in [`harness/CLAUDE.md`](harness/CLAUDE.md).

**Bot lifecycle and supervision**
- `start-bot.sh` — launch a bot's tmux session on its own socket and wait until ready
- `spin-up-bot.sh` — enroll a bot as a supervised service, then start it (idempotent)
- `spin-down-bot.sh` — full teardown for canary/throwaway bots; `--purge` also deletes the bot dir
- `pre-stop-handoff.sh` — graceful context handoff before a service stop
- `keepalive.sh` — per-bot watchdog: restarts a dead session and records heartbeat samples
- `keepalive-all.sh` — run keepalive for every bot
- `reconcile-fleet.sh` — audit supervision state: healthy, orphan, missing, unsupervised-down, unbound
- `supervisor.sh` — systemd/launchd adapter; the one door for new `systemctl`/`launchctl` calls (other files are ratcheted at their current count)
- `supervisor-caller.py` — kernel ancestry check for the adapter, so a bot cannot stop its own coordinator
- `bot-unit-owner.py` — reads a unit's working directory for the adapter without running it; foreign units are left alone, unreadable ownership refuses
- `runtime-admission.sh` — startup and watchdog release check, and the activation lock
- `rolling-restart.sh` — restart bots one at a time, each gated on a fresh Telegram `BRIDGE_READY`
- `weekly-worker-restart.sh` — weekly lossless restart of workers (not managers) onto the staged binary (opt-in)
- `reload-fleet.sh` — daily plugin refresh for the selected fleet, then marks running bots for an idle `/reload` (no restart; never changes authored config)
- `install-bot.sh` — bot service enrollment (launchd)
- `install-bot-systemd.sh` — bot service enrollment (systemd)

**Shared helpers and query doors**
- `lib-common.sh` — one door each for pane sends (`pane_send_verified`), fleet events (`emit_fleet_event`), switches (`switch_is_on`); `declared_bots_strict` where empty would license a write
- `cli-context.sh` — checks an adapter's explicit data root and CLI path; never falls back to PATH
- `env-tiers.sh` — print the `.env` tier cascade (host, root, fleet, bot) in runtime order
- `claude-version.sh` — print the Claude Code version, or refuse at rc 3
- `claude-session-pid.sh` — which Claude Code session the caller runs inside (walks its own ancestry)
- `fleet-state-update.sh` — atomic, flock-locked updates to `state/fleet-state.json`
- `tg-post.sh` — post a message to Telegram
- `mcp-package-grammar.py` — how an MCP server's `command` + `args` name a package, in one place

**Dispatch and the task loop**
- `dispatch.sh` — manager → worker dispatch helper; resolves the worker's tmux socket
- `dispatch-overdue.py` — the plane matcher behind fleet-pulse: overdue, orphaned, open, unassigned rows
- `issue-intake.py` — lists the issues a skill may take (author, or whoever applied the trust label, can triage the repo); `quote` wraps issue text as data
- `briefing-trigger.sh` — fire a bot's scheduled briefing as a slash command
- `manager-checkin.sh` — the check-in beat: prompts an idle manager to pick its next move (opt-in)

**Observable plane (the fleet's record)**
- `plane-emit.sh` — private recording adapter: sends to the plane daemon, else stages durably and reports uncommitted (never a cold CLI)
- `plane-socket-client.py` — stdlib socket leg of `plane-emit.sh`
- `plane-daemon.sh` — launcher for the ingest-only plane daemon
- `plane-view.sh` — launcher for the read-only operator plane UI (localhost)
- `plane-prune.sh` — timer: deletes metric samples past 30 days (opt-in: also an allowlist of system events), never the ledger
- `plane-expire.sh` — timer: expires assignments 7+ days past deadline
- `plane-host-probe.sh` — per-minute host samples: load, RAM, disk, swap, iowait, thermal
- `plane-lookup.py` — read-only plane lookups by task id, assignment, escalation, event
- `plane-readers.py` — stdlib list readers shared by the bash doors
- `plane-session-start.sh` — SessionStart hook: session and process ids for the plane
- `plane-dispatch-in.sh` — UserPromptSubmit hook: records what the receiving bot actually got
- `plane-telegram-in.sh` — UserPromptSubmit hook: records the operator's Telegram messages
- `plane-telegram-out.sh` — PostToolUse hook on the Telegram reply tool: records the reply
- `plane-rc-relay-out.sh` — Stop hook: records a Telegram answer sent without the reply tool

**Monitoring and alerts**
- `fleet-pulse.sh` — fleet watchdog: overdue dispatches, idle workers (opt-in), critical events, escalations
- `bot-vitals.sh` — Pre/PostToolUse hook: records tool calls
- `tail-fleet.sh` — tail and grep every bot's logs
- `disk-monitor.sh` — daily disk check; FLEET ALERT past threshold
- `fleet-memory-check.sh` — daily fleet RSS vs available RAM; FLEET ALERT past the reserve floor
- `host-health-check.sh` — alert on Pi under-voltage/throttling and SD/MMC storage stalls
- `creds-check.sh` — validate fleet credentials, including GitHub App mode
- `orphan-browser-reaper.sh` — Linux-only reap of browser processes orphaned by dead automation
- `notify-behind.sh` — daily report of framework checkouts behind their newest release
- `transcript-digest.sh` — SessionEnd hook: a model-written digest per session (opt-in: spends)
- `selfstart-snapshot.sh` — after an unplanned reboot, counts which bots self-started; run it before any rescue
- `boot-capture.sh` — records every declared bot at each host boot (opt-in)

**Maintenance and updates**
- `log-rotate.sh` — rotate one bot's logs
- `log-rotate-fleet.sh` — rotate every bot's logs
- `git-pull-all.sh` — pull every repo in a bot's `projects/`
- `data-sweep.sh` — weekly purge of vetted ephemeral files in bots' `data/`
- `check-npx-cache.sh` — check that MCP server packages are cached
- `update-claude-code.sh` — daily Claude Code update, no fleet bounce; in place by default (a failed install leaves no runnable `claude`), staged only when armed (opt-in)
- `update-siblings.sh` — weekly fast-forward of sibling checkouts to their newest release (opt-in)
- `bot-sweep-cron.sh` — periodic bot sweep via cron
- `code-audit-sweep.sh` — picks the stalest repo for a code audit, hands it to its owner (opt-in)
- `vault-sync.sh` — scheduled vault sync that reports what happened (opt-in)

**GitHub identity and tool-call guards**
- `git-credential-github-app` — git credential helper that mints GitHub App installation tokens
- `mint-github-token.sh` — private helper printing a fresh App token for hot-path scripts; never export it at boot
- `github-app-mcp-wrapper.py` — runs the GitHub MCP server with auto-refreshed App tokens
- `setup-github-app.sh` — one-time App validation and config write
- `gh-mention-guard.sh` — PreToolUse hook: rewrites `@<botname>` out of GitHub-bound text
- `mention-rewrite.py` — the rewriter behind `gh-mention-guard.sh`
- `vault-git-guard.sh` — PreToolUse hook: stops bots rewriting git state inside the vault
- `vault-git-decide.py` — the decision half of `vault-git-guard.sh`
- `credential-echo-guard.sh` — PreToolUse hook: refuses a CLI form that prints an env-held credential unless the variable is removed in the same command (#2090)
- `credential-echo-decide.py` — the decision half of `credential-echo-guard.sh`
- `heavy-slot-guard.sh` — PreToolUse hook: queues heavy Bash commands (suites, installs, builds) on the host's heavy-job slot (opt-in)
- `heavy-slot.py` — the heavy-job slot: `hook` finds heavy commands, `run` holds a slot or queues a ticket, `status` names holders and the queue
- `public-write-guard.sh` — PreToolUse hook: refuses a GitHub-bound write that would put a term from the host's list into a public repository (opt-in)
- `public-write-guard.py` — the decision half of `public-write-guard.sh`

## Repository Hygiene — MANDATORY

### What goes in git (shared, reusable, generalized)

Everything in these top-level directories is committed and shared:

- `library/` — All composable building blocks (see Architecture above)
- `voices/` — Personality overlays
- `templates/` — Jinja2 templates for CLAUDE.md generation
- `claudlobby/_runtime_scripts/` — Lifecycle and utility scripts
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

### Instruction files: CLAUDE.md and AGENTS.md

Claude Code reads `CLAUDE.md`; Codex reads `AGENTS.md`. Each `AGENTS.md` is a **committed byte-for-byte copy of the `CLAUDE.md` beside it**, and each `.agents/skills/<name>/` a copy of `.claude/skills/<name>/` (copies, because `tests/prepare_resources.py` refuses symlinks). Edit the Claude-side file, then copy it over (`cp CLAUDE.md AGENTS.md`); never reword the copy.

Every session started in this checkout loads this file whole, and so does every bot under it, since Claude Code reads each parent directory's `CLAUDE.md`. Nested files load on use only. Claude Code reads one when a session opens a file in its folder, if that folder is under where the session started (so bots never do). Codex reads the `AGENTS.md` chain from the root to its start folder within one 32 KiB budget (measured), so a nested file gets only what the files above it leave; tell Codex to read it. `tests/test_instruction_budget.py` enforces the copies, the indexes and both budgets: the chain through each nested file's rules (up to its `## Script reference`) fits in 32 KiB, and each nested file stays under Claude Code's 150k-character warning. Keep one line per thing here; a script's history goes in its own header.

### Adding library content

Each library category has its own format. Check the category's `README.md` for specifics. General rules:

1. Create the file in the appropriate `library/<category>/` directory
2. Use YAML frontmatter with `title:` and `description:` fields
3. Add an H1 heading (`# Title`) matching the frontmatter title — the loader strips it to avoid duplication in composed output
4. Use `{{BOT_NAME}}`, `{{FLEET_NAME}}`, `{{CLAUDLOBBY_ROOT}}` Jinja2 placeholders where appropriate
5. Stage a configuration plan in the independent canary root and verify the intended bot's rendered CLAUDE.md before operator activation.
6. Commit to a branch, PR, review

**Heading levels matter.** The template renders library content inside `##`/`###` sections. The loader runs `_demote_headings` to shift all headings down. An H1 (`#`) in your file becomes H2 in the output. If you start with H3, it becomes H4 — which may be too deep.

### Adding compositor features

1. Edit Python source in `claudlobby/`
2. In a disposable checkout with private HOME/TMPDIR, install the dev dependencies, prepare resources with `.venv/bin/python tests/prepare_resources.py --disposable-checkout "$PWD"`, then run the affected existing tests (details below).
3. Test against your local fleet: `claudlobby --fleet <name> config validate` then stage `config plan --release RELEASE_ID` and inspect `config diff PLAN_ID`
4. Run `claudlobby --fleet <name> config diff PLAN_ID` to verify no unintended rendered drift
5. Commit to a branch, PR, review

**Before writing or trusting a test, read [`documentation/test-suite.md`](documentation/test-suite.md).** Never test from a live fleet root; run unsandboxed; the suite is not green, so compare two separately prepared exports (before, after) on failing test **names**, the **counts** line and the **exit code**; never pipe pytest into grep; quarantine a flaky test (`@pytest.mark.quarantine(issue=<N>)`), never deselect it.

### Adding or modifying claudlobby/_runtime_scripts/ scripts

Read [`claudlobby/_runtime_scripts/CLAUDE.md`](claudlobby/_runtime_scripts/CLAUDE.md) first: the authoring rules and each script's full reference. Two rules reach beyond it: no apostrophes in comments inside `$( )` (bash 3.2; `tests/test_bash_parse.py` also checks `library/**/*.sh`), and an empty roster must never license a write or delete: there use `declared_bots_strict`, never `parse_fleet_bots` or `bot_in_fleet` (#1146). A new script gets one line in the index above; its detail goes in its own header.

### Validating changes to how a bot behaves — MANDATORY

Any change that affects **how a bot behaves at runtime** (claudlobby/_runtime_scripts/ supervision & observability scripts, hooks, skills, protocols, guardrails, principles, composed `bot.conf` env) must be **empirically validated** before merge: unit tests prove composition, only running the code proves behavior. **Deliver** → **add config** to the canary's `fleet.yaml` → **stage it in an independent canary root** (its own labels, `unit_prefix`, Plane state and channels; `host setup`, `config plan --release RELEASE_ID`, `config diff PLAN_ID`; never a production root) → **observe** the real behavior (`bash harness/validate-bot-change.sh` for observability events; otherwise drive the path with the canary's CLI and watch `event list --bot BOT`). **Cite the observation in the PR body**: claimed evidence is not evidence. Ground a fixture for an externally produced shape in a **live capture**, never in the producer's source, committing its shape but never its identifiers. This gate proves the code, not the rollout: separately, the manager validates an independent canary root before coordinated production activation by default (skip for single-bot, product-repo or non-runtime work; the `canary-rollout` protocol). Full text: [`documentation/validating-bot-changes.md`](documentation/validating-bot-changes.md).

### Validating changes to the onboarding path — MANDATORY

Any change to **what a brand-new user is told to run** — `README.md`, `documentation/getting-started.md`, `.claude/skills/setup/SKILL.md`, `claudlobby host setup`, `fleet.yaml.seed`, `.env.seed.example` — must be validated **on a cold host**, not from your checkout (#947): **export, do not clone**; **scrub the environment first**; **run the documented commands verbatim**; **log every exploration event** and report the count; **stop at the credential gate**. `tests/test_cold_start_contract.py` is a floor, not a substitute. **Cite the cold run in the PR body.** Procedure: [`documentation/validating-cold-start.md`](documentation/validating-cold-start.md).

### Never hand-edit generated output

Files in `runtime/bots/<name>/` are composed from a configuration plan. Hand-edits are overwritten by activation. To change a bot's config:

1. Edit `fleet.yaml` (fleet-level config) or `library/<category>/` (content)
2. Stage `claudlobby config plan --release RELEASE_ID`, inspect `config diff PLAN_ID`, then have the operator run `host activate PLAN_ID --install-directory PATH`
3. If the bot drifted during a session (`claudlobby config diff --bot <name>` shows changed files), review the changes and edit their authored source in `library/`

## Key Commands

```bash
# Composition
claudlobby config validate  # check fleet.yaml against library
claudlobby config plan --release <RELEASE_ID>  # stage all declared host fleets
claudlobby config diff [<PLAN_ID>]  # rendered drift, or a staged plan's paths and digests
claudlobby host activate <PLAN_ID> --install-directory <PATH>  # operator applies the staged host
claudlobby library list  # available building blocks
claudlobby bot create --interactive  # author a bot; stage and activate separately

# Operations
claudlobby bot start <bot>  # start a declared bot through the lifecycle owner
claudlobby fleet start  # enroll and start the selected fleet's bots
claudlobby fleet reconcile  # audit the fleet's supervision state
claudlobby fleet status  # fleet health dashboard
claudlobby bot status <name>  # one bot in detail
claudlobby host doctor [--switches]  # pre-flight diagnostic; --switches: every switch and its arming line
claudlobby host credentials reconcile  # declared vs stored vs equipped credentials
claudlobby config validate --runtime  # self-containment audit (--strict, --bot)
claudlobby host supervision reap-orphans --dry-run  # stale supervision units; --apply removes
claudlobby fleet reports list [--since <RFC3339>]  # the fleet's reports, from the plane
claudlobby fleet reports ack --through <ACK_CURSOR> --request-id <UUID>  # explicit report acknowledgement
claudlobby fleet uptime  # per-bot uptime, MTBR, restart rate
claudlobby event list  # the fleet's events (rc 3 when the plane cannot answer)
claudlobby workstream list  # the fleet's workstream registry
claudlobby brief --bot <name> [--json] # one read door over fleet state for a bot
claudlobby host cache warm  # pre-download npx + uvx MCP packages
claudlobby bot move <bot> --to <fleet> # move a bot between fleets

# A manager's acts on one open task (#1481), and the check-in
claudlobby --json task withdraw TASK_ID --reason "…" --request-id UUID  # terminal withdrawal
claudlobby --json task escalate TASK_ID --question "…" --request-id UUID  # non-terminal: ask a human
claudlobby --json task nudge TASK_ID --reason TEXT --request-id UUID  # record a nudge and notify the fleet manager
claudlobby --fleet <F> task recheck --request-id UUID [--dry-run]  # ask the manager about due work
claudlobby --json --fleet F checkin <list|show> [...]  # check-in decisions and outcomes
claudlobby --json --fleet F checkin record --file FILE [--selection-file FILE] --request-id UUID [--dry-run]  # commit before acting
claudlobby checkin selection <verify|focus-refs> FILE  # offline selection checks

# Scaffolding and one-time migrations (see each --help)
claudlobby library create --kind <skill|guardrail>
claudlobby migration <env|data|cron|memory|lessons|workstreams>

# After an unplanned reboot — RUN THIS BEFORE RESCUING ANYTHING
claudlobby/_runtime_scripts/selfstart-snapshot.sh  # how many bots self-started (#1002)

# Testing (the venv is required — PEP 668 refuses a bare install on Homebrew/Debian)
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[dev]'
./.venv/bin/pytest  # run test suite (unsandboxed; baseline is not green)
```

Use `--fleet <name>` for fleet reads and authoring. Configuration plans and activation cover the whole host; see `canary-rollout` for isolated validation.

## Python Package Structure

The module map (every module, what it owns and the rules behind it) is in [`documentation/architecture/module-map.md`](documentation/architecture/module-map.md). Where to start: `__main__.py` → `commands/` (the CLI), `config.py` (`fleet.yaml` parsing), `composer.py` (generation), `loader.py` (library loading), `validator.py`, `paths.py`, `switches.py` (the switch registry), and `plane/` (the observable plane; reference: `documentation/architecture/observable-plane.md`).
