# claudlobby

Compositor for agent-CLI fleets — Claude Code today, Codex through the execution adapter (#2149). Transforms `fleet.yaml` + `library/` into runnable bot directories with isolated identities, MCP servers, skills, and systemd/launchd supervision.

**North star:** Trivial to run a fleet of distinct, cooperating bots on cheap hardware — and to point that fleet at a goal (fleets know the mission they serve, pick work that advances it, and close it at each project's declared rigor).

**What that is for:** specialist agent teams that take on the work a team of people would otherwise do. Such a team **communicates in plain terms to the human while keeping the rigor behind it intact** — simplified for the reader, never simplified in the doing (`library/principles/simple-outside-rigorous-inside.md`). It works toward a business goal with enough autonomy to parallelize its own workstreams and fill its own idle time, and sustains that over days rather than a session. **Long-running is the precondition for the rest**: a fleet that cannot survive a restart with its context intact cannot hold a goal. Tracked as #974.

**New here?** See [`documentation/getting-started.md`](documentation/getting-started.md) for the clone-to-fleet walkthrough and [`documentation/fleet-yaml-schema.md`](documentation/fleet-yaml-schema.md) for every config field.

**This file is an index.** Detail lives beside what it describes: [`claudlobby/_runtime_scripts/CLAUDE.md`](claudlobby/_runtime_scripts/CLAUDE.md) (each runtime script in depth, the contracts of the public-CLI commands that replaced scripts, and the shell authoring rules), [`harness/CLAUDE.md`](harness/CLAUDE.md) (validation and measurement instruments), the other nested `CLAUDE.md` files, script headers, and `documentation/`. Read "Instruction files" below before adding to this one.

## Ecosystem boundary

What claudlobby is beside clauDNA and Claudron is in
[`documentation/architecture/overview.md`](documentation/architecture/overview.md). The local rules:

- **Skills here are fleet operations.** Engineering-workflow skills belong in clauDNA.
- **Durable knowledge lives in the vault.** Learned-the-hard-way content accrues through the
  Claudron door (`/claudna:capture`), not as new library files (see `library/CLAUDE.md`).
- **Consume siblings by contract, never by assertion.** Wire what is shipped (the `claudron` CLI
  door; the `claudron_compat.py` floor); never validate or warn about a sibling surface that does
  not exist at the pinned version. The canonical vault env var is `CLAUDRON_VAULT_PATH` (Claudron
  CLI contract).
- **Placement test** (one line): does it *wire, grant, or supervise*? → here. Behavior → clauDNA;
  knowledge → the vault. Full algorithm: Claudron repo,
  `documentation/decisions/005-claudfather-boundary-separation.md` §10.3.

## Architecture

The architecture diagram, the other key concepts, bot supervision and the canonical task commands are in [`documentation/architecture/overview.md`](documentation/architecture/overview.md).

The template `claude.md.j2` owns all top-level structure — library files supply slot content.

### Key concepts

- **MCP fragments** — JSON wire configs in `library/mcp/` with `${ENV_VAR}` placeholders. Never real tokens.
- **Tools** — Composited bot scripts in `library/tools/<name>/` (`tool.yaml` + Jinja template), rendered per-bot into `<bot_dir>/tools/` (0755) with compose-time params; secrets stay runtime env reads. See `library/tools/README.md`.

### Runtime model

**Changing a running fleet:** read timing, not file type, determines when a change reaches a bot (#1310). `CLAUDE.md`, `bot.conf` and `.mcp.json` are read at session start; changes wait for restart. Skills, hooks and runtime scripts are read on demand; publication reaches running bots immediately. Composed permissions apply at the next tool call. Validate in an independent installation before publication; see [fleet-update-lifecycle](documentation/fleet-update-lifecycle.md).

**Defaults (ruled 2026-09-07):** a job or door is **ON by default** unless it *deletes data, spends money, mutates operator source, sends outbound to people at scale, or has no deployment gate*. The registry is the list: `claudlobby/switches.py` gives each opt-in's reason, and `claudlobby host doctor --switches` prints the live table and the line that arms each. A door turned off no-ops **loudly**.

### Runtime scripts (`claudlobby/_runtime_scripts/`)

One line per script, for routing; operators use the public CLI. **Before changing one, read [`claudlobby/_runtime_scripts/CLAUDE.md`](claudlobby/_runtime_scripts/CLAUDE.md)**: the shell authoring rules and each script's full reference. Validation and measurement instruments live in `harness/`, indexed in [`harness/CLAUDE.md`](harness/CLAUDE.md).

**Bot lifecycle and supervision**
- `start-bot.sh` — launch a bot's tmux session on its own socket and wait until ready
- `bot-subreaper.py` — each bot session's child subreaper, which adopts its orphans
- `spin-up-bot.sh` — enroll a bot as a supervised service, then start it (idempotent)
- `spin-down-bot.sh` — full teardown for canary/throwaway bots; `--purge` also deletes the bot dir
- `pre-stop-handoff.sh` — graceful context handoff before a service stop
- `keepalive.sh` — per-bot watchdog: restarts a dead session and records heartbeat samples
- `compaction-slot.py` — the host's compaction lease: one compaction at a time, none while load1 is 40 or more
- `usage-limit.py` — reads a pane for a usage-limit stop and its reset time
- `usage-limit-hook.sh` — StopFailure/Stop hook: records a usage-limit stop
- `keepalive-all.sh` — run keepalive for every bot
- `reconcile-fleet.sh` — audit supervision state: healthy, orphan, missing, unsupervised-down, unbound
- `supervisor.sh` — systemd/launchd adapter; the one door for new `systemctl`/`launchctl` calls
- `supervisor-caller.py` — kernel ancestry check for the adapter, so a bot cannot stop its own coordinator
- `bot-unit-owner.py` — reads a unit's working directory for the adapter without running it
- `runtime-admission.sh` — startup and watchdog installation check, and the current update fence
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
- `plane-dispatch-in.sh` — UserPromptSubmit hook: records what the receiving bot actually got
- `plane-telegram-in.sh` — UserPromptSubmit hook: records the operator's Telegram messages
- `plane-telegram-out.sh` — PostToolUse hook on the Telegram reply tool: records the reply
- `plane-rc-relay-out.sh` — Stop hook: records a Telegram answer sent without the reply tool

**Monitoring and alerts**
- `fleet-pulse.sh` — fleet watchdog: overdue dispatches, idle workers (opt-in), critical events, escalations
- `bot-vitals.sh` — Pre/PostToolUse hook: records tool calls
- `turn-marker.sh` — UserPromptSubmit/Stop/StopFailure hook: stamps when input last arrived and when the turn ended
- `tail-fleet.sh` — tail and grep every bot's logs
- `disk-monitor.sh` — daily disk check; FLEET ALERT past threshold
- `fleet-memory-check.sh` — daily fleet RSS vs available RAM; FLEET ALERT past the reserve floor
- `host-health-check.sh` — alert on Pi under-voltage/throttling and SD/MMC storage stalls
- `creds-check.sh` — validate fleet credentials, including GitHub App mode
- `orphan-browser-reaper.sh` — Linux-only reap of browser processes orphaned by dead automation
- `browser-scan.py` — browsers outside a render scope, with the bot that started each; fleet-pulse reports them, never stops them
- `notify-behind.sh` — daily report of framework checkouts behind their newest release
- `selfstart-snapshot.sh` — after an unplanned reboot, counts which bots self-started; run it before any rescue
- `boot-capture.sh` — records every declared bot at each host boot (opt-in)

**Maintenance and updates**
- `log-rotate.sh` — rotate one bot's logs
- `log-rotate-fleet.sh` — rotate every bot's logs
- `git-pull-all.sh` — pull every repo in a bot's `projects/`
- `data-sweep.sh` — weekly purge of vetted ephemeral files in bots' `data/`
- `check-npx-cache.sh` — check that MCP server packages are cached
- `update-claude-code.sh` — daily Claude Code update, no fleet bounce; in place by default, staged only when armed (opt-in)
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
- `signal-guard.sh` — PreToolUse hook: refuses a signal to a process the caller did not start: pkill/killall, a pid not from `$!`, a job spec or a pid file (#1069)
- `signal-decide.py` — the decision half of `signal-guard.sh`
- `heavy-slot-guard.sh` — PreToolUse hook: queues heavy Bash commands (suites, installs, builds) on the host's heavy-job slot (opt-in)
- `heavy-slot.py` — the heavy-job slot: `hook` finds heavy commands, `run` holds a slot or queues a ticket, `run --profile render` gates and watches a render, `status` names holders and the queue
- `public-write-guard.sh` — PreToolUse hook: refuses a GitHub-bound write that would put a term from the host's list into a public repository (opt-in)
- `public-write-guard.py` — the decision half of `public-write-guard.sh`

## Repository Hygiene — MANDATORY

### What goes in git (shared, reusable, generalized)

Everything in these top-level directories is committed and shared:

- `library/` — All composable building blocks, voices included (see Architecture above)
- `templates/` — Jinja2 templates for CLAUDE.md generation
- `claudlobby/_runtime_scripts/` — Lifecycle and utility scripts
- `claudlobby/` — Python compositor source
- `documentation/` — Architecture docs, schema reference, setup guides
- `fleet.yaml.example` — Template manifest (committed; `fleet.yaml` is NOT)

### What stays local (gitignored, fleet-specific, secret)

These are ALL gitignored — never commit them:

- `fleet.yaml` — Your active fleet config. Copy from `fleet.yaml.example`.
- `local/` — Fleet overlays. Each `local/<fleet>/` contains fleet.yaml, local library overrides (voices in `library/voices/`), and runtime output. **All fleet-specific content lives here.**
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

Each `AGENTS.md` is a **committed byte-for-byte copy of the `CLAUDE.md` beside it**, and each `.agents/skills/<name>/` a copy of `.claude/skills/<name>/` (copies, because `tests/prepare_resources.py` refuses symlinks). Edit the Claude-side file, then copy it over (`cp CLAUDE.md AGENTS.md`); never reword the copy.

Codex reads the `AGENTS.md` chain from the root to its start folder within one 32 KiB budget (measured), so a nested file gets only what the files above it leave; tell Codex to read it. `tests/test_instruction_budget.py` enforces the copies, the indexes and both budgets: the chain through each nested file's rules (up to its `## Script reference`) fits in 32 KiB, and each nested file stays under Claude Code's 150k-character warning. Keep one line per thing here; a script's history goes in its own header.

How Claude Code and Codex load these files is in [`documentation/instruction-files.md`](documentation/instruction-files.md); it adds no rule.

### Adding library content

Each library category has its own format. Check the category's `README.md` for specifics. General rules:

1. Create the file in the appropriate `library/<category>/` directory
2. Use YAML frontmatter with `title:` and `description:` fields
3. Add an H1 heading (`# Title`) matching the frontmatter title — the loader strips it to avoid duplication in composed output
4. Use `{{BOT_NAME}}`, `{{FLEET_NAME}}`, `{{CLAUDLOBBY_ROOT}}` Jinja2 placeholders where appropriate
5. In an independent canary installation, inspect the bot's rendered CLAUDE.md before starting it.
6. Commit to a branch, PR, review

**Heading levels matter.** The template renders library content inside `##`/`###` sections. The loader runs `_demote_headings` to shift all headings down. An H1 (`#`) in your file becomes H2 in the output. If you start with H3, it becomes H4 — which may be too deep.

### Adding compositor features

1. Edit Python source in `claudlobby/`
2. In a disposable checkout with private HOME/TMPDIR, install the dev dependencies, prepare resources with `.venv/bin/python tests/prepare_resources.py --disposable-checkout "$PWD"`, then run the affected existing tests (details below).
3. Review authored changes and run `config validate` in an independent canary installation.
4. Apply the reviewed commit with `host update apply FULL_COMMIT`; inspect status and behavior. First installation follows the getting-started guide.
5. Commit to a branch, PR, review

**Before writing or trusting a test, read [`documentation/test-suite.md`](documentation/test-suite.md).** Never test from a live fleet root; run unsandboxed; the suite can go red, so compare two separately prepared exports (before, after) on failing test **names**, the **counts** line and the **exit code**; never pipe pytest into grep; quarantine a flaky test (`@pytest.mark.quarantine(issue=<N>)`), never deselect it.

### Adding or modifying claudlobby/_runtime_scripts/ scripts

Read [`claudlobby/_runtime_scripts/CLAUDE.md`](claudlobby/_runtime_scripts/CLAUDE.md) first: the authoring rules and each script's full reference. Two rules reach beyond it: no apostrophes in comments inside `$( )` (bash 3.2; `tests/test_bash_parse.py` also checks `library/**/*.sh`), and an empty roster must never license a write or delete: there use `declared_bots_strict`, never `parse_fleet_bots` or `bot_in_fleet` (#1146). A new script gets one line in the index above; its detail goes in its own header.

### Validating changes to how a bot behaves — MANDATORY

Any change that affects **how a bot behaves at runtime** (claudlobby/_runtime_scripts/ supervision & observability scripts, hooks, skills, protocols, guardrails, principles, composed `bot.conf` env) must be **empirically validated** before merge: unit tests prove composition, only running the code proves behavior. Run it in an independent canary root (its own labels, `unit_prefix`, Plane state and channels), never a production root, watch the real behavior happen, and **cite the observation in the PR body**: claimed evidence is not evidence. Full text: [`documentation/validating-bot-changes.md`](documentation/validating-bot-changes.md).

### Validating changes to the onboarding path — MANDATORY

Any change to **what a brand-new user is told to run** — `README.md`, `documentation/getting-started.md`, `.claude/skills/setup/SKILL.md`, `claudlobby host setup`, `fleet.yaml.seed`, `.env.seed.example` — must be validated **on a cold host**, not from your checkout (#947): **export, do not clone**; **scrub the environment first**; **run the documented commands verbatim**; **log every exploration event** and report the count; **stop at the credential gate**. `tests/test_cold_start_contract.py` is a floor, not a substitute. **Cite the cold run in the PR body.** Procedure: [`documentation/validating-cold-start.md`](documentation/validating-cold-start.md).

### Never hand-edit generated output

Files in `runtime/bots/<name>/` are composed by the Git updater. Hand-edits are overwritten when the updater publishes generated output. To change a bot's config:

1. Edit `fleet.yaml` (fleet-level config) or `library/<category>/` (content)
2. Review and validate authored changes in an independent canary, then operator `host update apply FULL_COMMIT`. Inspect `host update status`; continue interruptions with the original request. `config diff` checks published drift, not unapplied source.
3. If the bot drifted during a session (`claudlobby config diff --bot <name>` shows changed files), review the changes and edit their authored source in `library/`

### Every PR body: a Harness section and a Rollout check

CI's `rollout-check / Rollout check` fails a PR whose body has no `## Harness` or no `## Rollout check` section. `gh pr create --body` and the API skip `.github/pull_request_template.md`, so copy its lines into the body and fill them in. Under `## Harness`: `- **CI:**` (what CI ran: the harness job's scenarios, any test that runs an instrument), `- **Live:**` (each instrument or live test run, with its result), or both; or the one line `N/A: <why none applies>`. Under `## Rollout check`: `- **Observe:**`, `- **Control:**`, `- **When:**`, `- **Who:**`. A PR that changes only `documentation/**`, `README.md`, `CHANGELOG.md` or `changelog.d/**` may put the one line `N/A: docs-only` there instead, and one that changes only `tests/**` or `changelog.d/**` the line `N/A: tests-only`. Rules: `library/guardrails/verify-rollout.md`.

### Changelog

Entries go in `changelog.d/<head branch, each / as ->.md`, never in `CHANGELOG.md`.

## Key Commands

The rest of the command list (composition, operations, a manager's acts on one open task, scaffolding and migrations) is in [`documentation/key-commands.md`](documentation/key-commands.md).

```bash
# After an unplanned reboot — RUN THIS BEFORE RESCUING ANYTHING
claudlobby/_runtime_scripts/selfstart-snapshot.sh  # how many bots self-started (#1002)

# Testing (the venv is required — PEP 668 refuses a bare install on Homebrew/Debian)
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[dev]'
./.venv/bin/pytest  # run test suite (unsandboxed)
```

Use `--fleet` for fleet reads/authoring; updates cover the installation. Validate in independent roots. Existing sealed hosts require reviewed conversion. Git roster moves/removal remain unsupported.

## Python Package Structure

The module map (every module, what it owns and the rules behind it) is in [`documentation/architecture/module-map.md`](documentation/architecture/module-map.md). Where to start: `__main__.py` → `commands/` (the CLI), `config.py` (`fleet.yaml` parsing), `composer.py` (generation), `loader.py` (library loading), `validator.py`, `paths.py`, `switches.py` (the switch registry), and `plane/` (the observable plane; reference: `documentation/architecture/observable-plane.md`).
